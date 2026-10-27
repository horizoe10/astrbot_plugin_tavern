from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


TURN_STATE_KEY = "__tavern_turn_order__"
TURN_STATE_VERSION = 1


def _user_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(ord(char) < 32 for char in text):
        return ""
    return text


def _round_no(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(1_000_000_000, parsed))


def normalize_turn_state(
    value: Any,
    *,
    allowed_user_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    allowed = (
        {_user_id(item) for item in allowed_user_ids}
        if allowed_user_ids is not None
        else None
    )
    if allowed is not None:
        allowed.discard("")

    order: list[str] = []
    raw_order = raw.get("order")
    if isinstance(raw_order, (list, tuple)):
        for item in raw_order[:500]:
            user_id = _user_id(item)
            if (
                user_id
                and user_id not in order
                and (allowed is None or user_id in allowed)
            ):
                order.append(user_id)

    current_user_id = _user_id(raw.get("current_user_id"))
    if current_user_id not in order:
        current_user_id = order[0] if order else ""

    return {
        "version": TURN_STATE_VERSION,
        "round_no": _round_no(raw.get("round_no")),
        "order": order,
        "current_user_id": current_user_id,
    }


def turn_state_from_world(
    world_state: Mapping[str, Any] | None,
    *,
    allowed_user_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    state = world_state if isinstance(world_state, Mapping) else {}
    return normalize_turn_state(
        state.get(TURN_STATE_KEY),
        allowed_user_ids=allowed_user_ids,
    )


def public_world_state(
    world_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = deepcopy(dict(world_state or {}))
    result.pop(TURN_STATE_KEY, None)
    return result


def embed_turn_state(
    world_state: Mapping[str, Any] | None,
    turn_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = public_world_state(world_state)
    normalized = normalize_turn_state(turn_state)
    if normalized["order"]:
        result[TURN_STATE_KEY] = normalized
    return result


def join_turn(
    state: Mapping[str, Any] | None,
    user_id: str,
) -> tuple[dict[str, Any], bool]:
    normalized = normalize_turn_state(state)
    member = _user_id(user_id)
    if not member:
        raise ValueError("用户 ID 无效")
    if member in normalized["order"]:
        return normalized, False
    normalized["order"].append(member)
    if not normalized["current_user_id"]:
        normalized["current_user_id"] = member
    return normalized, True


def leave_turn(
    state: Mapping[str, Any] | None,
    user_id: str,
) -> tuple[dict[str, Any], bool]:
    normalized = normalize_turn_state(state)
    member = _user_id(user_id)
    if member not in normalized["order"]:
        return normalized, False

    old_order = list(normalized["order"])
    removed_index = old_order.index(member)
    was_current = normalized["current_user_id"] == member
    new_order = [item for item in old_order if item != member]
    normalized["order"] = new_order

    if not new_order:
        normalized["current_user_id"] = ""
        return normalized, True
    if was_current:
        if removed_index >= len(new_order):
            normalized["current_user_id"] = new_order[0]
            normalized["round_no"] += 1
        else:
            normalized["current_user_id"] = new_order[removed_index]
    elif normalized["current_user_id"] not in new_order:
        normalized["current_user_id"] = new_order[0]
    return normalized, True


def advance_turn(
    state: Mapping[str, Any] | None,
    current_user_id: str,
) -> dict[str, Any]:
    normalized = normalize_turn_state(state)
    actor = _user_id(current_user_id)
    if not normalized["order"]:
        raise ValueError("回合队列为空")
    if normalized["current_user_id"] != actor:
        raise ValueError("当前行动者不匹配")

    current_index = normalized["order"].index(actor)
    next_index = (current_index + 1) % len(normalized["order"])
    normalized["current_user_id"] = normalized["order"][next_index]
    if next_index == 0:
        normalized["round_no"] += 1
    return normalized


def replace_turn_order(
    state: Mapping[str, Any] | None,
    order: Iterable[str],
) -> dict[str, Any]:
    normalized = normalize_turn_state(state)
    new_order: list[str] = []
    for item in order:
        user_id = _user_id(item)
        if not user_id:
            raise ValueError("回合顺序包含无效用户 ID")
        if user_id in new_order:
            raise ValueError("回合顺序不能包含重复用户")
        new_order.append(user_id)

    old_current = normalized["current_user_id"]
    normalized["order"] = new_order
    normalized["current_user_id"] = (
        old_current
        if old_current in new_order
        else (new_order[0] if new_order else "")
    )
    return normalized
