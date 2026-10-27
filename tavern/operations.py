from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def transport_event_id(event: Any) -> str:
    """Best-effort stable message identity across AstrBot adapters."""

    candidates = [
        getattr(event, "message_id", ""),
        getattr(getattr(event, "message_obj", None), "message_id", ""),
        getattr(getattr(event, "message_obj", None), "id", ""),
    ]
    getter = getattr(event, "get_message_id", None)
    if callable(getter):
        try:
            candidates.insert(0, getter())
        except Exception:
            pass
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text[:160]
    return ""


def operation_key(
    session_id: str,
    operation_type: str,
    *,
    turn_no: int = 0,
    actor_id: str = "",
    source_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Build a stable idempotency key without storing raw private content."""

    digest = hashlib.sha256(
        json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return ":".join(
        (
            str(operation_type or "operation")[:40],
            str(session_id or "")[:80],
            str(max(0, int(turn_no))),
            str(actor_id or "")[:80],
            str(source_id or "")[:80],
            digest,
        )
    )


def recovery_summary(
    operations: Sequence[Mapping[str, Any]],
    *,
    session_state: str,
    has_active_choices: bool,
    has_active_vote: bool,
) -> dict[str, Any]:
    pending = [x for x in operations if str(x.get("status")) == "pending"]
    failed = [x for x in operations if str(x.get("status")) == "failed"]
    phases = [
        str((x.get("result") or {}).get("phase") or "reserved")
        for x in pending
    ]
    if pending:
        action = "inspect_pending_operation"
        message = "检测到未完成事务，请先核对生成、提交与发送阶段。"
    elif session_state == "running" and not has_active_choices and not has_active_vote:
        action = "rebuild_choices"
        message = "故事处于运行状态但没有活动选项或投票，建议重建当前选项。"
    else:
        action = "none"
        message = "未发现需要人工恢复的事务状态。"
    return {
        "healthy": action == "none",
        "recommended_action": action,
        "message": message,
        "pending_count": len(pending),
        "failed_count": len(failed),
        "pending_phases": phases,
        "operations": [dict(x) for x in operations],
    }
