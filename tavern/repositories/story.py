"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *


class StoryRepositoryMixin:
    async def recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self._run(self._recent_events, session_id, limit)

    def _recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT s.history_floor_seq, sr.recovery_json
                FROM sessions s
                LEFT JOIN session_rule_states sr ON sr.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            recovery = json_load(session["recovery_json"], {})
            excluded_ranges: list[tuple[int, int]] = []
            if isinstance(recovery, Mapping):
                for item in recovery.get("excluded_event_ranges", []):
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    try:
                        start, end = int(item[0]), int(item[1])
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if start <= end:
                        excluded_ranges.append((start, end))
            exclusions = "".join(
                " AND NOT (seq BETWEEN ? AND ?)"
                for _ in excluded_ranges
            )
            parameters: list[Any] = [
                session_id,
                session["history_floor_seq"],
            ]
            for start, end in excluded_ranges:
                parameters.extend((start, end))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE session_id = ? AND seq >= ?
                {exclusions}
                ORDER BY seq DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            return [self._event(row) for row in reversed(rows)]

    async def append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._append_ooc,
            session_id,
            actor_id,
            actor_name,
            content,
        )

    def _append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            event_id = new_id("event")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'ooc', ?, ?, ?, '{}', ?)
                """,
                (
                    event_id,
                    session_id,
                    session["turn_no"],
                    actor_id,
                    actor_name,
                    content,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return self._event(row)

    async def list_memories(
        self,
        session_id: str,
        query: str = "",
        limit: int = 100,
        *,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_memories,
            session_id,
            query,
            limit,
            include_invalidated,
        )

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        compact = "".join(str(query).lower().split())
        if not compact:
            return set()
        terms = {compact}
        terms.update(
            compact[index : index + 2]
            for index in range(max(0, len(compact) - 1))
        )
        return {term for term in terms if term}

    def _list_memories(
        self,
        session_id: str,
        query: str,
        limit: int,
        include_invalidated: bool,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       mg.visibility AS governance_visibility,
                       mg.locked AS governance_locked,
                       mg.pinned AS governance_pinned,
                       mg.invalidated AS governance_invalidated,
                       mg.supersedes_id AS governance_supersedes_id,
                       mg.conflict_status AS governance_conflict_status,
                       mg.note AS governance_note
                FROM memories m
                LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                WHERE m.session_id = ?
                  AND (? OR COALESCE(mg.invalidated, 0) = 0)
                ORDER BY COALESCE(mg.pinned, 0) DESC,
                         COALESCE(mg.locked, 0) DESC,
                         m.importance DESC, m.salience DESC, m.updated_at DESC
                LIMIT 500
                """,
                (session_id, int(include_invalidated)),
            ).fetchall()
            memories = [self._memory(row) for row in rows]
            terms = self._query_terms(query)
            if terms:
                for memory in memories:
                    haystack = "".join(
                        (
                            memory["content"]
                            + " "
                            + " ".join(memory["tags"])
                            + " "
                            + memory["kind"]
                        )
                        .lower()
                        .split()
                    )
                    matches = sum(term in haystack for term in terms)
                    memory["_score"] = (
                        matches * 5
                        + memory["importance"] * 2
                        + float(memory["salience"])
                    )
                memories = [
                    memory
                    for memory in memories
                    if (
                        memory["locked"]
                        or memory["pinned"]
                        or memory.get("_score", 0) > memory["importance"] * 2
                    )
                ]
                memories.sort(
                    key=lambda item: (
                        int(item["pinned"]),
                        int(item["locked"]),
                        item.get("_score", 0),
                    ),
                    reverse=True,
                )
            protected = [
                memory
                for memory in memories
                if memory["locked"] or memory["pinned"]
            ]
            selected = protected + [
                memory
                for memory in memories
                if memory not in protected
            ][: max(0, limit - len(protected))]
            archived = connection.execute(
                """
                SELECT readonly FROM session_archives
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if selected and not archived:
                now = utc_now()
                connection.executemany(
                    """
                    UPDATE memories
                    SET last_accessed_at = ?, salience = MIN(10, salience + 0.05)
                    WHERE id = ?
                    """,
                    [(now, item["id"]) for item in selected],
                )
            for item in selected:
                item.pop("_score", None)
            return selected

    async def save_memory(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_memory, dict(payload), actor_id)

    def _save_memory(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        memory_id = str(payload.get("id") or "").strip()
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        scope = str(payload.get("scope", "world")).lower()
        if scope not in {"world", "player", "npc"}:
            raise ValueError("非法记忆范围")
        scope_id = clean_text(payload.get("scope_id"), max_chars=128)
        kind = clean_text(payload.get("kind") or "fact", max_chars=32)
        content = clean_text(payload.get("content"), max_chars=1000)
        if not content:
            raise ValueError("记忆内容不能为空")
        try:
            importance = max(1, min(5, int(payload.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        tags_value = payload.get("tags")
        tags = []
        if isinstance(tags_value, list):
            tags = [
                clean_text(item, max_chars=32)
                for item in tags_value[:12]
                if clean_text(item, max_chars=32)
            ]
        fingerprint = memory_fingerprint(
            session_id,
            scope,
            scope_id,
            kind,
            content,
        )
        visibility = str(payload.get("visibility") or "public").lower()
        if visibility not in {"public", "host", "private"}:
            visibility = "public"
        conflict_status = str(
            payload.get("conflict_status") or "clear"
        ).lower()
        if conflict_status not in {"clear", "conflict", "resolved"}:
            conflict_status = "clear"
        supersedes_id = clean_text(
            payload.get("supersedes_id"),
            max_chars=128,
        )
        governance_note = clean_text(
            payload.get("governance_note"),
            max_chars=500,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current_governance = None
                if memory_id:
                    row = connection.execute(
                        "SELECT * FROM memories WHERE id = ?",
                        (memory_id,),
                    ).fetchone()
                    if not row:
                        raise DatabaseNotFoundError("记忆不存在")
                    if row["session_id"] != session_id:
                        raise ValueError("不能把记忆移动到其他副本")
                    current_governance = connection.execute(
                        """
                        SELECT * FROM memory_governance
                        WHERE memory_id = ?
                        """,
                        (memory_id,),
                    ).fetchone()
                    if current_governance:
                        if "visibility" not in payload:
                            visibility = current_governance["visibility"]
                        if "conflict_status" not in payload:
                            conflict_status = current_governance[
                                "conflict_status"
                            ]
                        if "supersedes_id" not in payload:
                            supersedes_id = current_governance[
                                "supersedes_id"
                            ]
                        if "governance_note" not in payload:
                            governance_note = current_governance["note"]
                    connection.execute(
                        """
                        UPDATE memories SET
                            scope = ?, scope_id = ?, kind = ?, content = ?,
                            importance = ?, tags_json = ?, fingerprint = ?,
                            updated_at = ?, last_accessed_at = ?
                        WHERE id = ?
                        """,
                        (
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            memory_id,
                        ),
                    )
                    action = "memory.update"
                else:
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '', ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            tags_json = excluded.tags_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT * FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    memory_id = row["id"]
                    action = "memory.create"
                if supersedes_id:
                    replaced = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE id = ? AND session_id = ?
                        """,
                        (supersedes_id, session_id),
                    ).fetchone()
                    if not replaced or supersedes_id == memory_id:
                        raise ValueError("被替代记忆不存在或不能替代自身")
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, 'public', 0, 0, 1, '', 'resolved',
                                  '已被新事实替代', ?, ?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            invalidated = 1,
                            conflict_status = 'resolved',
                            note = CASE
                                WHEN note = '' THEN '已被新事实替代'
                                ELSE note
                            END,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                        """,
                        (supersedes_id, actor_id, now),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_governance(
                        memory_id, visibility, locked, pinned,
                        invalidated, supersedes_id, conflict_status,
                        note, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        visibility = excluded.visibility,
                        locked = excluded.locked,
                        pinned = excluded.pinned,
                        invalidated = excluded.invalidated,
                        supersedes_id = excluded.supersedes_id,
                        conflict_status = excluded.conflict_status,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        visibility,
                        int(
                            bool(
                                payload.get(
                                    "locked",
                                    current_governance["locked"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "pinned",
                                    current_governance["pinned"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "invalidated",
                                    current_governance["invalidated"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        supersedes_id,
                        conflict_status,
                        governance_note,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    memory_id,
                    {"scope": scope, "kind": kind},
                )
                row = connection.execute(
                    """
                    SELECT m.*,
                           mg.visibility AS governance_visibility,
                           mg.locked AS governance_locked,
                           mg.pinned AS governance_pinned,
                           mg.invalidated AS governance_invalidated,
                           mg.supersedes_id AS governance_supersedes_id,
                           mg.conflict_status AS governance_conflict_status,
                           mg.note AS governance_note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._memory(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_memory(
        self,
        memory_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_memory, memory_id, actor_id)

    def _delete_memory(self, memory_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("记忆不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM memories WHERE id = ?",
                    (memory_id,),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "memory.delete",
                    memory_id,
                    {"kind": row["kind"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        name: str,
        kind: str,
        created_by: str,
        *,
        replace: bool,
    ) -> str:
        name = clean_text(name, max_chars=100)
        if not name:
            raise ValueError("存档名不能为空")
        snapshot_id = new_id("save")
        if replace:
            connection.execute(
                """
                DELETE FROM snapshots
                WHERE session_id = ? AND name = ?
                """,
                (session["id"], name),
            )
        elif connection.execute(
            """
            SELECT 1 FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone():
            raise ValueError(
                "已存在同名存档；请确认后使用覆盖模式"
            )
        sql = """
            INSERT INTO snapshots(
                id, session_id, name, kind, turn_no, session_revision,
                world_id, world_state_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        connection.execute(
            sql,
            (
                snapshot_id,
                session["id"],
                name,
                kind,
                session["turn_no"],
                session["revision"],
                session["world_id"],
                session["world_state_json"],
                created_by,
                utc_now(),
            ),
        )
        row = connection.execute(
            """
            SELECT id FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone()
        snapshot_id = str(row["id"])
        workflow = self._collect_workflow_snapshot(
            connection,
            session["id"],
        )
        connection.execute(
            """
            INSERT INTO snapshot_workflows(snapshot_id, workflow_json)
            VALUES (?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                workflow_json = excluded.workflow_json
            """,
            (snapshot_id, json_dump(workflow)),
        )
        return snapshot_id

    @staticmethod
    def _collect_workflow_snapshot(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        session = connection.execute(
            """
            SELECT history_floor_seq FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        event_anchor_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) FROM events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        tables = {
            "participants": (
                "SELECT * FROM participants WHERE session_id = ?"
            ),
            "character_runtime_states": (
                "SELECT * FROM character_runtime_states WHERE session_id = ?"
            ),
            "choice_sets": (
                "SELECT * FROM choice_sets WHERE session_id = ?"
            ),
            "group_votes": (
                "SELECT * FROM group_votes WHERE session_id = ?"
            ),
            "selected_world_events": (
                "SELECT * FROM selected_world_events WHERE session_id = ?"
            ),
            "timer_instances": (
                "SELECT * FROM timer_instances WHERE session_id = ?"
            ),
            "delegation_grants": (
                "SELECT * FROM delegation_grants WHERE session_id = ?"
            ),
            "permission_grants": (
                "SELECT * FROM permission_grants WHERE session_id = ?"
            ),
            "return_requests": (
                "SELECT * FROM return_requests WHERE session_id = ?"
            ),
            "ban_records": (
                """
                SELECT * FROM ban_records
                WHERE session_id = ? AND scope = 'instance'
                """
            ),
            "session_rule_states": (
                "SELECT * FROM session_rule_states WHERE session_id = ?"
            ),
            "dm_control_states": (
                "SELECT * FROM dm_control_states WHERE session_id = ?"
            ),
            "session_characters": (
                "SELECT * FROM session_characters WHERE session_id = ?"
            ),
            "story_ledger": (
                "SELECT * FROM story_ledger WHERE session_id = ?"
            ),
            "scene_clocks": (
                "SELECT * FROM scene_clocks WHERE session_id = ?"
            ),
            "assist_tokens": (
                "SELECT * FROM assist_tokens WHERE session_id = ?"
            ),
        }
        result: dict[str, Any] = {
            "format": "astrbot-tavern-workflow",
            "version": 3,
            "history_floor_seq": int(
                session["history_floor_seq"] if session else 0
            ),
            "event_anchor_seq": event_anchor_seq,
        }
        vote_ids: list[str] = []
        participant_ids: list[str] = []
        session_character_ids: list[str] = []
        for table, query in tables.items():
            rows = connection.execute(query, (session_id,)).fetchall()
            result[table] = [dict(row) for row in rows]
            if table == "group_votes":
                vote_ids = [str(row["id"]) for row in rows]
            if table == "participants":
                participant_ids = [str(row["id"]) for row in rows]
            if table == "session_characters":
                session_character_ids = [str(row["id"]) for row in rows]
        if vote_ids:
            placeholders = ",".join("?" for _ in vote_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM vote_ballots
                WHERE vote_id IN ({placeholders})
                """,
                tuple(vote_ids),
            ).fetchall()
            result["vote_ballots"] = [dict(row) for row in rows]
        else:
            result["vote_ballots"] = []
        if participant_ids:
            placeholders = ",".join("?" for _ in participant_ids)
            for table in (
                "character_card_drafts",
                "card_binding_codes",
            ):
                rows = connection.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE participant_id IN ({placeholders})
                    """,
                    tuple(participant_ids),
                ).fetchall()
                result[table] = [dict(row) for row in rows]
        else:
            result["character_card_drafts"] = []
            result["card_binding_codes"] = []
        if session_character_ids:
            placeholders = ",".join("?" for _ in session_character_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM session_character_states
                WHERE character_id IN ({placeholders})
                """,
                tuple(session_character_ids),
            ).fetchall()
            result["session_character_states"] = [
                dict(row) for row in rows
            ]
        else:
            result["session_character_states"] = []
        return result

    async def create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._create_snapshot,
            session_id,
            name,
            actor_id,
            replace,
        )

    def _create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        replace: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    name,
                    "manual",
                    actor_id,
                    replace=replace,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.create",
                    snapshot_id,
                    {"name": name, "turn_no": session["turn_no"]},
                )
                row = connection.execute(
                    "SELECT * FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._snapshot(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_snapshots(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_snapshots, session_id)

    def _list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (session_id,),
            ).fetchall()
            return [self._snapshot(row) for row in rows]

    async def restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._restore_snapshot,
            session_id,
            snapshot_ref,
            actor_id,
        )

    def _restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot = connection.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE session_id = ? AND (id = ? OR name = ?)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, snapshot_ref, snapshot_ref),
                ).fetchone()
                if not snapshot:
                    raise DatabaseNotFoundError("存档不存在")

                self._insert_snapshot(
                    connection,
                    session,
                    f"safety-before-restore-{session['revision']}",
                    "safety",
                    actor_id,
                    replace=True,
                )
                max_seq = connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0)
                    FROM events WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
                workflow_row = connection.execute(
                    """
                    SELECT workflow_json FROM snapshot_workflows
                    WHERE snapshot_id = ?
                    """,
                    (snapshot["id"],),
                ).fetchone()
                workflow = json_load(
                    workflow_row["workflow_json"] if workflow_row else "",
                    {},
                )
                if not isinstance(workflow, Mapping):
                    workflow = {}
                floor = bounded_int(
                    workflow.get("history_floor_seq"),
                    int(session["history_floor_seq"] or 0),
                    0,
                    int(max_seq) + 1,
                )
                anchor = bounded_int(
                    workflow.get("event_anchor_seq"),
                    int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0) FROM events
                            WHERE session_id = ? AND created_at <= ?
                            """,
                            (session_id, snapshot["created_at"]),
                        ).fetchone()[0]
                    ),
                    0,
                    int(max_seq),
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id = ?, turn_no = ?, world_state_json = ?,
                        history_floor_seq = ?, revision = revision + 1,
                        state = 'paused', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        snapshot["world_id"],
                        snapshot["turn_no"],
                        snapshot["world_state_json"],
                        floor,
                        now,
                        session_id,
                    ),
                )
                self._restore_workflow_snapshot(
                    connection,
                    snapshot["id"],
                    session_id,
                )
                rule_row = connection.execute(
                    """
                    SELECT recovery_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                recovery = json_load(
                    rule_row["recovery_json"] if rule_row else "",
                    {},
                )
                recovery = (
                    dict(recovery)
                    if isinstance(recovery, Mapping)
                    else {}
                )
                excluded: list[list[int]] = []
                for item in recovery.get("excluded_event_ranges", []):
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    try:
                        start, end = int(item[0]), int(item[1])
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if start <= end:
                        excluded.append([start, end])
                if anchor + 1 <= int(max_seq):
                    excluded.append([anchor + 1, int(max_seq)])
                recovery.update(
                    {
                        "state": "restored",
                        "snapshot_id": str(snapshot["id"]),
                        "event_anchor_seq": anchor,
                        "excluded_event_ranges": excluded[-64:],
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """
                    UPDATE session_rule_states
                    SET recovery_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(recovery), now, session_id),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        new_id("event"),
                        session_id,
                        snapshot["turn_no"],
                        actor_id,
                        f"已恢复存档「{snapshot['name']}」，会话已暂停。",
                        json_dump(
                            {
                                "snapshot_id": snapshot["id"],
                                "restored": True,
                            }
                        ),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.restore",
                    snapshot["id"],
                    {
                        "name": snapshot["name"],
                        "turn_no": snapshot["turn_no"],
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _restore_workflow_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        session_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT workflow_json FROM snapshot_workflows
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if not row:
            # Old snapshots had no workflow section. Cancel incompatible
            # in-flight work rather than mixing it with the restored branch.
            now = utc_now()
            connection.execute(
                """
                UPDATE choice_sets SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE group_votes SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status = 'open'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE timer_instances SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status IN ('active', 'paused')
                """,
                (now, session_id),
            )
            return
        data = json_load(row["workflow_json"], {})
        if data.get("format") != "astrbot-tavern-workflow":
            raise ValueError("存档中的流程快照格式无效")
        for table in (
            "session_character_states",
            "assist_tokens",
            "vote_ballots",
            "group_votes",
            "choice_sets",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "participants",
            "scene_clocks",
            "story_ledger",
            "session_characters",
            "session_rule_states",
            "dm_control_states",
        ):
            if table == "vote_ballots":
                connection.execute(
                    """
                    DELETE FROM vote_ballots
                    WHERE vote_id IN (
                        SELECT id FROM group_votes WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table in {
                "character_card_drafts",
                "card_binding_codes",
            }:
                connection.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "session_character_states":
                connection.execute(
                    """
                    DELETE FROM session_character_states
                    WHERE character_id IN (
                        SELECT id FROM session_characters
                        WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "ban_records":
                connection.execute(
                    """
                    DELETE FROM ban_records
                    WHERE session_id = ? AND scope = 'instance'
                    """,
                    (session_id,),
                )
            else:
                connection.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
        columns: dict[str, tuple[str, ...]] = {
            "participants": (
                "id", "session_id", "player_id", "group_user_id",
                "private_user_id", "private_origin", "display_name",
                "character_card_id", "character_version_id",
                "character_name", "character_code", "aliases_json",
                "card_status", "ready", "participation_status",
                "seat_reserved_at", "joined_round",
                "consecutive_timeouts", "exit_reason",
                "created_at", "updated_at",
            ),
            "character_card_drafts": (
                "id", "participant_id", "template_version", "fields_json",
                "current_step", "status", "expires_at",
                "created_at", "updated_at",
            ),
            "card_binding_codes": (
                "id", "participant_id", "code", "status", "expires_at",
                "private_user_id", "private_origin", "created_at", "used_at",
            ),
            "character_runtime_states": (
                "id", "session_id", "participant_id", "character_card_id",
                "state_json", "revision", "created_at", "updated_at",
            ),
            "choice_sets": (
                "id", "session_id", "participant_id", "round_no",
                "session_revision", "choices_json", "status",
                "reroll_count", "selected_key", "flavor_text",
                "idempotency_key", "created_at", "updated_at",
            ),
            "group_votes": (
                "id", "session_id", "source_event_id", "question",
                "options_json", "eligible_user_ids_json", "stage", "status",
                "winner_key", "suspended_user_id", "deadline_at",
                "result_json", "created_at", "updated_at",
            ),
            "vote_ballots": (
                "id", "vote_id", "user_id", "option_key",
                "created_at", "updated_at",
            ),
            "selected_world_events": (
                "id", "session_id", "round_no", "pool_item_id",
                "payload_json", "status", "narrative",
                "created_at", "resolved_at",
            ),
            "timer_instances": (
                "id", "session_id", "participant_id", "timer_type",
                "status", "deadline_at", "remaining_seconds",
                "reminder_at", "reminder_sent", "action_json",
                "created_at", "updated_at",
            ),
            "delegation_grants": (
                "id", "session_id", "participant_id", "owner_user_id",
                "delegate_user_id", "permissions_json", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "permission_grants": (
                "id", "session_id", "user_id", "role",
                "granted_by", "created_at",
            ),
            "return_requests": (
                "id", "session_id", "participant_id", "requested_by",
                "status", "exit_type", "objective", "progress_json",
                "vote_id", "created_at", "updated_at",
            ),
            "ban_records": (
                "id", "session_id", "platform_id", "group_id", "user_id",
                "participant_id", "scope", "reason", "actor_id", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "session_rule_states": (
                "session_id", "progress_json",
                "content_boundaries_json", "npc_policy_json",
                "context_budget_json", "dice_rules_json", "recovery_json",
                "revision", "created_at", "updated_at",
            ),
            "dm_control_states": (
                "session_id", "mode", "active_dm_user_id", "phase",
                "directive", "beat_no", "current_actor_type",
                "current_actor_ref", "preserved_turn_json", "revision",
                "created_at", "updated_at",
            ),
            "session_characters": (
                "id", "session_id", "stable_key", "name", "aliases_json",
                "role_type", "public_profile_json", "known_facts_json",
                "misconceptions_json", "source", "review_status",
                "lifecycle_status", "persistent", "first_event_id",
                "last_event_id", "first_turn", "last_turn", "revision",
                "created_at", "updated_at",
            ),
            "session_character_states": (
                "character_id", "state_json", "revision", "updated_at",
            ),
            "story_ledger": (
                "id", "session_id", "stable_key", "kind", "title",
                "description", "status", "visibility", "source_event_id",
                "completed_event_id", "revision", "created_at", "updated_at",
            ),
            "scene_clocks": (
                "id", "session_id", "stable_key", "title", "segments",
                "current_value", "visibility", "trigger_text", "status",
                "triggered_event_id", "revision", "created_at", "updated_at",
            ),
            "assist_tokens": (
                "id", "session_id", "source_participant_id",
                "target_participant_id", "stat", "method", "status",
                "expires_round", "source_event_id", "created_at",
                "consumed_at",
            ),
        }
        insert_order = (
            "session_rule_states",
            "dm_control_states",
            "participants",
            "character_card_drafts",
            "card_binding_codes",
            "character_runtime_states",
            "choice_sets",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "session_characters",
            "session_character_states",
            "story_ledger",
            "scene_clocks",
            "assist_tokens",
        )
        for table in insert_order:
            rows = data.get(table, [])
            if table in {
                "session_rule_states",
                "dm_control_states",
                "session_characters",
                "session_character_states",
                "story_ledger",
                "scene_clocks",
                "assist_tokens",
            } and int(data.get("version", 1) or 1) < 2:
                rows = []
            if not isinstance(rows, list):
                raise ValueError(f"流程快照表 {table} 格式错误")
            self._import_rows(
                connection,
                table,
                rows,
                columns[table],
            )
        if not data.get("session_rule_states"):
            self._initialize_current_rows(connection)
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'paused', reminder_at = '', reminder_sent = 0
            WHERE session_id = ? AND status = 'active'
            """,
            (session_id,),
        )

    async def restore_latest_auto(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._restore_latest_auto,
            session_id,
            actor_id,
        )

    def _restore_latest_auto(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM snapshots
                WHERE session_id = ?
                  AND kind IN ('auto', 'safety', 'undo')
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有可回滚的保护点")
        return self._restore_snapshot(session_id, row["id"], actor_id)

    async def delete_snapshot(
        self,
        snapshot_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_snapshot, snapshot_id, actor_id)

    def _delete_snapshot(self, snapshot_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("存档不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                if row["kind"] in {"safety", "undo", "final"}:
                    raise ValueError(
                        "安全快照、回滚点与最终保护存档不能手动删除"
                    )
                connection.execute(
                    "DELETE FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "snapshot.delete",
                    snapshot_id,
                    {"name": row["name"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def commit_turn(
        self,
        *,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]],
        check_payload: Mapping[str, Any] | None,
        model_payload: Mapping[str, Any] | None,
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: Mapping[str, Any] | None = None,
        operation_id: str = "",
        operation_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_turn_sync,
            session_id,
            expected_revision,
            player_id,
            player_user_id,
            player_name,
            player_input,
            narrative,
            dict(world_state),
            [dict(item) for item in memories],
            dict(check_payload or {}),
            dict(model_payload or {}),
            clean_text(director_note, max_chars=500),
            auto_snapshot_interval,
            store_model_payload,
            dict(workflow or {}),
            operation_id,
            dict(operation_result or {}),
        )

    def _commit_turn_sync(
        self,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        check_payload: dict[str, Any],
        model_payload: dict[str, Any],
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: dict[str, Any],
        operation_id: str = "",
        operation_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["revision"] != expected_revision:
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("会话不在运行状态")

                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                if player_user_id not in turn_state["order"]:
                    if turn_state["order"]:
                        raise InvalidTransitionError(
                            "该玩家尚未加入当前回合队列"
                        )
                    turn_state, _ = join_turn(
                        turn_state,
                        player_user_id,
                    )
                if turn_state["current_user_id"] != player_user_id:
                    status = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    raise InvalidTransitionError(
                        f"当前轮到 {status['current_name'] or status['current_user_id']}"
                    )
                acting_round = turn_state["round_no"]
                group_decision = workflow.get("group_decision")
                preserves_action_right = isinstance(
                    group_decision,
                    Mapping,
                )
                next_turn_state = (
                    dict(turn_state)
                    if preserves_action_right
                    else advance_turn(
                        turn_state,
                        player_user_id,
                    )
                )
                if (
                    not preserves_action_right
                    and next_turn_state["round_no"] > acting_round
                ):
                    pending_rows = connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ? AND card_status = 'approved'
                          AND participation_status = 'active'
                          AND joined_round <= ?
                        ORDER BY created_at
                        """,
                        (session_id, next_turn_state["round_no"]),
                    ).fetchall()
                    for pending in pending_rows:
                        pending_user_id = str(pending["group_user_id"])
                        if pending_user_id not in next_turn_state["order"]:
                            next_turn_state, _ = join_turn(
                                next_turn_state,
                                pending_user_id,
                            )
                persisted_world_state = embed_turn_state(
                    public_world_state(world_state),
                    next_turn_state,
                )
                new_turn = session["turn_no"] + 1
                self._insert_snapshot(
                    connection,
                    session,
                    (
                        f"undo-before-turn-{new_turn}"
                        f"-revision-{session['revision']}"
                    ),
                    "undo",
                    "system",
                    replace=False,
                )
                if (
                    auto_snapshot_interval > 0
                    and session["turn_no"] > 0
                    and session["turn_no"] % auto_snapshot_interval == 0
                ):
                    self._insert_snapshot(
                        connection,
                        session,
                        f"auto-turn-{session['turn_no']}",
                        "auto",
                        "system",
                        replace=True,
                    )

                now = utc_now()
                player_event_id = new_id("event")
                narrator_event_id = new_id("event")
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'player', ?, ?, ?, ?, ?)
                    """,
                    (
                        player_event_id,
                        session_id,
                        new_turn,
                        player_user_id,
                        player_name,
                        player_input,
                        json_dump({"player_id": player_id}),
                        now,
                    ),
                )

                workflow_result = self._commit_vnext_workflow(
                    connection,
                    session=session,
                    new_turn=new_turn,
                    acting_round=acting_round,
                    next_turn_state=next_turn_state,
                    player_user_id=player_user_id,
                    player_event_id=player_event_id,
                    narrator_event_id=narrator_event_id,
                    world_state=persisted_world_state,
                    check_payload=check_payload,
                    workflow=workflow,
                    now=now,
                )
                narrator_meta: dict[str, Any] = {}
                if check_payload:
                    narrator_meta["check"] = check_payload
                if store_model_payload and model_payload:
                    narrator_meta["model_payload"] = model_payload
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'narrator', 'narrator',
                              '酒馆叙事者', ?, ?, ?)
                    """,
                    (
                        narrator_event_id,
                        session_id,
                        new_turn,
                        narrative,
                        json_dump(narrator_meta),
                        now,
                    ),
                )
                participant_row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, player_user_id),
                ).fetchone()
                if workflow and participant_row:
                    v05_result = self._apply_v05_turn_ops(
                        connection,
                        session=session,
                        participant=participant_row,
                        new_turn=new_turn,
                        acting_round=acting_round,
                        source_event_id=narrator_event_id,
                        workflow=workflow,
                        check_payload=check_payload,
                        now=now,
                    )
                    workflow_result["v05"] = v05_result
                connection.execute(
                    """
                    UPDATE sessions SET
                        turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_turn,
                        json_dump(persisted_world_state),
                        now,
                        session_id,
                    ),
                )

                for memory in memories[:12]:
                    scope = str(memory.get("scope", "world"))
                    scope_id = str(memory.get("scope_id", ""))
                    kind = str(memory.get("kind", "fact"))
                    content = str(memory.get("content", "")).strip()
                    if not content:
                        continue
                    importance = max(
                        1,
                        min(5, int(memory.get("importance", 3))),
                    )
                    tags = (
                        memory.get("tags")
                        if isinstance(memory.get("tags"), list)
                        else []
                    )
                    fingerprint = memory_fingerprint(
                        session_id,
                        scope,
                        scope_id,
                        kind,
                        content,
                    )
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            narrator_event_id,
                            now,
                            now,
                            now,
                        ),
                    )
                    stored_memory = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    if stored_memory:
                        memory_id = str(stored_memory["id"])
                        visibility = str(
                            memory.get("visibility") or "public"
                        ).lower()
                        if visibility not in {"public", "host", "private"}:
                            visibility = "public"
                        supersedes_id = clean_text(
                            memory.get("supersedes_id"),
                            max_chars=128,
                        )
                        connection.execute(
                            """
                            INSERT INTO memory_governance(
                                memory_id, visibility, locked, pinned,
                                invalidated, supersedes_id, conflict_status,
                                note, updated_by, updated_at
                            ) VALUES (?, ?, ?, ?, 0, ?, 'clear', '',
                                      'narrator', ?)
                            ON CONFLICT(memory_id) DO UPDATE SET
                                visibility = excluded.visibility,
                                locked = MAX(locked, excluded.locked),
                                pinned = MAX(pinned, excluded.pinned),
                                supersedes_id = CASE
                                    WHEN excluded.supersedes_id <> ''
                                    THEN excluded.supersedes_id
                                    ELSE supersedes_id
                                END,
                                updated_at = excluded.updated_at
                            """,
                            (
                                memory_id,
                                visibility,
                                int(bool(memory.get("locked", False))),
                                int(bool(memory.get("pinned", False))),
                                supersedes_id,
                                now,
                            ),
                        )
                        if supersedes_id:
                            connection.execute(
                                """
                                UPDATE memory_governance
                                SET invalidated = 1, updated_by = 'narrator',
                                    updated_at = ?
                                WHERE memory_id = ?
                                """,
                                (now, supersedes_id),
                            )

                self._insert_audit(
                    connection,
                    session_id,
                    player_user_id,
                    "turn.commit",
                    narrator_event_id,
                    {
                        "turn_no": new_turn,
                        "round_no": acting_round,
                        "next_player_user_id": (
                            next_turn_state["current_user_id"]
                        ),
                        "check": check_payload or None,
                        "memory_count": len(memories[:12]),
                        "director_note": director_note,
                        "workflow": workflow_result,
                    },
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'auto'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'undo'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                # 0.11.1：回合提交与事务回执完成合并进同一事务。
                # 此前 commit_turn 成功后再单独 update_operation(completed)，
                # 两者跨事务，崩溃/异常会把已提交回合永久留在 pending，
                # 导致同一行动的后续重试被“该行动正在处理中”拦截。
                if operation_id:
                    op_row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if op_row:
                        merged = json_load(op_row["result_json"], {})
                        merged = (
                            merged if isinstance(merged, dict) else {}
                        )
                        merged.update(dict(operation_result or {}))
                        merged["phase"] = "committed"
                        connection.execute(
                            """
                            UPDATE operation_receipts
                            SET result_json = ?, status = 'completed',
                                updated_at = ?
                            WHERE operation_id = ?
                            """,
                            (
                                json_dump(merged),
                                utc_now(),
                                op_row["operation_id"],
                            ),
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            "system",
                            "operation.update",
                            op_row["operation_id"],
                            {"status": "completed", "phase": "committed"},
                        )
                connection.execute("COMMIT")
                result = self._session(row)
                result["player_event_id"] = player_event_id
                result["narrator_event_id"] = narrator_event_id
                result["workflow"] = workflow_result
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _participant(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        result = {
            "id": row["id"],
            "session_id": row["session_id"],
            "player_id": row["player_id"],
            "group_user_id": row["group_user_id"],
            "private_user_id": row["private_user_id"],
            "private_origin": row["private_origin"],
            "display_name": row["display_name"],
            "character_card_id": row["character_card_id"],
            "character_version_id": row["character_version_id"],
            "character_name": row["character_name"],
            "character_code": row["character_code"],
            "aliases": json_load(row["aliases_json"], []),
            "card_status": row["card_status"],
            "ready": bool(row["ready"]),
            "participation_status": row["participation_status"],
            "seat_reserved_at": row["seat_reserved_at"],
            "joined_round": row["joined_round"],
            "consecutive_timeouts": row["consecutive_timeouts"],
            "exit_reason": row["exit_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for key in (
            "draft_status",
            "draft_step",
            "draft_expires_at",
            "binding_code",
            "binding_expires_at",
            "draft_profile_json",
            "draft_template_version",
            "runtime_state_json",
            "runtime_revision",
            "card_profile_json",
            "card_stats_json",
            "card_version_no",
            "card_template_version",
            "card_version_status",
            "card_review_note",
            "card_reviewed_by",
            "card_version_created_at",
        ):
            if key in keys:
                value = row[key]
                if key == "draft_profile_json":
                    result["draft_profile"] = json_load(value, {})
                elif key == "runtime_state_json":
                    result["runtime_state"] = json_load(value, {})
                elif key == "card_profile_json":
                    result["card_profile"] = json_load(value, {})
                elif key == "card_stats_json":
                    result["card_stats"] = json_load(value, {})
                elif key == "runtime_revision":
                    result["runtime_revision"] = value
                else:
                    result[key] = value
        return result

    @staticmethod
    def _choice_set(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "participant_id": row["participant_id"],
            "round_no": row["round_no"],
            "session_revision": row["session_revision"],
            "choices": json_load(row["choices_json"], []),
            "status": row["status"],
            "reroll_count": row["reroll_count"],
            "selected_key": row["selected_key"],
            "flavor_text": row["flavor_text"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _vote(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "source_event_id": row["source_event_id"],
            "question": row["question"],
            "options": json_load(row["options_json"], []),
            "eligible_user_ids": json_load(
                row["eligible_user_ids_json"],
                [],
            ),
            "stage": row["stage"],
            "status": row["status"],
            "winner_key": row["winner_key"],
            "suspended_user_id": row["suspended_user_id"],
            "deadline_at": row["deadline_at"],
            "result": json_load(row["result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
