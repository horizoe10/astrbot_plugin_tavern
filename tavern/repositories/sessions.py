"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *


class SessionRepositoryMixin:
    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group,
            platform_id,
            group_id,
        )

    def _get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY
                    s.selected DESC,
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
            return self._session(row) if row else None

    async def get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group_ref,
            platform_id,
            group_id,
            instance_ref,
        )

    def _get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        reference = str(instance_ref or "").strip()
        if not reference:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                  AND (s.id = ? OR s.instance_slug = ?)
                LIMIT 1
                """,
                (platform_id, group_id, reference, reference.lower()),
            ).fetchone()
            return self._session(row) if row else None

    async def list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_group_sessions,
            platform_id,
            group_id,
        )

    def _list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    w.description AS world_description,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY s.selected DESC, s.updated_at DESC
                """,
                (platform_id, group_id),
            ).fetchall()
            return [self._session(row) for row in rows]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_session, session_id)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                       COALESCE(gr.remark, '') AS group_remark,
                       COALESCE(gr.revision, 1) AS group_revision,
                       COALESCE(ss.relative_path, '')
                           AS storage_relative_path,
                       COALESCE(ss.sync_status, 'pending')
                           AS storage_sync_status,
                       COALESCE(ss.last_error, '') AS storage_last_error,
                       COALESCE(ss.playthrough_no, 1) AS playthrough_no
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("会话不存在")
            return self._session(row)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._run(self._list_sessions)

    def _list_sessions(
        self,
        session_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        ids = (
            [str(item) for item in session_ids if str(item)]
            if session_ids is not None
            else None
        )
        if ids == []:
            return []
        where_clause = ""
        parameters: tuple[Any, ...] = ()
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            where_clause = f"WHERE s.id IN ({placeholders})"
            parameters = tuple(ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    COALESCE(gr.remark, '') AS group_remark,
                    COALESCE(gr.revision, 1) AS group_revision,
                    COALESCE(ss.relative_path, '')
                        AS storage_relative_path,
                    COALESCE(ss.sync_status, 'pending')
                        AS storage_sync_status,
                    COALESCE(ss.last_error, '') AS storage_last_error,
                    COALESCE(ss.playthrough_no, 1) AS playthrough_no,
                    srs.progress_json, srs.recovery_json,
                    sa.termination_type, sa.reason AS archive_reason,
                    sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                    COALESCE(sa.readonly, 0) AS readonly,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count,
                    (
                        SELECT COUNT(*) FROM participants ready_pt
                        WHERE ready_pt.session_id = s.id
                          AND ready_pt.ready = 1
                          AND ready_pt.participation_status = 'active'
                    ) AS ready_count,
                    (
                        SELECT COUNT(*) FROM memories m
                        WHERE m.session_id = s.id
                    ) AS memory_count,
                    (
                        SELECT COUNT(*) FROM snapshots sn
                        WHERE sn.session_id = s.id
                    ) AS snapshot_count,
                    (
                        SELECT COUNT(*) FROM session_characters sc
                        WHERE sc.session_id = s.id
                          AND sc.lifecycle_status = 'active'
                    ) AS npc_count,
                    (
                        SELECT COUNT(*) FROM timer_instances ti
                        WHERE ti.session_id = s.id
                          AND ti.status IN ('active', 'paused')
                    ) AS active_timer_count,
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM group_votes gv
                            WHERE gv.session_id = s.id AND gv.status = 'open'
                        ) THEN 'vote'
                        WHEN EXISTS (
                            SELECT 1 FROM choice_sets cs
                            WHERE cs.session_id = s.id AND cs.status = 'active'
                        ) THEN 'choice'
                        WHEN s.state = 'preparing' THEN 'preparation'
                        WHEN s.state = 'paused' THEN 'admin'
                        ELSE ''
                    END AS waiting_for,
                    (
                        SELECT deadline_at FROM timer_instances due
                        WHERE due.session_id = s.id
                          AND due.status = 'active'
                        ORDER BY CASE due.timer_type
                            WHEN 'vote' THEN 0
                            WHEN 'turn' THEN 1
                            ELSE 2
                        END, due.created_at DESC
                        LIMIT 1
                    ) AS active_deadline_at
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                LEFT JOIN session_rule_states srs ON srs.session_id = s.id
                LEFT JOIN session_archives sa ON sa.session_id = s.id
                {where_clause}
                ORDER BY
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                """,
                parameters,
            ).fetchall()
            return [self._session(row) for row in rows]

    async def search_sessions(
        self,
        query: str = "",
        scope: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self._run(
            self._search_sessions,
            query,
            scope,
            page,
            page_size,
        )

    def _search_sessions(
        self,
        query: str,
        scope: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "all").strip().lower()
        if normalized_scope not in {"all", "group", "story"}:
            raise ValueError("检索范围必须为 all、group 或 story")
        normalized_query = clean_text(query, max_chars=200).casefold()
        normalized_page = max(1, int(page or 1))
        normalized_page_size = min(100, max(5, int(page_size or 20)))
        group_match = """
            (
                instr(lower(COALESCE(gr.remark, '')), ?) > 0
                OR instr(lower(s.group_id), ?) > 0
                OR instr(lower(s.platform_id), ?) > 0
            )
        """
        story_match = """
            (
                instr(lower(s.instance_name), ?) > 0
                OR instr(lower(s.instance_slug), ?) > 0
                OR instr(lower(s.id), ?) > 0
                OR instr(lower(w.name), ?) > 0
                OR instr(lower(w.slug), ?) > 0
                OR EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.session_id = s.id
                      AND instr(lower(e.content), ?) > 0
                )
            )
        """
        where = ""
        parameters: list[Any] = []
        if normalized_query:
            if normalized_scope == "group":
                where = f"WHERE {group_match}"
                parameters.extend([normalized_query] * 3)
            elif normalized_scope == "story":
                where = f"WHERE {story_match}"
                parameters.extend([normalized_query] * 6)
            else:
                where = f"WHERE ({group_match} OR {story_match})"
                parameters.extend([normalized_query] * 9)
        with self._connect() as connection:
            base = f"""
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                {where}
            """
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {base}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            pages = max(
                1,
                (total + normalized_page_size - 1)
                // normalized_page_size,
            )
            effective_page = min(normalized_page, pages)
            offset = (effective_page - 1) * normalized_page_size
            ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT s.id
                    {base}
                    ORDER BY
                        CASE s.state
                            WHEN 'running' THEN 0
                            WHEN 'preparing' THEN 1
                            WHEN 'paused' THEN 2
                            WHEN 'maintenance' THEN 3
                            WHEN 'finished' THEN 5
                            ELSE 4
                        END,
                        s.updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        *parameters,
                        normalized_page_size,
                        offset,
                    ),
                ).fetchall()
            ]
            group_rows = connection.execute(
                """
                SELECT s.platform_id, s.group_id,
                       COALESCE(gr.remark, '') AS remark,
                       COALESCE(gr.revision, 1) AS revision,
                       COUNT(*) AS story_count,
                       SUM(CASE WHEN s.state = 'running' THEN 1 ELSE 0 END)
                           AS running_count
                FROM sessions s
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                GROUP BY s.platform_id, s.group_id
                ORDER BY COALESCE(NULLIF(gr.remark, ''), s.group_id)
                """
            ).fetchall()
        items = self._list_sessions(ids)
        page_keys = {
            (item["platform_id"], item["group_id"]) for item in items
        }
        groups = [
            dict(row)
            for row in group_rows
            if (str(row["platform_id"]), str(row["group_id"])) in page_keys
        ]
        return {
            "items": items,
            "groups": groups,
            "query": normalized_query,
            "scope": normalized_scope,
            "page": effective_page,
            "page_size": normalized_page_size,
            "total": total,
            "pages": pages,
        }

    async def list_session_options(self) -> list[dict[str, Any]]:
        return await self._run(self._list_session_options)

    def _list_session_options(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                {
                    "id": str(row["id"]),
                    "platform_id": str(row["platform_id"]),
                    "group_id": str(row["group_id"]),
                    "group_remark": str(row["group_remark"] or ""),
                    "instance_name": str(row["instance_name"]),
                    "instance_slug": str(row["instance_slug"]),
                    "world_name": str(row["world_name"]),
                    "state": str(row["state"]),
                }
                for row in connection.execute(
                    """
                    SELECT s.id, s.platform_id, s.group_id,
                           s.instance_name, s.instance_slug, s.state,
                           w.name AS world_name,
                           COALESCE(gr.remark, '') AS group_remark
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN group_registry gr
                      ON gr.platform_id = s.platform_id
                     AND gr.group_id = s.group_id
                    ORDER BY
                        COALESCE(NULLIF(gr.remark, ''), s.group_id),
                        s.updated_at DESC
                    """
                ).fetchall()
            ]

    async def save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        result = await self._run(
            self._save_group_remark,
            platform_id,
            group_id,
            remark,
            actor_id,
            expected_revision,
        )
        await asyncio.to_thread(
            self.storage.sync_group,
            result["platform_id"],
            result["group_id"],
        )
        return result

    def _save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台 ID",
        )
        normalized_group = validate_platform_id(group_id, label="群 ID")
        normalized_remark = clean_text(remark, max_chars=120)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    LIMIT 1
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not exists:
                    raise DatabaseNotFoundError("群会话不存在")
                row = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not row:
                    registry_id = (
                        "group_"
                        + hashlib.sha256(
                            (
                                f"{normalized_platform}\0"
                                f"{normalized_group}"
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                    )
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO group_registry(
                            id, platform_id, group_id, remark, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            registry_id,
                            normalized_platform,
                            normalized_group,
                            normalized_remark,
                            now,
                            now,
                        ),
                    )
                else:
                    if (
                        int(expected_revision or 0) > 0
                        and int(row["revision"]) != int(expected_revision)
                    ):
                        raise DatabaseConflictError(
                            "群备注已被其他管理员更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE group_registry SET
                            remark = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (normalized_remark, utc_now(), row["id"]),
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "group.remark",
                    f"{normalized_platform}:{normalized_group}",
                    {
                        "remark": normalized_remark,
                        "revision": int(updated["revision"]),
                    },
                )
                connection.execute("COMMIT")
                return dict(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_storage_info(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.storage_info,
            session_id,
        )

    async def verify_storage(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.verify_instance,
            session_id,
        )

    async def ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str = "",
        instance_name: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_session,
            platform_id,
            group_id,
            unified_origin,
            world_ref,
            actor_id,
            instance_slug,
            instance_name,
        )

    async def clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        *,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str = "",
        candidate_world_ref: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._clone_session,
            source_session_id,
            actor_id,
            instance_slug,
            instance_name,
            snapshot_ref,
            candidate_world_ref,
        )

    def _clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str,
        candidate_world_ref: str,
    ) -> dict[str, Any]:
        slug = validate_slug(instance_slug)
        name = clean_text(instance_name, max_chars=100)
        if not name:
            raise ValueError("新副本名称不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (source_session_id,),
                ).fetchone()
                if not source:
                    raise DatabaseNotFoundError("源副本不存在")
                candidate_world = None
                candidate_payload: dict[str, Any] | None = None
                if candidate_world_ref:
                    candidate_world = connection.execute(
                        """
                        SELECT * FROM worlds
                        WHERE (id=? OR slug=?) AND archived=0
                        """,
                        (candidate_world_ref, candidate_world_ref),
                    ).fetchone()
                    if not candidate_world:
                        raise DatabaseNotFoundError("候选世界包不存在或已归档")
                    source_config = connection.execute(
                        "SELECT * FROM instance_configs WHERE session_id=?",
                        (source_session_id,),
                    ).fetchone()
                    if not source_config:
                        raise DatabaseNotFoundError("源副本缺少冻结世界配置")
                    from ..world_migration import compare_world_contracts

                    candidate_payload = self._world(candidate_world)
                    migration = compare_world_contracts(
                        json_load(source_config["world_snapshot_json"], {}),
                        candidate_payload,
                    )
                    if not migration["safe_for_clone"]:
                        codes = ", ".join(
                            str(item.get("code") or "unknown")
                            for item in migration["blockers"]
                        )
                        raise DatabaseConflictError(
                            f"候选世界不能安全克隆应用：{codes}"
                        )
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND instance_slug = ?
                    """,
                    (
                        source["platform_id"],
                        source["group_id"],
                        slug,
                    ),
                ).fetchone()
                if duplicate:
                    raise DatabaseConflictError("当前群已存在同标识副本")
                snapshot = None
                if snapshot_ref:
                    snapshot = connection.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE session_id = ? AND (id = ? OR name = ?)
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (
                            source_session_id,
                            snapshot_ref,
                            snapshot_ref,
                        ),
                    ).fetchone()
                    if not snapshot:
                        raise DatabaseNotFoundError("指定的源存档不存在")
                if not snapshot:
                    archive = connection.execute(
                        """
                        SELECT final_snapshot_id FROM session_archives
                        WHERE session_id = ?
                        """,
                        (source_session_id,),
                    ).fetchone()
                    if archive:
                        snapshot = connection.execute(
                            "SELECT * FROM snapshots WHERE id = ?",
                            (archive["final_snapshot_id"],),
                        ).fetchone()
                state_json = (
                    snapshot["world_state_json"]
                    if snapshot
                    else source["world_state_json"]
                )
                turn_no = (
                    int(snapshot["turn_no"])
                    if snapshot
                    else int(source["turn_no"])
                )
                now = utc_now()
                target_id = new_id("session")
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'closed', ?, 1, ?,
                              0, ?, ?)
                    """,
                    (
                        target_id,
                        source["platform_id"],
                        source["group_id"],
                        source["unified_origin"],
                        slug,
                        name,
                        candidate_world["id"] if candidate_world else source["world_id"],
                        turn_no,
                        state_json,
                        now,
                        now,
                    ),
                )
                config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if config:
                    phase = json_load(config["phase_meta_json"], {})
                    phase = dict(phase) if isinstance(phase, Mapping) else {}
                    phase["branched_from_session_id"] = source_session_id
                    phase["branched_from_snapshot_id"] = (
                        snapshot["id"] if snapshot else ""
                    )
                    if candidate_world and candidate_payload:
                        phase["world_upgrade_from_revision"] = int(
                            config["world_revision"]
                        )
                        phase["world_upgrade_to_revision"] = int(
                            candidate_world["revision"]
                        )
                    connection.execute(
                        """
                        INSERT INTO instance_configs(
                            session_id, world_revision, world_snapshot_json,
                            time_rules_json, phase_meta_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            (
                                candidate_world["revision"]
                                if candidate_world else config["world_revision"]
                            ),
                            (
                                json_dump(candidate_payload)
                                if candidate_payload else config["world_snapshot_json"]
                            ),
                            (
                                json_dump(world_time_rules(candidate_payload))
                                if candidate_payload else config["time_rules_json"]
                            ),
                            json_dump(phase),
                            now,
                            now,
                        ),
                    )
                rules = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if rules:
                    recovery = {
                        "state": "idle",
                        "message": "",
                        "operation_id": "",
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO session_rule_states(
                            session_id, progress_json,
                            content_boundaries_json, npc_policy_json,
                            context_budget_json, dice_rules_json,
                            recovery_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            target_id,
                            rules["progress_json"],
                            rules["content_boundaries_json"],
                            rules["npc_policy_json"],
                            rules["context_budget_json"],
                            rules["dice_rules_json"],
                            json_dump(recovery),
                            now,
                            now,
                        ),
                    )
                character_ids: dict[str, str] = {}
                for item in connection.execute(
                    """
                    SELECT sc.*, st.state_json
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall():
                    target_character_id = new_id("snpc")
                    character_ids[item["id"]] = target_character_id
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                  '', ?, ?, 1, ?, ?)
                        """,
                        (
                            target_character_id,
                            target_id,
                            item["stable_key"],
                            item["name"],
                            item["aliases_json"],
                            item["role_type"],
                            item["public_profile_json"],
                            item["known_facts_json"],
                            item["misconceptions_json"],
                            item["source"],
                            item["review_status"],
                            item["lifecycle_status"],
                            item["persistent"],
                            item["first_turn"],
                            item["last_turn"],
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            target_character_id,
                            item["state_json"] or "{}",
                            now,
                        ),
                    )
                for table, prefix, columns in (
                    (
                        "story_ledger",
                        "ledger",
                        (
                            "stable_key", "kind", "title", "description",
                            "status", "visibility",
                        ),
                    ),
                    (
                        "scene_clocks",
                        "clock",
                        (
                            "stable_key", "title", "segments",
                            "current_value", "visibility", "trigger_text",
                            "status",
                        ),
                    ),
                ):
                    for item in connection.execute(
                        f"SELECT * FROM {table} WHERE session_id = ?",
                        (source_session_id,),
                    ).fetchall():
                        target_row_id = new_id(prefix)
                        if table == "story_ledger":
                            connection.execute(
                                """
                                INSERT INTO story_ledger(
                                    id, session_id, stable_key, kind, title,
                                    description, status, visibility,
                                    source_event_id, completed_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO scene_clocks(
                                    id, session_id, stable_key, title,
                                    segments, current_value, visibility,
                                    trigger_text, status, triggered_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                memory_ids: dict[str, str] = {}
                memory_rows = connection.execute(
                    """
                    SELECT m.*, mg.visibility, mg.locked, mg.pinned,
                           mg.invalidated, mg.supersedes_id,
                           mg.conflict_status, mg.note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall()
                for item in memory_rows:
                    target_memory_id = new_id("memory")
                    memory_ids[item["id"]] = target_memory_id
                    fingerprint = memory_fingerprint(
                        target_id,
                        item["scope"],
                        item["scope_id"],
                        item["kind"],
                        item["content"],
                    )
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            target_id,
                            item["scope"],
                            item["scope_id"],
                            item["kind"],
                            item["content"],
                            item["importance"],
                            item["salience"],
                            item["tags_json"],
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                for item in memory_rows:
                    target_memory_id = memory_ids[item["id"]]
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            item["visibility"] or "public",
                            int(item["locked"] or 0),
                            int(item["pinned"] or 0),
                            int(item["invalidated"] or 0),
                            memory_ids.get(item["supersedes_id"], ""),
                            item["conflict_status"] or "clear",
                            item["note"] or "",
                            actor_id,
                            now,
                        ),
                    )
                event_id = new_id("event")
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        event_id,
                        target_id,
                        turn_no,
                        actor_id,
                        f"已从副本「{source['instance_name']}」克隆分支。",
                        json_dump(
                            {
                                "kind": "session_branch",
                                "source_session_id": source_session_id,
                                "source_snapshot_id": (
                                    snapshot["id"] if snapshot else ""
                                ),
                            }
                        ),
                        now,
                    ),
                )
                target_row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (target_id,),
                ).fetchone()
                self._insert_snapshot(
                    connection,
                    target_row,
                    "branch-origin",
                    "manual",
                    actor_id,
                    replace=False,
                )
                self._insert_audit(
                    connection,
                    target_id,
                    actor_id,
                    "session.clone",
                    source_session_id,
                    {
                        "source_snapshot_id": (
                            snapshot["id"] if snapshot else ""
                        ),
                        "instance_slug": slug,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (target_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(platform_id, label="平台 ID")
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                world = connection.execute(
                    """
                    SELECT * FROM worlds
                    WHERE (id = ? OR slug = ?) AND archived = 0
                    """,
                    (world_ref, world_ref),
                ).fetchone()
                if not world:
                    raise DatabaseNotFoundError(
                        "指定世界包不存在或已归档"
                    )
                normalized_instance_slug = validate_slug(
                    instance_slug or world["slug"]
                )
                normalized_instance_name = clean_text(
                    instance_name or world["name"],
                    max_chars=100,
                )
                if not normalized_instance_name:
                    raise ValueError("副本名称不能为空")

                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.platform_id = ? AND s.group_id = ?
                      AND s.instance_slug = ?
                    """,
                    (
                        platform_id,
                        group_id,
                        normalized_instance_slug,
                    ),
                ).fetchone()
                if row:
                    if row["world_id"] != world["id"]:
                        raise DatabaseConflictError(
                            "该群中的副本标识已被其他世界使用"
                        )
                    if row["state"] == SESSION_FINISHED:
                        base = normalized_instance_slug[:45].rstrip("-_")
                        stamp = datetime.now().astimezone().strftime(
                            "%Y%m%d%H%M%S"
                        )
                        candidate = f"{base}-run-{stamp}"
                        serial = 2
                        while connection.execute(
                            """
                            SELECT 1 FROM sessions
                            WHERE platform_id = ? AND group_id = ?
                              AND instance_slug = ?
                            """,
                            (platform_id, group_id, candidate),
                        ).fetchone():
                            suffix = f"-{serial:02d}"
                            candidate = (
                                f"{base[:64 - len(suffix) - 19]}"
                                f"-run-{stamp}{suffix}"
                            )
                            serial += 1
                        normalized_instance_slug = validate_slug(candidate)
                        row = None
                if row:
                    if unified_origin and row["unified_origin"] != unified_origin:
                        connection.execute(
                            """
                            UPDATE sessions
                            SET unified_origin = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (unified_origin, utc_now(), row["id"]),
                        )
                        row = connection.execute(
                            """
                            SELECT s.*, w.name AS world_name,
                                   w.slug AS world_slug
                            FROM sessions s
                            JOIN worlds w ON w.id = s.world_id
                            WHERE s.id = ?
                            """,
                            (row["id"],),
                        ).fetchone()
                    connection.execute("COMMIT")
                    return self._session(row)

                has_selected = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ? AND selected = 1
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                session_id = new_id("session")
                now = utc_now()
                initial_state = public_world_state(
                    json_load(world["initial_state_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'closed', 0, 1, ?, 0, ?, ?
                    )
                    """,
                    (
                        session_id,
                        platform_id,
                        group_id,
                        unified_origin,
                        normalized_instance_slug,
                        normalized_instance_name,
                        0 if has_selected else 1,
                        world["id"],
                        json_dump(initial_state),
                        now,
                        now,
                    ),
                )
                world_payload = {
                    "id": world["id"],
                    "slug": world["slug"],
                    "name": world["name"],
                    "description": world["description"],
                    "system_prompt": world["system_prompt"],
                    "rules": json_load(world["rules_json"], {}),
                    "opening_scene": world["opening_scene"],
                    "initial_state": json_load(
                        world["initial_state_json"],
                        {},
                    ),
                    "revision": world["revision"],
                }
                connection.execute(
                    """
                    INSERT INTO instance_configs(
                        session_id, world_revision, world_snapshot_json,
                        time_rules_json, phase_meta_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        world["revision"],
                        json_dump(world_payload),
                        json_dump(world_time_rules(world_payload)),
                        json_dump({"resume_mode": False}),
                        now,
                        now,
                    ),
                )
                self._initialize_current_rows(connection)
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.create",
                    session_id,
                    {
                        "platform_id": platform_id,
                        "group_id": group_id,
                        "world_id": world["id"],
                        "instance_slug": normalized_instance_slug,
                        "instance_name": normalized_instance_name,
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

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._transition_session,
            session_id,
            target_state,
            actor_id,
            world_ref,
        )

    def _transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str,
    ) -> dict[str, Any]:
        if target_state not in SESSION_STATES:
            raise ValueError("非法会话状态")
        if target_state == SESSION_FINISHED:
            raise InvalidTransitionError(
                "完结必须使用原子归档流程，不能直接切换状态"
            )
        allowed = {
            SESSION_CLOSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_MAINTENANCE,
            },
            SESSION_PREPARING: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
            SESSION_RUNNING: {
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_PAUSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_FINISHED: set(),
            SESSION_MAINTENANCE: {
                SESSION_PREPARING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                archived = connection.execute(
                    "SELECT 1 FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if archived or current["state"] == SESSION_FINISHED:
                    raise InvalidTransitionError(
                        "该副本已经永久归档；如需继续，请从最终存档克隆新副本"
                    )
                if target_state not in allowed[current["state"]]:
                    raise InvalidTransitionError(
                        f"不能从 {current['state']} 切换为 {target_state}"
                    )

                world_id = current["world_id"]
                world_state_json = current["world_state_json"]
                turn_no = current["turn_no"]
                history_floor_seq = current["history_floor_seq"]
                switched_world = False
                if world_ref:
                    world = connection.execute(
                        """
                        SELECT * FROM worlds
                        WHERE (id = ? OR slug = ?) AND archived = 0
                        """,
                        (world_ref, world_ref),
                    ).fetchone()
                    if not world:
                        raise DatabaseNotFoundError("世界包不存在或已归档")
                    if world["id"] != world_id:
                        if current["state"] != SESSION_CLOSED:
                            raise InvalidTransitionError(
                                "只有关闭状态才能更换世界包"
                            )
                        world_id = world["id"]
                        world_state_json = json_dump(
                            public_world_state(
                                json_load(world["initial_state_json"], {})
                            )
                        )
                        turn_no = 0
                        max_seq = connection.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0)
                            FROM events WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()[0]
                        history_floor_seq = max_seq + 1
                        switched_world = True
                        world_payload = {
                            "id": world["id"],
                            "slug": world["slug"],
                            "name": world["name"],
                            "description": world["description"],
                            "system_prompt": world["system_prompt"],
                            "rules": json_load(world["rules_json"], {}),
                            "opening_scene": world["opening_scene"],
                            "initial_state": json_load(
                                world["initial_state_json"],
                                {},
                            ),
                            "revision": world["revision"],
                        }
                        connection.execute(
                            """
                            UPDATE instance_configs SET
                                world_revision = ?,
                                world_snapshot_json = ?,
                                time_rules_json = ?,
                                phase_meta_json = ?,
                                updated_at = ?
                            WHERE session_id = ?
                            """,
                            (
                                world["revision"],
                                json_dump(world_payload),
                                json_dump(world_time_rules(world_payload)),
                                json_dump({"resume_mode": False}),
                                utc_now(),
                                session_id,
                            ),
                        )

                now = utc_now()
                selected = int(current["selected"])
                auto_paused: list[str] = []
                if target_state in {
                    SESSION_PREPARING,
                    SESSION_RUNNING,
                }:
                    running_rows = connection.execute(
                        """
                        SELECT id FROM sessions
                        WHERE platform_id = ? AND group_id = ?
                          AND state = 'running' AND id <> ?
                        """,
                        (
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    ).fetchall()
                    auto_paused = [str(row["id"]) for row in running_rows]
                    connection.execute(
                        """
                        UPDATE sessions SET
                            state = CASE
                                WHEN state = 'running' THEN 'paused'
                                ELSE state
                            END,
                            selected = 0,
                            revision = CASE
                                WHEN state = 'running' THEN revision + 1
                                ELSE revision
                            END,
                            updated_at = CASE
                                WHEN state = 'running' THEN ?
                                ELSE updated_at
                            END
                        WHERE platform_id = ? AND group_id = ? AND id <> ?
                        """,
                        (
                            now,
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    )
                    selected = 1
                if target_state == SESSION_PREPARING:
                    connection.execute(
                        """
                        UPDATE participants
                        SET ready = 0, updated_at = ?
                        WHERE session_id = ?
                          AND participation_status IN (
                              'reserved', 'active', 'standby', 'away'
                          )
                        """,
                        (now, session_id),
                    )
                    config_row = connection.execute(
                        """
                        SELECT phase_meta_json, time_rules_json
                        FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    phase_meta = json_load(
                        config_row["phase_meta_json"] if config_row else "",
                        {},
                    )
                    phase_meta["resume_mode"] = bool(turn_no)
                    phase_meta["entered_preparing_at"] = now
                    connection.execute(
                        """
                        UPDATE instance_configs
                        SET phase_meta_json = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (json_dump(phase_meta), now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'preparation'
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    time_rules = normalize_time_rules(
                        json_load(
                            config_row["time_rules_json"]
                            if config_row
                            else "",
                            {},
                        )
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id="",
                        timer_type="preparation",
                        timeout_seconds=time_rules[
                            "preparation_timeout_seconds"
                        ],
                        reminder_seconds=None,
                        action={"resume_mode": bool(turn_no)},
                    )
                if target_state in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE group_votes
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'open'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id = ?, state = ?, selected = ?, turn_no = ?,
                        world_state_json = ?, history_floor_seq = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        world_id,
                        target_state,
                        selected,
                        turn_no,
                        world_state_json,
                        history_floor_seq,
                        now,
                        session_id,
                    ),
                )
                detail = {
                    "from": current["state"],
                    "to": target_state,
                    "world_changed": switched_world,
                    "auto_paused_instances": auto_paused,
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.transition",
                    session_id,
                    detail,
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

    async def finalize_session(
        self,
        session_id: str,
        actor_id: str,
        *,
        termination_type: str = "completed",
        reason: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._finalize_session,
            session_id,
            actor_id,
            termination_type,
            reason,
        )

    async def delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        result = await self._run(
            self._delete_session,
            session_id,
            actor_id,
            confirm_name,
        )
        try:
            trashed = await asyncio.to_thread(
                self.storage.trash_relative_path,
                str(result.get("relative_path") or ""),
                label=str(result.get("instance_slug") or "story"),
            )
            result["trash_path"] = str(trashed or "")
        except Exception as exc:
            result["trash_error"] = str(exc)[:500]
        return result

    def _delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT s.*, ss.relative_path
                    FROM sessions s
                    LEFT JOIN story_storage ss ON ss.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在")
                if row["state"] not in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    raise InvalidTransitionError(
                        "只能删除已关闭或已归档的副本"
                    )
                if str(confirm_name or "").strip() != str(
                    row["instance_name"]
                ):
                    raise ValueError("确认名称与副本名称不一致")
                detail = {
                    "instance_name": row["instance_name"],
                    "instance_slug": row["instance_slug"],
                    "relative_path": str(row["relative_path"] or ""),
                    "state": row["state"],
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.delete",
                    session_id,
                    detail,
                )
                connection.execute(
                    """
                    DELETE FROM token_quota_policies
                    WHERE scope_type = 'session' AND scope_id = ?
                    """,
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM sessions WHERE id = ?",
                    (session_id,),
                )
                replacement = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND state <> 'finished'
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (row["platform_id"], row["group_id"]),
                ).fetchone()
                if replacement:
                    connection.execute(
                        """
                        UPDATE sessions SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (
                            replacement["id"],
                            row["platform_id"],
                            row["group_id"],
                        ),
                    )
                connection.execute("COMMIT")
                return {
                    "deleted": True,
                    "session_id": session_id,
                    **detail,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _finalize_session(
        self,
        session_id: str,
        actor_id: str,
        termination_type: str,
        reason: str,
    ) -> dict[str, Any]:
        termination_type = str(termination_type or "").strip().lower()
        if termination_type not in {"completed", "aborted"}:
            raise ValueError("结束类型必须为 completed 或 aborted")
        reason = clean_text(reason, max_chars=1000)
        if termination_type == "aborted" and not reason:
            raise ValueError("强制终止必须填写原因")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                existing = connection.execute(
                    "SELECT * FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if existing or session["state"] == SESSION_FINISHED:
                    raise InvalidTransitionError("该副本已经永久归档")

                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    ) AND status = 'active'
                    """,
                    (now, session_id),
                )
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                final_snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    f"final-{termination_type}-{stamp}",
                    "final",
                    actor_id,
                    replace=False,
                )
                ending_text = (
                    "故事抵达了已经确认的结局，副本进入永久归档。"
                    if termination_type == "completed"
                    else f"副本由管理员强制终止并永久归档。原因：{reason}"
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id,
                        actor_name, content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        new_id("event"),
                        session_id,
                        session["turn_no"],
                        actor_id,
                        ending_text,
                        json_dump(
                            {
                                "kind": "session_finalized",
                                "termination_type": termination_type,
                                "reason": reason,
                                "final_snapshot_id": final_snapshot_id,
                            }
                        ),
                        now,
                    ),
                )
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
                    UPDATE timer_instances SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ? AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE delegation_grants SET status = 'revoked',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    "DELETE FROM permission_grants WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    """
                    UPDATE return_requests SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ?
                      AND status NOT IN ('completed', 'rejected', 'cancelled')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                )
                recovery_row = connection.execute(
                    """
                    SELECT recovery_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                recovery = json_load(
                    recovery_row["recovery_json"] if recovery_row else "",
                    {},
                )
                recovery = (
                    dict(recovery) if isinstance(recovery, Mapping) else {}
                )
                recovery.update(
                    {
                        "state": "archived",
                        "message": ending_text,
                        "operation_id": final_snapshot_id,
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
                    UPDATE sessions SET state = 'finished', selected = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    INSERT INTO session_archives(
                        session_id, termination_type, reason,
                        final_snapshot_id, ended_by, ended_at, readonly
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        session_id,
                        termination_type,
                        reason,
                        final_snapshot_id,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.finish"
                    if termination_type == "completed"
                    else "session.abort",
                    session_id,
                    {
                        "termination_type": termination_type,
                        "reason": reason,
                        "final_snapshot_id": final_snapshot_id,
                        "readonly": True,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                           srs.progress_json, srs.recovery_json,
                           sa.termination_type,
                           sa.reason AS archive_reason,
                           sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                           sa.readonly
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN session_rule_states srs
                      ON srs.session_id = s.id
                    JOIN session_archives sa ON sa.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def save_manual_state(
        self,
        session_id: str,
        state: Mapping[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_manual_state,
            session_id,
            dict(state),
            expected_revision,
            actor_id,
        )

    def _save_manual_state(
        self,
        session_id: str,
        state: dict[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                if current["revision"] != expected_revision:
                    raise DatabaseConflictError("会话状态已改变，请刷新后重试")
                self._insert_snapshot(
                    connection,
                    current,
                    f"manual-before-edit-{current['revision']}",
                    "safety",
                    actor_id,
                    replace=True,
                )
                stored_state = json_load(current["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(
                    public_world_state(state),
                    turn_state,
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.state_edit",
                    session_id,
                    {"previous_revision": current["revision"]},
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

    async def ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_player,
            session_id,
            user_id,
            display_name,
        )

    def _ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO players(
                    id, session_id, user_id, display_name, character_name,
                    profile_json, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                ON CONFLICT(session_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id("player"),
                    session_id,
                    user_id,
                    display_name,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            return self._player(row)

    async def list_players(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_players, session_id)

    def _list_players(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ?
                ORDER BY enabled DESC, updated_at DESC
                """,
                (session_id,),
            ).fetchall()
            return [self._player(row) for row in rows]

    @staticmethod
    def _turn_status_for(
        connection: sqlite3.Connection,
        session_id: str,
        stored_world_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stored_world_state is None:
            session = connection.execute(
                "SELECT world_state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            stored_world_state = json_load(session["world_state_json"], {})

        rows = connection.execute(
            """
            SELECT * FROM players
            WHERE session_id = ? AND enabled = 1
            """,
            (session_id,),
        ).fetchall()
        players = {str(row["user_id"]): row for row in rows}
        state = turn_state_from_world(
            stored_world_state,
            allowed_user_ids=players,
        )
        order = []
        for position, user_id in enumerate(state["order"], start=1):
            row = players[user_id]
            order.append(
                {
                    "position": position,
                    "player_id": row["id"],
                    "user_id": user_id,
                    "display_name": row["display_name"],
                    "character_name": row["character_name"],
                    "name": row["character_name"] or row["display_name"],
                }
            )
        current = next(
            (
                item
                for item in order
                if item["user_id"] == state["current_user_id"]
            ),
            None,
        )
        return {
            "round_no": state["round_no"],
            "current_user_id": state["current_user_id"],
            "current_name": current["name"] if current else "",
            "order": order,
        }

    async def get_turn_status(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_turn_status, session_id)

    def _get_turn_status(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._turn_status_for(connection, session_id)

    async def join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._join_turn_order,
            session_id,
            user_id,
            display_name,
            actor_id,
        )

    def _join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name, character_name,
                        profile_json, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("player"),
                        session_id,
                        user_id,
                        display_name,
                        now,
                        now,
                    ),
                )
                player_row = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player_row["enabled"]:
                    raise InvalidTransitionError("你的玩家身份当前不可用")

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
                turn_state, joined = join_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), now, session_id),
                    )
                if joined:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.join",
                        user_id,
                        {"position": turn_state["order"].index(user_id) + 1},
                    )
                session_row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {
                    "joined": joined,
                    "player": self._player(player_row),
                    "session": self._session(session_row),
                    "turn": status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._leave_turn_order,
            session_id,
            user_id,
            actor_id,
        )

    def _leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
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
                turn_state, removed = leave_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), utc_now(), session_id),
                    )
                if removed:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.leave",
                        user_id,
                        {},
                    )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {"removed": removed, "turn": status}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._skip_turn,
            session_id,
            requester_id,
            actor_id,
            force,
        )

    def _skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        force: bool,
    ) -> dict[str, Any]:
        requester_id = validate_platform_id(requester_id, label="用户 ID")
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
                current_user_id = turn_state["current_user_id"]
                if not current_user_id:
                    raise InvalidTransitionError("回合队列为空")
                if not force and requester_id != current_user_id:
                    current = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    raise InvalidTransitionError(
                        f"当前轮到 {current['current_name'] or current_user_id}"
                    )
                turn_state = advance_turn(turn_state, current_user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.force_skip" if force else "turn_order.skip",
                    current_user_id,
                    {"round_no": turn_state["round_no"]},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_turn_order(
        self,
        session_id: str,
        order: Sequence[str],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_turn_order,
            session_id,
            list(order),
            actor_id,
        )

    def _set_turn_order(
        self,
        session_id: str,
        order: list[str],
        actor_id: str,
    ) -> dict[str, Any]:
        if len(order) > 100:
            raise ValueError("回合队列最多 100 人")
        normalized_order = [
            validate_platform_id(item, label="用户 ID") for item in order
        ]
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
                unknown = [
                    item for item in normalized_order if item not in enabled_ids
                ]
                if unknown:
                    raise ValueError("回合顺序包含不存在或已停用的玩家")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state = replace_turn_order(
                    turn_state,
                    normalized_order,
                )
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.set",
                    session_id,
                    {"order": normalized_order},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def designate_turn(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._designate_turn,
            session_id,
            user_id,
            actor_id,
        )

    def _designate_turn(
        self,
        session_id: str,
        user_id: str,
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
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                      AND participation_status = 'active'
                      AND card_status = 'approved'
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise ValueError("指定角色当前不在有效行动阵容中")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                if user_id not in turn_state["order"]:
                    raise ValueError("指定角色当前不在回合队列中")
                turn_state["current_user_id"] = user_id
                now = utc_now()
                new_revision = int(session["revision"]) + 1
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(embed_turn_state(stored_state, turn_state)),
                        now,
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'turn'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                choice_id = new_id("choices")
                choices = fallback_choices(stored_state)
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status, reroll_count,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                    """,
                    (
                        choice_id,
                        session_id,
                        participant["id"],
                        turn_state["round_no"],
                        new_revision,
                        json_dump(choices),
                        f"designate:{session_id}:{new_revision}",
                        now,
                        now,
                    ),
                )
                config = connection.execute(
                    """
                    SELECT time_rules_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                rules = normalize_time_rules(
                    json_load(config["time_rules_json"] if config else "", {})
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant["id"],
                    timer_type="turn",
                    timeout_seconds=rules["turn_timeout_seconds"],
                    reminder_seconds=rules["turn_reminder_seconds"],
                    action={
                        "choice_set_id": choice_id,
                        "user_id": user_id,
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn.designate",
                    participant["id"],
                    {"user_id": user_id},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    embed_turn_state(stored_state, turn_state),
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _remove_turn_member(
        connection: sqlite3.Connection,
        session_id: str,
        user_id: str,
        *,
        updated_at: str,
    ) -> bool:
        session = connection.execute(
            "SELECT world_state_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise DatabaseNotFoundError("会话不存在")
        stored_state = json_load(session["world_state_json"], {})
        turn_state, removed = leave_turn(
            turn_state_from_world(stored_state),
            user_id,
        )
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
        turn_state = normalize_turn_state(
            turn_state,
            allowed_user_ids=enabled_ids,
        )
        updated_state = embed_turn_state(stored_state, turn_state)
        if json_dump(updated_state) != json_dump(stored_state):
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (json_dump(updated_state), updated_at, session_id),
            )
        return removed

    async def save_player(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_player, dict(payload), actor_id)

    def _save_player(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        user_id = validate_platform_id(
            payload.get("user_id"),
            label="用户 ID",
        )
        display_name = clean_text(
            payload.get("display_name"),
            max_chars=100,
        )
        if not display_name:
            raise ValueError("显示名称不能为空")
        character_name = clean_text(
            payload.get("character_name"),
            max_chars=100,
        )
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("玩家资料必须是 JSON 对象")
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, character_name = ?,
                            profile_json = ?, enabled = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            existing["id"],
                        ),
                    )
                    # A16.3：玩家资料里的角色名/显示名同步到 participants，
                    # 避免「回合与行动者(players)」与「阵容/行动卡(participants)」名字不一致。
                    if character_name:
                        connection.execute(
                            """
                            UPDATE participants SET
                                character_name = ?, display_name = ?,
                                updated_at = ?
                            WHERE session_id = ? AND group_user_id = ?
                            """,
                            (character_name, display_name, now, session_id, user_id),
                        )
                    player_id = existing["id"]
                    action = "player.update"
                else:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            now,
                        ),
                    )
                    action = "player.create"
                if not enabled:
                    self._remove_turn_member(
                        connection,
                        session_id,
                        user_id,
                        updated_at=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    player_id,
                    {"user_id": user_id, "display_name": display_name},
                )
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._player(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_player(
        self,
        player_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_player, player_id, actor_id)

    def _delete_player(self, player_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM players WHERE id = ?",
                    (player_id,),
                )
                self._remove_turn_member(
                    connection,
                    row["session_id"],
                    row["user_id"],
                    updated_at=utc_now(),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "player.delete",
                    player_id,
                    {"user_id": row["user_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
