from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


KNOWLEDGE_LEVELS = (
    "unknown",
    "rumor",
    "basic",
    "familiar",
    "expert",
    "insider",
)
SELECTION_MODES = {
    "single",
    "multiple",
    "ranked",
    "conditional",
    "system_assigned",
    "author_fixed",
}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def normalize_preset_dimensions(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    card = card if isinstance(card, Mapping) else {}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_sequence(card.get("preset_dimensions"))):
        if not isinstance(raw, Mapping):
            continue
        dimension_id = str(raw.get("id") or "").strip()
        if not dimension_id or dimension_id in seen:
            continue
        seen.add(dimension_id)
        selection = raw.get("selection")
        selection = selection if isinstance(selection, Mapping) else {}
        mode = str(selection.get("mode") or raw.get("mode") or "single").lower()
        if mode not in SELECTION_MODES:
            mode = "single"
        default_min = 0 if not raw.get("required", True) else 1
        minimum = max(0, int(selection.get("min", default_min) or 0))
        default_max = 1 if mode in {"single", "conditional"} else max(1, minimum)
        maximum = max(minimum, int(selection.get("max", default_max) or default_max))
        if mode in {"single", "conditional", "system_assigned", "author_fixed"}:
            maximum = 1
            minimum = min(minimum, 1)
        options: list[dict[str, Any]] = []
        option_ids: set[str] = set()
        for option_raw in _sequence(raw.get("options")):
            if not isinstance(option_raw, Mapping):
                continue
            option = deepcopy(dict(option_raw))
            option_id = str(option.get("id") or option.get("value") or "").strip()
            label = str(option.get("label") or option.get("name") or option_id).strip()
            if not option_id or not label or option_id in option_ids:
                continue
            option_ids.add(option_id)
            option["id"] = option_id
            option.setdefault("value", label)
            option.setdefault("label", label)
            option.setdefault("enabled", True)
            options.append(option)
        result.append(
            {
                "id": dimension_id,
                "label": str(raw.get("label") or dimension_id).strip(),
                "description": str(raw.get("description") or "").strip(),
                "selection": {"mode": mode, "min": minimum, "max": maximum},
                "required": bool(raw.get("required", minimum > 0)),
                "allow_custom": bool(raw.get("allow_custom", False)),
                "randomizable": bool(raw.get("randomizable", False)),
                "player_editable": bool(raw.get("player_editable", True)),
                "visibility": str(raw.get("visibility") or "player"),
                "display_order": int(raw.get("display_order", (index + 1) * 10)),
                "page_size": max(1, min(10, int(raw.get("page_size", 5) or 5))),
                "options": options,
            }
        )
    return sorted(result, key=lambda item: (item["display_order"], item["id"]))


def validate_preset_dimensions(card: Mapping[str, Any] | None) -> dict[str, Any]:
    card = card if isinstance(card, Mapping) else {}
    raw_dimensions = _sequence(card.get("preset_dimensions"))
    dimensions = normalize_preset_dimensions(card)
    if len(dimensions) != len(raw_dimensions):
        raise ValueError("preset_dimensions 存在空 ID、重复 ID 或非法维度")
    known = {item["id"] for item in dimensions}
    option_count = 0
    for dimension in dimensions:
        if dimension["required"] and not dimension["options"]:
            raise ValueError(f"必选预设维度 {dimension['id']} 没有有效选项")
        option_count += len(dimension["options"])
        ids = [str(item["id"]) for item in dimension["options"]]
        if len(ids) != len(set(ids)):
            raise ValueError(f"预设维度 {dimension['id']} 存在重复选项 ID")
        for option in dimension["options"]:
            for rule_name in ("requirements", "conflicts"):
                for rule in _sequence(option.get(rule_name)):
                    if not isinstance(rule, Mapping):
                        raise ValueError(
                            f"{dimension['id']}.{option['id']}.{rule_name} 必须是对象数组"
                        )
                    dependency = str(rule.get("dimension") or "")
                    if dependency and dependency not in known:
                        raise ValueError(
                            f"{dimension['id']}.{option['id']} 引用了不存在的维度 {dependency}"
                        )
    return {"dimension_count": len(dimensions), "option_count": option_count}


def validate_preset_selection(
    card: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Validate selected stable IDs against v4 requirements and conflicts."""
    dimensions = normalize_preset_dimensions(card)
    selected_snapshots = _selected_snapshots(fields)
    selected: dict[str, set[str]] = {
        key: {
            str(item.get("id") or item.get("value") or "")
            for item in values
            if str(item.get("id") or item.get("value") or "")
        }
        for key, values in selected_snapshots.items()
    }
    if require_complete:
        for dimension in dimensions:
            count = len(selected.get(dimension["id"], set()))
            minimum = int(dimension["selection"]["min"])
            maximum = int(dimension["selection"]["max"])
            if not minimum <= count <= maximum:
                raise ValueError(
                    f"{dimension['label']}必须选择 {minimum}—{maximum} 项"
                )
    for dimension_id, snapshots in selected_snapshots.items():
        for option in snapshots:
            label = str(option.get("label") or option.get("id") or "该预设")
            for rule in _sequence(option.get("requirements")):
                if not isinstance(rule, Mapping):
                    continue
                dependency = str(rule.get("dimension") or "")
                expected = set(_unique(_sequence(rule.get("values"))))
                actual = selected.get(dependency, set())
                if expected and not (actual & expected):
                    if actual or require_complete:
                        raise ValueError(
                            f"{label} 的前置条件未满足：{dependency}"
                        )
            for rule in _sequence(option.get("conflicts")):
                if not isinstance(rule, Mapping):
                    continue
                dependency = str(rule.get("dimension") or "")
                blocked = set(_unique(_sequence(rule.get("values"))))
                if blocked & selected.get(dependency, set()):
                    raise ValueError(
                        f"{label} 与已选择的 {dependency} 互斥"
                    )
    return {"valid": True, "selected": {k: sorted(v) for k, v in selected.items()}}


def dimension_fields(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dimension in normalize_preset_dimensions(card):
        mode = dimension["selection"]["mode"]
        if mode in {"system_assigned", "author_fixed"}:
            continue
        multi = mode in {"multiple", "ranked"}
        result.append(
            {
                "key": dimension["id"],
                "label": dimension["label"],
                "required": dimension["required"],
                "private": dimension["visibility"] in {"host", "private"},
                "max_chars": 4000,
                "type": "multi_select" if multi else "preset_select",
                "options": deepcopy(dimension["options"]),
                "page_size": dimension["page_size"],
                "min_choices": dimension["selection"]["min"],
                "max_choices": dimension["selection"]["max"],
                "display_order": dimension["display_order"],
                "preset_dimension": True,
            }
        )
    return result


def _boundary_sources(world: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    card = rules.get("character_card")
    card = card if isinstance(card, Mapping) else {}
    return rules, card


def _selected_snapshots(fields: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    refs = fields.get("_preset_refs")
    refs = refs if isinstance(refs, Mapping) else {}
    result: dict[str, list[Mapping[str, Any]]] = {}
    for key, value in refs.items():
        entries = value if isinstance(value, list) else [value]
        snapshots: list[Mapping[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            snapshot = entry.get("snapshot")
            if isinstance(snapshot, Mapping):
                snapshots.append(snapshot)
        result[str(key)] = snapshots
    return result


def resolve_character_presets(
    world: Mapping[str, Any], fields: Mapping[str, Any]
) -> dict[str, Any]:
    rules, card = _boundary_sources(world)
    knowledge = rules.get("knowledge_boundary")
    knowledge = knowledge if isinstance(knowledge, Mapping) else {}
    content = rules.get("content_boundary")
    content = content if isinstance(content, Mapping) else {}
    knowledge_profiles = rules.get("knowledge_profiles") or card.get("knowledge_profiles")
    knowledge_profiles = knowledge_profiles if isinstance(knowledge_profiles, Mapping) else {}
    content_profiles = rules.get("content_profiles") or card.get("content_profiles")
    content_profiles = content_profiles if isinstance(content_profiles, Mapping) else {}

    knowledge_result: dict[str, Any] = {
        "policy": str(knowledge.get("policy") or "strict"),
        "global_rules": _unique(_sequence(knowledge.get("global_rules"))),
        "forbidden_domains": _unique(_sequence(knowledge.get("forbidden_domains"))),
        "metagame_policy": str(knowledge.get("metagame_policy") or "deny"),
        "unknown_fact_behavior": str(
            knowledge.get("unknown_fact_behavior")
            or "表现为不知道、误解或请求检定"
        ),
        "domains": {},
        "known_facts": [],
        "unknown_facts": [],
        "sources": ["world"],
    }
    content_result: dict[str, Any] = {
        "rating": str(content.get("rating") or "general"),
        "hard_denials": _unique(_sequence(content.get("hard_denials"))),
        "fade_to_black": _unique(_sequence(content.get("fade_to_black"))),
        "allowed_with_limits": dict(content.get("allowed_with_limits") or {}),
        "narrative_rules": _unique(_sequence(content.get("narrative_rules"))),
        "player_may_tighten": bool(content.get("player_may_tighten", True)),
        "player_may_relax": False,
        "sources": ["world"],
    }

    explicit_unknown: set[str] = set()
    domain_levels: dict[str, list[str]] = {}
    for dimension_id, snapshots in _selected_snapshots(fields).items():
        for snapshot in snapshots:
            option_id = str(snapshot.get("id") or snapshot.get("value") or "")
            source_label = f"{dimension_id}:{option_id}".rstrip(":")
            knowledge_ref = str(snapshot.get("knowledge_profile_ref") or "")
            content_ref = str(snapshot.get("content_profile_ref") or "")
            profile = knowledge_profiles.get(knowledge_ref.removeprefix("knowledge."))
            if not isinstance(profile, Mapping):
                profile = knowledge_profiles.get(knowledge_ref)
            if isinstance(profile, Mapping):
                knowledge_result["sources"].append(source_label)
                for domain, raw_level in dict(profile.get("domains") or {}).items():
                    level = str(raw_level or "unknown").lower()
                    if level not in KNOWLEDGE_LEVELS:
                        level = "unknown"
                    domain_levels.setdefault(str(domain), []).append(level)
                    if level == "unknown":
                        explicit_unknown.add(str(domain))
                knowledge_result["known_facts"].extend(
                    _sequence(profile.get("known_facts"))
                )
                knowledge_result["unknown_facts"].extend(
                    _sequence(profile.get("unknown_facts"))
                )
            profile = content_profiles.get(content_ref.removeprefix("content."))
            if not isinstance(profile, Mapping):
                profile = content_profiles.get(content_ref)
            if isinstance(profile, Mapping):
                content_result["sources"].append(source_label)
                content_result["hard_denials"].extend(
                    _sequence(profile.get("additional_denials"))
                )
                content_result["fade_to_black"].extend(
                    _sequence(profile.get("fade_to_black"))
                )
                content_result["narrative_rules"].extend(
                    _sequence(profile.get("narrative_rules"))
                )
    for domain, levels in domain_levels.items():
        if domain in explicit_unknown:
            knowledge_result["domains"][domain] = "unknown"
        else:
            knowledge_result["domains"][domain] = max(
                levels, key=KNOWLEDGE_LEVELS.index
            )
    knowledge_result["known_facts"] = _unique(knowledge_result["known_facts"])
    knowledge_result["unknown_facts"] = _unique(knowledge_result["unknown_facts"])
    unknown = set(knowledge_result["unknown_facts"])
    knowledge_result["known_facts"] = [
        item for item in knowledge_result["known_facts"] if item not in unknown
    ]
    for key in ("hard_denials", "fade_to_black", "narrative_rules", "sources"):
        content_result[key] = _unique(content_result[key])
    knowledge_result["sources"] = _unique(knowledge_result["sources"])
    return {"knowledge": knowledge_result, "content": content_result}


def explain_boundary_sources(
    world: Mapping[str, Any], fields: Mapping[str, Any]
) -> dict[str, Any]:
    return resolve_character_presets(world, fields)


def check_character_knowledge(
    resolved: Mapping[str, Any], domain: str
) -> dict[str, Any]:
    knowledge = resolved.get("knowledge")
    knowledge = knowledge if isinstance(knowledge, Mapping) else {}
    domain = str(domain or "").strip()
    if domain in set(_sequence(knowledge.get("forbidden_domains"))):
        return {"allowed": False, "level": "unknown", "reason": "world_forbidden"}
    level = str((knowledge.get("domains") or {}).get(domain) or "unknown")
    return {"allowed": level != "unknown", "level": level, "reason": "resolved"}


def check_content_permission(
    resolved: Mapping[str, Any], content_tags: Sequence[str]
) -> dict[str, Any]:
    content = resolved.get("content")
    content = content if isinstance(content, Mapping) else {}
    denied = set(_sequence(content.get("hard_denials")))
    blocked = [str(tag) for tag in content_tags if str(tag) in denied]
    return {"allowed": not blocked, "blocked": blocked}


__all__ = [
    "KNOWLEDGE_LEVELS",
    "check_character_knowledge",
    "check_content_permission",
    "dimension_fields",
    "explain_boundary_sources",
    "normalize_preset_dimensions",
    "resolve_character_presets",
    "validate_preset_dimensions",
    "validate_preset_selection",
]
