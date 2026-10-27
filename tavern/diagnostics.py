from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .constants import DATABASE_SCHEMA_VERSION, PLUGIN_VERSION
from .operations import recovery_summary
from .world_contract import world_contract


_SECRET_KEYS = re.compile(
    r"(?:token|secret|password|api[_-]?key|private_origin|unified_origin|system_prompt)",
    re.I,
)
_ID_KEYS = re.compile(
    r"(?:user_id|actor_id|owner_user_id|private_user_id|transport_event_id|operation_id)$",
    re.I,
)


def _mask(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "anon_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEYS.search(key):
        return "[REDACTED]"
    if _ID_KEYS.search(key):
        return _mask(value)
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [redact(item, key) for item in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…[TRUNCATED]"
    return value


async def build_diagnostic_report(database: Any, session_id: str) -> dict[str, Any]:
    session = await database.get_session(session_id)
    session = dict(session)
    world_state = session.get("world_state") or {}
    session["world_state"] = {
        "location": world_state.get("location", ""),
        "time": world_state.get("time", ""),
        "scene_summary": world_state.get("scene_summary", ""),
        "fact_count": len(world_state.get("facts", []))
        if isinstance(world_state.get("facts"), list)
        else 0,
    }
    instance = await database.get_instance_config(session_id)
    events = await database.recent_events(session_id, 30)
    for item in events:
        if item.get("role") == "player":
            item["content"] = "[PLAYER INPUT REDACTED]"
        else:
            item["content"] = str(item.get("content") or "")[:1200]
    audit = await database.list_audit(session_id, 100, 0)
    for item in audit:
        if str(item.get("action") or "").startswith("operation."):
            item["target"] = _mask(item.get("target"))
    operations = await database.list_session_operations(session_id, 50)
    active_choices = await database.active_choice_set(session_id)
    active_vote = await database.active_vote(session_id)
    storage = await database.get_storage_info(session_id)
    providers = await database.list_provider_health()
    contract = world_contract(instance.get("world_snapshot") or {})
    report = {
        "format": "astrbot-tavern-diagnostic",
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "plugin_version": PLUGIN_VERSION,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "session": session,
        "contract_summary": {
            "version": contract.get("version"),
            "stats_mode": contract.get("stats", {}).get("mode"),
            "resolution_mode": contract.get("resolution", {}).get("mode"),
            "dice_system": contract.get("resolution", {}).get("dice_system"),
            "attribute_ids": [x.get("key") for x in contract.get("attributes", [])],
        },
        "workflow": {
            "active_choice_set": active_choices,
            "active_vote": active_vote,
            "operations": operations,
            "recovery": recovery_summary(
                operations,
                session_state=str(session.get("state") or ""),
                has_active_choices=bool(active_choices),
                has_active_vote=bool(active_vote),
            ),
        },
        "recent_events": events,
        "audit": audit,
        "provider_health": providers,
        "storage": storage,
        "privacy": {
            "user_ids": "hashed",
            "credentials": "removed",
            "system_prompt": "removed",
            "private_fields": "removed",
        },
    }
    return redact(report)
