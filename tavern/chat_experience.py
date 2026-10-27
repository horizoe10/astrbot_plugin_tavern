"""Optional world-package contract for multiplayer chat experience.

The module is deliberately world-agnostic: it contains policies and labels,
not hard-coded currencies, attributes, platforms or setting lore.  Missing or
disabled configuration is a complete no-op for legacy packages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "character_creation": {
        "primary": "private_code",
        "fallbacks": ["webui_token"],
    },
    "multiplayer": {
        "spotlight": "round_robin",
        "group_decisions": "vote",
        "absent_player": "standby",
    },
    "safety": {
        "enabled": True,
        "anonymous_pause": False,
        "consent_reminder": "",
        "lines": [],
        "veils": [],
    },
    "continuity": {
        "recap_every_turns": 0,
        "checkpoint_every_turns": 0,
        "unresolved_threads_limit": 8,
        "preserve_npc_intent": True,
    },
    "delivery": {
        "proactive_fallback": "next_event",
        "mention_style": "name",
        "max_text_length": 3500,
    },
    "dm": {
        "allow_narrative_override": True,
        "allow_secret_whispers": True,
        "allow_manual_checks": True,
        "allow_state_intervention": True,
    },
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any, limit: int = 50) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:300])
        if len(result) >= limit:
            break
    return result


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_chat_experience(world: Mapping[str, Any] | None) -> dict[str, Any]:
    source = _mapping(world)
    rules = _mapping(source.get("rules"))
    raw = _mapping(source.get("chat_experience") or rules.get("chat_experience"))
    creation = _mapping(raw.get("character_creation"))
    multiplayer = _mapping(raw.get("multiplayer"))
    safety = _mapping(raw.get("safety"))
    continuity = _mapping(raw.get("continuity"))
    delivery = _mapping(raw.get("delivery"))
    dm = _mapping(raw.get("dm"))
    return {
        "enabled": bool(raw.get("enabled", False)),
        "character_creation": {
            "primary": str(creation.get("primary") or "private_code"),
            "fallbacks": _strings(creation.get("fallbacks"), 3) or ["webui_token"],
        },
        "multiplayer": {
            "spotlight": str(multiplayer.get("spotlight") or "round_robin"),
            "group_decisions": str(multiplayer.get("group_decisions") or "vote"),
            "absent_player": str(multiplayer.get("absent_player") or "standby"),
        },
        "safety": {
            "enabled": bool(safety.get("enabled", True)),
            "anonymous_pause": bool(safety.get("anonymous_pause", False)),
            "consent_reminder": str(safety.get("consent_reminder") or "")[:500],
            "lines": _strings(safety.get("lines")),
            "veils": _strings(safety.get("veils")),
        },
        "continuity": {
            "recap_every_turns": _bounded(continuity.get("recap_every_turns"), 0, 0, 50),
            "checkpoint_every_turns": _bounded(continuity.get("checkpoint_every_turns"), 0, 0, 50),
            "unresolved_threads_limit": _bounded(continuity.get("unresolved_threads_limit"), 8, 0, 30),
            "preserve_npc_intent": bool(continuity.get("preserve_npc_intent", True)),
        },
        "delivery": {
            "proactive_fallback": str(delivery.get("proactive_fallback") or "next_event"),
            "mention_style": str(delivery.get("mention_style") or "name"),
            "max_text_length": _bounded(delivery.get("max_text_length"), 3500, 500, 10000),
        },
        "dm": {
            key: bool(dm.get(key, True))
            for key in DEFAULTS["dm"]
        },
    }


def validate_chat_experience(world: Mapping[str, Any]) -> dict[str, Any]:
    value = normalize_chat_experience(world)
    if not value["enabled"]:
        return value
    allowed = {
        "character_creation.primary": {"private_code", "group_text", "webui_token"},
        "multiplayer.spotlight": {"round_robin", "soft_round_robin", "free"},
        "multiplayer.group_decisions": {"vote", "consensus", "host"},
        "multiplayer.absent_player": {"standby", "delegate", "skip"},
        "delivery.proactive_fallback": {"next_event", "webui_only", "discard"},
        "delivery.mention_style": {"auto", "name", "none"},
    }
    for path, choices in allowed.items():
        section, key = path.split(".", 1)
        actual = str(value[section][key])
        if actual not in choices:
            raise ValueError(f"chat_experience.{path} 无效：{actual}")
    fallbacks = value["character_creation"]["fallbacks"]
    invalid = [item for item in fallbacks if item not in allowed["character_creation.primary"]]
    if invalid:
        raise ValueError("chat_experience.character_creation.fallbacks 存在无效方式：" + "、".join(invalid))
    return value


def narrator_directives(world: Mapping[str, Any]) -> dict[str, Any]:
    value = normalize_chat_experience(world)
    if not value["enabled"]:
        return {}
    return {
        "multiplayer": value["multiplayer"],
        "safety": value["safety"],
        "continuity": value["continuity"],
        "dm_policy": value["dm"],
        "rules": [
            "为每位在场玩家保留可回应空间，不替未行动玩家作决定。",
            "连续性优先：保留未解决线索、NPC 已知事实、承诺和当前意图。",
            "触及内容边界时淡出处理；安全暂停立即生效且无需玩家解释原因。",
        ],
    }


__all__ = [
    "DEFAULTS",
    "narrator_directives",
    "normalize_chat_experience",
    "validate_chat_experience",
]
