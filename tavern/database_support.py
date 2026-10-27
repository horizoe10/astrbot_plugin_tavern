from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from .constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_CHARACTERS,
    DEFAULT_WORLD,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
    SESSION_STATES,
)
from .card_lifecycle import validate_card_revision
from .card_wizard import choose_options, store_preset_snapshots
from .lifecycle import (
    CARD_APPROVED,
    CARD_DRAFT,
    CARD_PENDING,
    CARD_REJECTED,
    CARD_UNCREATED,
    CHOICE_KEYS,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_ARCHIVED,
    PARTICIPANT_AWAY,
    PARTICIPANT_RESERVED,
    PARTICIPANT_RETIRED,
    PARTICIPANT_STANDBY,
    SEAT_HOLDING_STATUSES,
    card_stat_allocation,
    card_template,
    deadline_after,
    next_fillable_card_step,
    repair_profession_preset_draft,
    resolve_profession_stats,
    uses_profession_preset_stats,
    fallback_choices,
    normalize_choices,
    normalize_progress,
    normalize_time_rules,
    opening_choices,
    player_limits,
    safe_exit_narrative,
    initial_character_runtime_state,
    validate_card_template_config,
    utc_now as lifecycle_utc_now,
    vote_result,
    world_session_modules,
    world_time_rules,
)
from .stat_generation import (
    STAT_GENERATION_SNAPSHOT_KEY,
    calculate_preset_stack_stats,
    clear_generated_stats,
    stat_generation_config,
    sync_preset_stack_fields,
    uses_preset_stack_stats,
)
from .resolution import memory_fingerprint
from .presets import resolve_character_presets, validate_preset_selection
from .world_contract import validate_world_contract
from .security import clean_text, truncate_text, validate_platform_id, validate_slug
from .storage import (
    InstanceStorage,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)
from .turns import (
    advance_turn,
    embed_turn_state,
    join_turn,
    leave_turn,
    normalize_turn_state,
    public_world_state,
    replace_turn_order,
    turn_state_from_world,
)

T = TypeVar("T")
TIMER_REMINDER_INTERVAL_SECONDS = 30
CARD_COMPLETION_REMINDER_INTERVAL_SECONDS = 2 * 60
COUNTDOWN_TYPES = (
    "card_code",
    "card_completion",
    "preparation",
    "ready",
    "turn",
    "vote",
    "standby",
    "all_idle",
)
# 同一副本内只允许存在一个在跑的实例；换人/换回合时旧计时器必须作废。
# 否则 继续/读档/回合推进 会不断叠加 turn 计时器，
# 每一轮轮询都按行数重复推送提醒，形成刷屏。
SESSION_SINGLETON_TIMER_TYPES = frozenset(
    {
        "turn",
        "vote",
        "preparation",
        "all_idle",
    }
)


def timer_reminder_interval(
    timer_type: object,
    action: Mapping[str, Any] | None = None,
) -> int:
    if isinstance(action, Mapping):
        try:
            configured = int(action.get("reminder_interval_seconds") or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured > 0:
            return configured
    if str(timer_type or "") == "card_completion":
        return CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
    return TIMER_REMINDER_INTERVAL_SECONDS


def timer_reminder_enabled(
    timer_type: object,
    action: Mapping[str, Any],
) -> bool:
    if str(timer_type or "") == "card_completion":
        return bool(action.get("reminder_enabled", False))
    try:
        return int(action.get("reminder_interval_seconds") or 0) > 0
    except (TypeError, ValueError):
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def clean_card_field(
    value: object,
    *,
    label: str,
    max_chars: int,
) -> str:
    raw = str(value or "")
    if any(character.isspace() for character in raw):
        raise ValueError(
            f"{label}不能包含空格、全角空格、换行或制表符"
        )
    return clean_text(raw, max_chars=max_chars)


def bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse editable JSON integers without letting one bad rule stop play."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return min(maximum, max(minimum, parsed))


class DatabaseConflictError(RuntimeError):
    pass


class DatabaseNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass
