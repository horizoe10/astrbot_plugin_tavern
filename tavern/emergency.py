from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .lifecycle import normalize_choices


class EmergencyService:
    """Small, auditable recovery actions for one damaged workflow component."""

    def __init__(self, database: Any) -> None:
        self.database = database

    async def execute(
        self,
        session_id: str,
        action: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action == "replace_choices":
            instance = await self.database.get_instance_config(session_id)
            choices = normalize_choices(
                payload.get("choices"),
                instance.get("world_snapshot") or {},
            )
            return await self.database.emergency_replace_choices(
                session_id, choices, actor_id
            )
        if action == "edit_last_narrative":
            return await self.database.emergency_edit_last_narrative(
                session_id,
                str(payload.get("narrative") or ""),
                actor_id,
            )
        if action == "bridge_narrative":
            return await self.database.emergency_append_narrative(
                session_id,
                str(payload.get("narrative") or ""),
                actor_id,
            )
        if action == "cancel_operation":
            return await self.database.update_operation(
                str(payload.get("operation_id") or ""),
                status="failed",
                phase="cancelled_by_admin",
                result={"reason": str(payload.get("reason") or "管理员终止")},
                actor_id=actor_id,
            )
        if action == "rollback_before_turn":
            restored = await self.database.restore_latest_auto(
                session_id,
                actor_id,
            )
            return {"session": restored, "action": action}
        raise ValueError("不支持的回合急救操作")
