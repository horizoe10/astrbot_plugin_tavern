"""A16：人工 DM 受控操作领域仓库。

提供剧情插入/公告/密语、行动锁定、输入锁定、强制结束投票、关系调整等
受控命令集的数据库能力。所有操作走统一事务 + 审计；权限判定在上层
（``tavern/permissions.py``）完成。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..database_support import new_id, json_dump, json_load, utc_now
from ..resolution import apply_state_patch


class DmRepositoryMixin:
    def _insert_event(
        self,
        connection: Any,
        session_id: str,
        *,
        role: str,
        content: str,
        actor_id: str = "",
        actor_name: str = "",
        meta: Any = None,
        turn_no: int | None = None,
    ) -> str:
        event_id = new_id("event")
        now = utc_now()
        if turn_no is None:
            row = connection.execute(
                "SELECT turn_no FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            turn_no = int(row["turn_no"]) if row else 0
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                turn_no,
                role,
                actor_id,
                actor_name,
                content,
                json_dump(meta or {}),
                now,
            ),
        )
        return event_id

    # ── 剧情控制 ──────────────────────────────────────────────────
    async def insert_dm_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
        mode: str = "append",
    ) -> dict[str, Any]:
        """插入/覆盖剧情正文：写入 role=narrator 事件并标记 edited_by_dm。"""
        return await self._run(
            self._insert_dm_narrative,
            session_id,
            str(narrative or "").strip(),
            actor_id,
            str(mode or "append").strip(),
        )

    def _insert_dm_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
        mode: str,
    ) -> dict[str, Any]:
        if not narrative:
            raise ValueError("剧情正文不能为空")
        if mode not in {"append", "override"}:
            raise ValueError("模式必须为 append 或 override")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(
                    connection,
                    session_id,
                    role="narrator",
                    content=narrative,
                    actor_id=actor_id,
                    actor_name="DM",
                    meta={"edited_by_dm": True, "mode": mode},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.narrative",
                    event_id,
                    {"mode": mode},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"event_id": event_id, "mode": mode}

    async def publish_announcement(
        self,
        session_id: str,
        text: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._publish_announcement, session_id, str(text or "").strip(), actor_id
        )

    def _publish_announcement(
        self,
        session_id: str,
        text: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not text:
            raise ValueError("公告内容不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(
                    connection,
                    session_id,
                    role="announcement",
                    content=text,
                    actor_id=actor_id,
                    actor_name="DM",
                    meta={"visibility": "public"},
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm.announce", event_id, {}
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"event_id": event_id}

    async def whisper_to(
        self,
        session_id: str,
        text: str,
        participant_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._whisper_to,
            session_id,
            str(text or "").strip(),
            str(participant_id or "").strip(),
            actor_id,
        )

    def _whisper_to(
        self,
        session_id: str,
        text: str,
        participant_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not text or not participant_id:
            raise ValueError("密语内容与目标角色不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(
                    connection,
                    session_id,
                    role="whisper",
                    content=text,
                    actor_id=actor_id,
                    actor_name="DM",
                    meta={"visibility": "private", "target_participant_id": participant_id},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.whisper",
                    event_id,
                    {"participant_id": participant_id},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"event_id": event_id, "participant_id": participant_id}

    # ── 行动 / 输入锁定 ───────────────────────────────────────────
    async def set_action_lock(
        self,
        session_id: str,
        participant_id: str,
        locked: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_action_lock, session_id, participant_id, bool(locked), actor_id
        )

    def _set_action_lock(
        self,
        session_id: str,
        participant_id: str,
        locked: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE participants SET action_locked = ?, updated_at = ?
                    WHERE id = ? AND session_id = ?
                    """,
                    (int(locked), utc_now(), participant_id, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.action_lock",
                    participant_id,
                    {"locked": locked},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"participant_id": participant_id, "locked": locked}

    async def set_input_lock(
        self,
        session_id: str,
        locked: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_input_lock, session_id, bool(locked), actor_id
        )

    def _set_input_lock(
        self,
        session_id: str,
        locked: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE sessions SET input_locked = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (int(locked), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.input_lock",
                    "",
                    {"locked": locked},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"session_id": session_id, "locked": locked}

    # ── 投票控制 ──────────────────────────────────────────────────
    async def force_end_vote(
        self,
        session_id: str,
        winner_key: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """强制结束进行中的集体投票并指定最终结果。"""
        return await self._run(
            self._force_end_vote, session_id, str(winner_key or "").strip(), actor_id
        )

    def _force_end_vote(
        self,
        session_id: str,
        winner_key: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                vote_row = connection.execute(
                    """
                    SELECT * FROM group_votes
                    WHERE session_id = ? AND status = 'open'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not vote_row:
                    raise ValueError("当前没有进行中的集体投票")
                valid_keys = {
                    str(item.get("key"))
                    for item in json_load(vote_row["options_json"], [])
                }
                if winner_key and winner_key not in valid_keys:
                    raise ValueError(
                        "指定结果必须是当前投票选项之一："
                        + " / ".join(sorted(valid_keys))
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE group_votes
                    SET status = 'passed', winner_key = ?, ended_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (winner_key, now, now, vote_row["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.vote.force_end",
                    str(vote_row["id"]),
                    {"winner_key": winner_key},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"vote_id": str(vote_row["id"]), "winner_key": winner_key}

    # ── 关系调整 ──────────────────────────────────────────────────
    async def apply_relationship_delta(
        self,
        session_id: str,
        source: str,
        target: str,
        dimension: str,
        delta: int,
        actor_id: str,
    ) -> dict[str, Any]:
        """DM 手动调整关系/好感度（写入受控世界状态并保存快照）。"""
        return await self._run(
            self._apply_relationship_delta,
            session_id,
            str(source or "").strip(),
            str(target or "").strip(),
            str(dimension or "信任").strip(),
            int(delta or 0),
            actor_id,
        )

    def _apply_relationship_delta(
        self,
        session_id: str,
        source: str,
        target: str,
        dimension: str,
        delta: int,
        actor_id: str,
    ) -> dict[str, Any]:
        if not source or not target:
            raise ValueError("关系双方不能为空")
        if not -20 <= delta <= 20:
            raise ValueError("好感度单次调整范围 -20..20")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise ValueError("会话不存在")
                state = json_load(session["world_state_json"], {})
                new_state = apply_state_patch(
                    state,
                    {
                        "relationship_ops": [
                            {
                                "source": source,
                                "target": target,
                                "dimension": dimension,
                                "delta": delta,
                            }
                        ]
                    },
                )
                connection.execute(
                    """
                    UPDATE sessions SET world_state_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (
                        json_dump(new_state),
                        utc_now(),
                        session_id,
                        int(session["revision"]),
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.relationship",
                    "",
                    {
                        "source": source,
                        "target": target,
                        "dimension": dimension,
                        "delta": delta,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"source": source, "target": target, "delta": delta}

    async def pending_operations(self, session_id: str) -> list[dict[str, Any]]:
        """查看副本等待中的生成任务（供 DM 取消卡死任务）。"""
        return await self._run(self._pending_operations, session_id)

    def _pending_operations(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM action_operations
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 50
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]


    # ── 检定记录 ──────────────────────────────────────────────────
    async def record_manual_roll(
        self,
        session_id: str,
        participant_id: str,
        stat: str,
        total: int,
        note: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """DM 手动记录一次检定结果（写事件 + 审计，不生成回执）。"""
        return await self._run(
            self._record_manual_roll,
            session_id,
            str(participant_id or "").strip(),
            str(stat or "").strip(),
            int(total or 0),
            str(note or "").strip(),
            actor_id,
        )

    def _record_manual_roll(
        self,
        session_id: str,
        participant_id: str,
        stat: str,
        total: int,
        note: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not stat:
            raise ValueError("检定名称不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event_id = self._insert_event(
                    connection,
                    session_id,
                    role="roll",
                    content=f"{stat} 检定：{total}",
                    actor_id=actor_id,
                    actor_name="DM",
                    meta={
                        "manual": True,
                        "stat": stat,
                        "total": total,
                        "note": note,
                        "participant_id": participant_id,
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm.manual_roll",
                    event_id,
                    {"stat": stat, "total": total},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"event_id": event_id, "stat": stat, "total": total}

    # ── A17：统一操作幂等（action_operations） ──────────────────────
    async def claim_action_operation(
        self,
        session_id: str,
        operation_id: str,
        kind: str,
        actor_id: str,
        operator_id: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """尝试认领一次操作（幂等）：已存在则返回 replay，否则写入 committed 状态。"""
        return await self._run(
            self._claim_action_operation,
            session_id,
            str(operation_id or "").strip(),
            str(kind or "action").strip(),
            str(actor_id or "").strip(),
            str(operator_id or "").strip(),
            dict(context or {}),
        )

    def _claim_action_operation(
        self,
        session_id: str,
        operation_id: str,
        kind: str,
        actor_id: str,
        operator_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if not operation_id:
            raise ValueError("缺少 operation_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM action_operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return {
                        "claimed": False,
                        "replay": True,
                        "operation_id": operation_id,
                        "status": existing["status"],
                    }
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO action_operations(
                        id, session_id, kind, actor_id, operator_id,
                        context_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        kind,
                        actor_id,
                        operator_id,
                        json_dump(context),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"claimed": True, "replay": False, "operation_id": operation_id}

    async def update_action_operation(
        self,
        operation_id: str,
        status: str,
    ) -> int:
        return await self._run(
            self._update_action_operation, str(operation_id or "").strip(),
            str(status or "committed").strip(),
        )

    def _update_action_operation(
        self,
        operation_id: str,
        status: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE action_operations SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, utc_now(), operation_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return cursor.rowcount
