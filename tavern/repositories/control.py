"""Session-level AI/DM control mode and non-player narrative commits."""

from ..database_support import *


class ControlRepositoryMixin:
    @staticmethod
    def _control_state(row: sqlite3.Row | None, session_id: str) -> dict[str, Any]:
        if not row:
            return {
                "session_id": session_id,
                "mode": "auto",
                "active_dm_user_id": "",
                "phase": "auto",
                "directive": "",
                "beat_no": 0,
                "current_actor_type": "",
                "current_actor_ref": "",
                "preserved_turn": {},
                "revision": 0,
                "updated_at": "",
            }
        return {
            "session_id": row["session_id"],
            "mode": row["mode"],
            "active_dm_user_id": row["active_dm_user_id"],
            "phase": row["phase"],
            "directive": row["directive"],
            "beat_no": int(row["beat_no"]),
            "current_actor_type": row["current_actor_type"],
            "current_actor_ref": row["current_actor_ref"],
            "preserved_turn": json_load(row["preserved_turn_json"], {}),
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_control_state(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_control_state, session_id)

    def _get_control_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dm_control_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return self._control_state(row, session_id)

    async def enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(
            self._enable_dm_mode, session_id, dm_user_id, actor_id
        )

    def _enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        dm_user_id = validate_platform_id(dm_user_id, label="主持人用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("主持模式只能在运行中的副本开启")
                if connection.execute(
                    "SELECT 1 FROM group_votes WHERE session_id = ? AND status = 'open'",
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError(
                        "当前存在未结束的集体投票，请先结束或明确取消投票"
                    )
                now = utc_now()
                turn = turn_state_from_world(json_load(session["world_state_json"], {}))
                connection.execute(
                    """
                    INSERT INTO dm_control_states(
                        session_id, mode, active_dm_user_id, phase, directive,
                        beat_no, current_actor_type, current_actor_ref,
                        preserved_turn_json, revision, created_at, updated_at
                    ) VALUES (?, 'dm', ?, 'awaiting_dm', '', 0, '', '', ?, 1, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        mode = 'dm', active_dm_user_id = excluded.active_dm_user_id,
                        phase = 'awaiting_dm', directive = '',
                        current_actor_type = '', current_actor_ref = '',
                        preserved_turn_json = excluded.preserved_turn_json,
                        revision = dm_control_states.revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, dm_user_id, json_dump(turn), now, now),
                )
                connection.execute(
                    """
                    UPDATE choice_sets SET status = 'superseded', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'turn'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm_mode_enabled",
                    dm_user_id,
                    {"choice_status": "superseded_by_dm", "preserved_turn": turn},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(
            self._set_dm_directive, session_id, directive, actor_id
        )

    def _set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        directive = clean_text(directive, max_chars=4000)
        if not directive:
            raise ValueError("主持指引不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    raise InvalidTransitionError("当前未开启主持模式")
                if actor_id != row["active_dm_user_id"]:
                    raise PermissionError("只有当前活动 DM 可以设置指引")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET directive = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (directive, now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_directive_saved",
                    session_id, {"length": len(directive)},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_dm_handoff(
        self,
        session_id: str,
        actor_type: str,
        actor_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_dm_handoff,
            session_id,
            actor_type,
            actor_ref,
            actor_id,
        )

    def _set_dm_handoff(
        self,
        session_id: str,
        actor_type: str,
        actor_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        actor_type = str(actor_type or "").lower()
        if actor_type not in {"player", "npc"}:
            raise ValueError("交棒目标必须是 player 或 npc")
        actor_ref = clean_text(actor_ref, max_chars=160)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    raise InvalidTransitionError("当前未开启主持模式")
                if actor_id != row["active_dm_user_id"]:
                    raise PermissionError("只有当前活动 DM 可以交棒")
                phase = "player_handoff" if actor_type == "player" else "npc_handoff"
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET phase = ?,
                        current_actor_type = ?, current_actor_ref = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (phase, actor_type, actor_ref, now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_handoff_started",
                    actor_ref, {"actor_type": actor_type},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def finish_dm_handoff(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._finish_dm_handoff, session_id)

    def _finish_dm_handoff(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    connection.execute("COMMIT")
                    return self._control_state(row, session_id)
                now = utc_now()
                connection.execute(
                    "UPDATE choice_sets SET status = 'superseded', updated_at = ? WHERE session_id = ? AND status = 'active'",
                    (now, session_id),
                )
                connection.execute(
                    "UPDATE timer_instances SET status = 'cancelled', updated_at = ? WHERE session_id = ? AND timer_type = 'turn' AND status IN ('active','paused')",
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE dm_control_states SET phase = 'awaiting_dm',
                        current_actor_type = '', current_actor_ref = '',
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def disable_dm_mode(
        self, session_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(self._disable_dm_mode, session_id, actor_id)

    def _disable_dm_mode(self, session_id: str, actor_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return self._control_state(None, session_id)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET mode = 'auto', phase = 'auto',
                        directive = '', current_actor_type = '',
                        current_actor_ref = '', revision = revision + 1,
                        updated_at = ? WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_mode_disabled",
                    session_id, {},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def commit_dm_beat(
        self,
        *,
        session_id: str,
        expected_revision: int,
        dm_user_id: str,
        instruction: str,
        narrative: str,
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]] = (),
        model_payload: Mapping[str, Any] | None = None,
        workflow: Mapping[str, Any] | None = None,
        direct: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_dm_beat,
            session_id,
            expected_revision,
            dm_user_id,
            instruction,
            narrative,
            dict(world_state),
            [dict(item) for item in memories],
            dict(model_payload or {}),
            dict(workflow or {}),
            direct,
        )

    def _commit_dm_beat(
        self,
        session_id: str,
        expected_revision: int,
        dm_user_id: str,
        instruction: str,
        narrative: str,
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        model_payload: dict[str, Any],
        workflow: dict[str, Any],
        direct: bool,
    ) -> dict[str, Any]:
        instruction = clean_text(instruction, max_chars=4000)
        narrative = clean_text(narrative, max_chars=12000)
        if not narrative:
            raise ValueError("主持剧情不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                control = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not session or not control:
                    raise DatabaseNotFoundError("副本或主持状态不存在")
                if int(session["revision"]) != int(expected_revision):
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("暂停或非运行状态不能主持推进")
                if control["mode"] != "dm" or control["active_dm_user_id"] != dm_user_id:
                    raise PermissionError("只有当前活动 DM 可以提交主持剧情")
                if connection.execute(
                    "SELECT 1 FROM group_votes WHERE session_id = ? AND status = 'open'",
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError("集体投票进行中，不能主持推进")

                self._insert_snapshot(
                    connection,
                    session,
                    f"undo-before-dm-{int(control['beat_no']) + 1}-revision-{session['revision']}",
                    "undo",
                    dm_user_id,
                    replace=False,
                )
                now = utc_now()
                new_turn = int(session["turn_no"]) + 1
                beat_no = int(control["beat_no"]) + 1
                stored_state = json_load(session["world_state_json"], {})
                turn = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(public_world_state(world_state), turn)
                event_id = new_id("event")
                meta = {
                    "dm_beat": True,
                    "dm_beat_no": beat_no,
                    "direct": bool(direct),
                    "instruction": instruction,
                }
                if model_payload:
                    meta["model_payload"] = model_payload
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'narrator', ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, session_id, new_turn, dm_user_id,
                        "主持人直述" if direct else "主持推进",
                        narrative, json_dump(meta), now,
                    ),
                )
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND participation_status = 'active'
                      AND card_status = 'approved'
                    ORDER BY CASE WHEN group_user_id = ? THEN 0 ELSE 1 END,
                             created_at LIMIT 1
                    """,
                    (session_id, turn.get("current_user_id") or ""),
                ).fetchone()
                workflow_result: dict[str, Any] = {}
                if participant and workflow:
                    workflow_result = self._apply_v05_turn_ops(
                        connection,
                        session=session,
                        participant=participant,
                        new_turn=new_turn,
                        acting_round=int(turn.get("round_no") or 1),
                        source_event_id=event_id,
                        workflow=workflow,
                        check_payload={},
                        now=now,
                    )
                for memory in memories[:12]:
                    content = clean_text(memory.get("content"), max_chars=1200)
                    if not content:
                        continue
                    scope = str(memory.get("scope") or "world")
                    scope_id = str(memory.get("scope_id") or "")
                    kind = str(memory.get("kind") or "fact")
                    fingerprint = memory_fingerprint(
                        session_id, scope, scope_id, kind, content
                    )
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at, last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            new_id("memory"), session_id, scope, scope_id, kind,
                            content, max(1, min(5, int(memory.get("importance", 3)))),
                            json_dump(list(memory.get("tags") or [])), fingerprint,
                            event_id, now, now, now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ? WHERE id = ?
                    """,
                    (new_turn, json_dump(persisted_state), now, session_id),
                )
                connection.execute(
                    """
                    UPDATE dm_control_states SET beat_no = ?, directive = '',
                        phase = 'awaiting_dm', current_actor_type = '',
                        current_actor_ref = '', revision = revision + 1,
                        updated_at = ? WHERE session_id = ?
                    """,
                    (beat_no, now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    dm_user_id,
                    "dm_narrative_appended" if direct else "dm_beat_committed",
                    event_id,
                    {"beat_no": beat_no, "turn_no": new_turn, "workflow": workflow_result},
                )
                connection.execute("COMMIT")
                return {
                    "session_id": session_id,
                    "event_id": event_id,
                    "beat_no": beat_no,
                    "turn_no": new_turn,
                    "narrative": narrative,
                    "workflow": workflow_result,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

