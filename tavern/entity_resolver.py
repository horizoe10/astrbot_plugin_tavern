"""A16：统一实体显示名解析服务。

目标：内部逻辑使用稳定 ID，界面/群聊优先显示可读名称；任何无法解析的引用
都返回明确的降级名称，绝不把完整内部 ID 直接当普通名称展示。

支持解析：participant / player / NPC(session_characters) / team / 世界包实体 ref，
并兼容旧存档中的「裸 UUID」与「带前缀 ID」两种历史形态。

本模块不依赖 AstrBot；``resolve_label`` 为纯函数，可独立单测。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 已知前缀（按长度降序匹配，避免 world:character_ 被 character_ 抢先截断）
_PREFIX_RE = re.compile(
    r"^(?:"
    r"world:character_|world:entity_|world:|"
    r"participant_|player_|session_character_|snpc_|npc:|npc_|"
    r"character_|team_|party_|"
    r")"
)

_PARTY_WORDS = re.compile(r"队伍|party", re.I)
_UUID_ISH = re.compile(r"^[0-9a-f]{8,32}$")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_IDISH = re.compile(r"^[\w:._-]{8,}$")


def _text(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _short_id(ref: str) -> str:
    return _text(ref)[:8]


def strip_prefix(ref: str) -> str:
    return _PREFIX_RE.sub("", str(ref or "").strip(), count=1)


def _readable_name(ref: str) -> bool:
    """是否是可读的命名实体（中文 / 含空格 / 短名 / 含非常规字符）。"""
    text = str(ref or "").strip()
    if not text:
        return False
    if _CJK.search(text):
        return True
    if " " in text:
        return True
    if len(text) < 8:
        return True
    if not _IDISH.match(text):
        return True
    return False


def fallback_name(ref: str) -> str:
    """无法解析时的降级显示名。

    - 可读名称（组织/势力/地区等叙事中的名字）原样展示，不再“未知实体(<名>)”；
    - 真正的内部 ID（uuid / 带前缀 ID）才显示“未知角色/已离开玩家/已删除实体/未知实体”。"""
    text = str(ref or "").strip()
    if not text:
        return "未知实体"
    if _PARTY_WORDS.search(text):
        return "队伍"
    lowered = text.lower()
    if lowered.startswith(("player_", "participant_")):
        return "已离开玩家"
    if lowered.startswith(("npc:", "npc_", "snpc_", "character_", "world:")):
        return "已删除实体"
    if _readable_name(text):
        return text
    short = _short_id(text)
    return f"未知实体({short})"


def build_participant_labels(roster: Any) -> dict[str, dict[str, Any]]:
    """从 roster 构建 participant 标签表（id / uuid 后缀 / user_id / 名称 → 信息）。"""
    labels: dict[str, dict[str, Any]] = {}
    for item in roster if isinstance(roster, list) else []:
        if not isinstance(item, Mapping):
            continue
        pid = _text(item.get("id"))
        user_id = _text(item.get("group_user_id") or item.get("user_id"))
        name = _text(
            item.get("character_name") or item.get("display_name")
        ) or user_id
        if not pid and not user_id:
            continue
        info = {
            "id": pid,
            "type": "participant",
            "name": name,
            "source": "participant",
            "deleted": False,
            "departed": _text(item.get("participation_status")) not in {"", "active"},
        }
        for key in (
            pid,
            strip_prefix(pid) if pid else "",
            user_id,
            name,
        ):
            if key and key not in labels:
                labels[key] = info
    return labels


def build_entity_labels(
    roster: Any,
    session_characters: Any = None,
    world_characters: Any = None,
) -> dict[str, dict[str, Any]]:
    """构建统一的实体标签表（参与者 + 会话 NPC + 世界角色 + 队伍）。

    每个实体以 稳定 ID / uuid 后缀 / user_id / stable_key / 名称 / `npc:` 前缀
    等多种键指向同一标签信息，供写入规范化与前端解析复用。
    """
    labels: dict[str, dict[str, Any]] = {}
    labels.update(build_participant_labels(roster))

    def add(key: Any, info: dict[str, Any]) -> None:
        key = str(key or "").strip()
        if key and key not in labels:
            labels[key] = info

    for item in session_characters if isinstance(session_characters, list) else []:
        if not isinstance(item, Mapping):
            continue
        sid = _text(item.get("id"))
        name = _text(item.get("name"))
        if not sid and not name:
            continue
        info = {
            "id": sid,
            "type": "npc",
            "name": name or sid,
            "source": "session_npc",
            "deleted": _text(item.get("lifecycle_status")) == "archived",
            "departed": False,
        }
        add(sid, info)
        add(strip_prefix(sid) if sid else "", info)
        add(item.get("stable_key"), info)
        add(name, info)
        add(f"npc:{name}", info) if name else None

    for item in world_characters if isinstance(world_characters, list) else []:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        info = {
            "id": _text(item.get("id")) or f"world:character:{name}",
            "type": "npc",
            "name": name,
            "source": "world_character",
            "deleted": False,
            "departed": False,
        }
        add(name, info)
        add(f"npc:{name}", info)

    for key in ("队伍", "party", "team"):
        add(key, {"id": "team", "type": "team", "name": "队伍", "source": "team",
                  "deleted": False, "departed": False})
    return labels


def resolve_label(
    labels: Mapping[str, Any],
    ref: Any,
) -> dict[str, Any]:
    """解析单个引用（纯函数）。返回 {name, id, type, ...}，永不抛错。"""
    text = str(ref or "").strip()
    if not text:
        return {"name": "", "type": "unknown", "id": "", "fallback": False}

    if text in labels:
        return dict(labels[text])

    if _PARTY_WORDS.search(text):
        return {"name": "队伍", "type": "team", "id": "team", "fallback": False}

    stripped = strip_prefix(text)
    if stripped and stripped != text:
        if stripped in labels:
            return dict(labels[stripped])
        # 带前缀但剥离后仍是 uuid 后缀 → 后缀匹配
        matched = _suffix_match(labels, stripped)
        if matched is not None:
            return matched

    if len(text) >= 8:
        matched = _suffix_match(labels, text)
        if matched is not None:
            return matched

    if _readable_name(text):
        # 可读名称：作为通用命名实体返回（组织/势力/地区/自定义实体等）。
        return {
            "name": text,
            "type": "entity",
            "id": text,
            "generic": True,
            "fallback": False,
        }

    return {
        "name": fallback_name(text),
        "type": "unknown",
        "id": text,
        "short_id": _short_id(text),
        "fallback": True,
    }


def _suffix_match(
    labels: Mapping[str, Any],
    needle: str,
) -> dict[str, Any] | None:
    if len(needle) < 8:
        return None
    for key, info in labels.items():
        key = str(key or "")
        if len(key) > 8 and key.endswith(needle) and isinstance(info, Mapping):
            return dict(info)
    return None


def resolve_pair_label(
    labels: Mapping[str, Any],
    source: Any,
    target: Any,
) -> dict[str, Any]:
    """解析关系键两侧，返回统一标签（含是否为队伍）。"""
    left = resolve_label(labels, source)
    right = resolve_label(labels, target)
    return {
        "name": f"{left['name']} → {right['name']}",
        "left": left,
        "right": right,
        "type": "relationship",
        "is_party": bool(_PARTY_WORDS.search(str(source or "")))
        or bool(_PARTY_WORDS.search(str(target or ""))),
        "fallback": bool(left.get("fallback") or right.get("fallback")),
    }


def normalize_relationship_ref(
    labels: Mapping[str, Any],
    ref: Any,
) -> str:
    """把关系 source/target 规范化为稳定引用，避免再写入裸 UUID。

    规则：
    - 命中 participant（id / user_id / 名称 / uuid 后缀）→ 返回 participant id；
    - 命中队伍词 → 返回 "队伍"；
    - 其他（NPC 名称、世界实体 ref 等）→ 原样保留（显示层负责解析）。
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    if _PARTY_WORDS.search(text):
        return "队伍"
    resolved = resolve_label(labels, text)
    if not resolved.get("fallback") and resolved.get("id"):
        return str(resolved["id"])
    # 未解析的裸 uuid 前缀化，便于识别但不可解析时仍走降级
    if _UUID_ISH.match(text):
        return f"participant_{text}"
    return text


def normalize_relationship_ops(
    ops: Any,
    labels: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """规范化 relationship_ops 的 source/target（在 apply_state_patch 之前调用）。"""
    if not isinstance(ops, list):
        return []
    normalized: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, Mapping):
            continue
        item = dict(op)
        item["source"] = normalize_relationship_ref(labels, item.get("source"))
        item["target"] = normalize_relationship_ref(labels, item.get("target"))
        normalized.append(item)
    return normalized


__all__ = [
    "build_entity_labels",
    "build_participant_labels",
    "fallback_name",
    "normalize_relationship_ops",
    "normalize_relationship_ref",
    "resolve_label",
    "resolve_pair_label",
    "strip_prefix",
]
