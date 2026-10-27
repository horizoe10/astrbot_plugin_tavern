from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any


WIZARD_PAGE_KEY = "_wizard_pages"
PRESET_REFS_KEY = "_preset_refs"
LAST_MESSAGE_KEY = "_last_card_message"
NAV_NEXT = {"下一页", "下页", "next"}
NAV_PREVIOUS = {"上一页", "上页", "prev", "previous"}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _mapping_path(root: Mapping[str, Any], path: str) -> Any:
    current: Any = root
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def field_visible(
    field: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> bool:
    condition = field.get("visible_when")
    if not isinstance(condition, Mapping) or not condition:
        return True
    values = values if isinstance(values, Mapping) else {}
    for dependency, expected in condition.items():
        actual = values.get(str(dependency))
        refs = values.get(PRESET_REFS_KEY)
        ref = (
            refs.get(str(dependency), {})
            if isinstance(refs, Mapping)
            else {}
        )
        actual_candidates = {
            str(actual or ""),
            str(ref.get("id") or "") if isinstance(ref, Mapping) else "",
            str(ref.get("value") or "") if isinstance(ref, Mapping) else "",
            str(ref.get("label") or "") if isinstance(ref, Mapping) else "",
        }
        allowed = _sequence(expected)
        if allowed:
            if not actual_candidates & {str(item) for item in allowed}:
                return False
        elif actual != expected:
            return False
    return True


def _preset_source_value(
    template: Mapping[str, Any], field: Mapping[str, Any]
) -> Any:
    source = str(
        field.get("preset_source") or field.get("options_source") or ""
    ).strip()
    if not source:
        return field.get("options")
    candidates = [source]
    if source.startswith("rules.character_card."):
        candidates.append(source.removeprefix("rules.character_card."))
    if source.startswith("character_card."):
        candidates.append(source.removeprefix("character_card."))
    candidates.extend((f"preset_sets.{source}", source.removesuffix("_presets")))
    for candidate in dict.fromkeys(candidates):
        value = _mapping_path(template, candidate)
        if _sequence(value):
            return value
    return field.get("options")


def preset_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not field_visible(field, values):
        return []
    value_field = str(field.get("value_field") or "value")
    label_field = str(field.get("label_field") or "label")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_sequence(_preset_source_value(template, field))):
        if isinstance(raw, Mapping):
            source = dict(raw)
            preset_id = str(
                source.get("id")
                or source.get("key")
                or source.get(value_field)
                or source.get("name")
                or source.get(label_field)
                or index + 1
            ).strip()
            value = str(
                source.get(value_field)
                or source.get("selection_value")
                or source.get("value")
                or source.get("name")
                or source.get(label_field)
                or preset_id
            ).strip()
            label = str(
                source.get(label_field)
                or source.get("label")
                or source.get("name")
                or value
            ).strip()
            aliases = [str(item).strip() for item in _sequence(source.get("aliases"))]
            description = str(
                source.get("description")
                or source.get("role")
                or source.get("summary")
                or ""
            ).strip()
        else:
            value = label = preset_id = str(raw or "").strip()
            aliases = []
            description = ""
            source = {"value": value, "label": label}
        identity = preset_id.casefold()
        if not value or not label or not identity:
            continue
        if identity in seen:
            raise ValueError(
                f"预设字段 {field.get('key') or '?'} 存在重复稳定 ID："
                f"{preset_id}"
            )
        seen.add(identity)
        result.append(
            {
                "id": preset_id,
                "value": value,
                "label": label,
                "description": description,
                "aliases": aliases,
                "source": source,
            }
        )
    return result


def page_size(field: Mapping[str, Any]) -> int:
    try:
        value = int(field.get("page_size", 5))
    except (TypeError, ValueError):
        value = 5
    return min(10, max(1, value))


def current_page(values: Mapping[str, Any], field_key: str) -> int:
    pages = values.get(WIZARD_PAGE_KEY)
    pages = pages if isinstance(pages, Mapping) else {}
    try:
        return max(0, int(pages.get(field_key, 0)))
    except (TypeError, ValueError):
        return 0


def paged_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    options = preset_options(template, field, values)
    size = page_size(field)
    total_pages = max(1, (len(options) + size - 1) // size)
    page = min(current_page(values, str(field.get("key") or "")), total_pages - 1)
    start = page * size
    return {
        "options": options,
        "items": options[start : start + size],
        "page": page,
        "page_number": page + 1,
        "total_pages": total_pages,
        "page_size": size,
    }


def navigate_page(
    values: dict[str, Any],
    field: Mapping[str, Any],
    command: str,
    *,
    total_options: int,
) -> bool:
    normalized = str(command or "").strip().casefold()
    if normalized not in NAV_NEXT | NAV_PREVIOUS:
        return False
    size = page_size(field)
    total_pages = max(1, (max(0, total_options) + size - 1) // size)
    key = str(field.get("key") or "")
    page = current_page(values, key)
    if normalized in NAV_NEXT:
        page = min(total_pages - 1, page + 1)
    else:
        page = max(0, page - 1)
    pages = values.get(WIZARD_PAGE_KEY)
    pages = dict(pages) if isinstance(pages, Mapping) else {}
    pages[key] = page
    values[WIZARD_PAGE_KEY] = pages
    return True


def choose_option(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    submitted: str,
) -> dict[str, Any]:
    page = paged_options(template, field, values)
    text = str(submitted or "").strip()
    if text.isdigit():
        ordinal = int(text)
        if 1 <= ordinal <= len(page["items"]):
            return dict(page["items"][ordinal - 1])
        raise ValueError("序号不在当前页，请从本页显示的序号中选择")
    reference = text.casefold()
    matches = []
    for option in page["options"]:
        candidates = {
            str(option.get("id") or "").casefold(),
            str(option.get("value") or "").casefold(),
            str(option.get("label") or "").casefold(),
            *(str(item).casefold() for item in option.get("aliases", [])),
        }
        if reference and reference in candidates:
            matches.append(option)
    if len(matches) == 1:
        return dict(matches[0])
    if len(matches) > 1:
        raise ValueError("该名称对应多个预设，请改用稳定 ID 或本页序号")
    raise ValueError("必须从当前步骤显示的预设项中选择")


def choose_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    submitted: str,
) -> list[dict[str, Any]]:
    """Resolve a multi-select answer while preserving the player's order."""

    page = paged_options(template, field, values)
    tokens = [
        token.strip()
        for token in re.split(r"[,，、;；\s]+", str(submitted or "").strip())
        if token.strip()
    ]
    if not tokens:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in tokens:
        if token.isdigit():
            ordinal = int(token)
            if not 1 <= ordinal <= len(page["items"]):
                raise ValueError("序号不在当前页，请从本页显示的序号中选择")
            option = dict(page["items"][ordinal - 1])
        else:
            option = choose_option(template, field, values, token)
        identity = str(option.get("id") or "").casefold()
        if identity and identity not in seen:
            result.append(option)
            seen.add(identity)
    minimum = max(0, int(field.get("min_choices", 0) or 0))
    maximum = max(minimum, int(field.get("max_choices", 100) or 100))
    if len(result) < minimum or len(result) > maximum:
        raise ValueError(f"本项必须选择 {minimum}—{maximum} 个预设")
    return result


def store_preset_snapshot(
    values: dict[str, Any], field_key: str, option: Mapping[str, Any]
) -> None:
    refs = values.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    source = option.get("source")
    refs[field_key] = {
        "id": str(option.get("id") or ""),
        "value": str(option.get("value") or ""),
        "label": str(option.get("label") or ""),
        "snapshot": deepcopy(dict(source)) if isinstance(source, Mapping) else {},
    }
    values[PRESET_REFS_KEY] = refs


def store_preset_snapshots(
    values: dict[str, Any], field_key: str, options: Sequence[Mapping[str, Any]]
) -> None:
    refs = values.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    refs[field_key] = [
        {
            "id": str(option.get("id") or ""),
            "value": str(option.get("value") or ""),
            "label": str(option.get("label") or ""),
            "snapshot": deepcopy(dict(option.get("source") or {})),
        }
        for option in options
    ]
    values[PRESET_REFS_KEY] = refs


def clear_field_and_dependents(
    template: Mapping[str, Any], values: dict[str, Any], field_key: str
) -> None:
    fields = [item for item in template.get("fields", []) if isinstance(item, Mapping)]
    changed_definition = next(
        (
            item
            for item in fields
            if str(item.get("key") or "") == str(field_key)
        ),
        {},
    )
    pending = [
        str(field_key),
        *[
            str(item)
            for item in _sequence(changed_definition.get("clear_on_change"))
        ],
    ]
    while pending:
        changed = pending.pop(0)
        values.pop(changed, None)
        refs = values.get(PRESET_REFS_KEY)
        if isinstance(refs, dict):
            refs.pop(changed, None)
        for field in fields:
            key = str(field.get("key") or "")
            visible_when = field.get("visible_when")
            depends = isinstance(visible_when, Mapping) and changed in {
                str(item) for item in visible_when
            }
            if depends and key in values and key not in pending:
                pending.append(key)


def preset_source_exists(template: Mapping[str, Any], field: Mapping[str, Any]) -> bool:
    return bool(preset_options(template, field, {}))


__all__ = [
    "LAST_MESSAGE_KEY",
    "NAV_NEXT",
    "NAV_PREVIOUS",
    "PRESET_REFS_KEY",
    "WIZARD_PAGE_KEY",
    "choose_option",
    "choose_options",
    "clear_field_and_dependents",
    "current_page",
    "field_visible",
    "navigate_page",
    "paged_options",
    "preset_options",
    "preset_source_exists",
    "store_preset_snapshot",
    "store_preset_snapshots",
]
