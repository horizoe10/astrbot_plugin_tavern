"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..resolution_receipts import content_hash
from ..rule_runtime import RuleRuntime


class RuleRepositoryMixin:
    async def resolve_action_intent(
        self,
        session_id: str,
        intent: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = True,
        operation_id: str = "",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._resolve_action_intent,
            session_id,
            dict(intent),
            dict(context or {}),
            bool(dry_run),
            operation_id,
            actor_id,
        )

    def _resolve_action_intent(
        self,
        session_id: str,
        intent: dict[str, Any],
        context: dict[str, Any],
        dry_run: bool,
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240) or new_id("op")
        request_hash = content_hash(
            {"session_id": session_id, "intent": intent, "context": context}
        )
        with self._connect() as connection:
            if not dry_run:
                connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM operation_commits WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing:
                    if existing["input_hash"] != request_hash:
                        raise DatabaseConflictError("操作 ID 已被不同请求使用")
                    result = json_load(existing["result_json"], {})
                    if not dry_run:
                        connection.execute("COMMIT")
                    return result

                row = connection.execute(
                    """
                    SELECT s.*, ic.world_snapshot_json, ic.world_revision
                    FROM sessions s
                    JOIN instance_configs ic ON ic.session_id=s.id
                    WHERE s.id=?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在或没有冻结世界配置")
                if not dry_run:
                    self._assert_session_writable(connection, session_id)
                world = json_load(row["world_snapshot_json"], {})
                actor_ref = str(intent.get("actor_ref") or "")
                capability_rows = connection.execute(
                    """
                    SELECT * FROM actor_capability_instances
                    WHERE session_id=? AND actor_ref=? AND available=1
                    ORDER BY created_at ASC
                    """,
                    (session_id, actor_ref),
                ).fetchall()
                participant_id = (
                    actor_ref.split(":", 1)[1]
                    if actor_ref.startswith("character:") else ""
                )
                actor_state_row = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id=? AND participant_id=?
                    """,
                    (session_id, participant_id),
                ).fetchone() if participant_id else None
                actor_runtime_state = json_load(
                    actor_state_row["state_json"] if actor_state_row else "{}", {}
                )
                actor_runtime_state = (
                    dict(actor_runtime_state)
                    if isinstance(actor_runtime_state, Mapping) else {}
                )
                actor_context = context.get("actor")
                actor_context = dict(actor_context) if isinstance(actor_context, Mapping) else {}
                runtime_refs = actor_runtime_state.get("refs", {})
                runtime_refs = runtime_refs if isinstance(runtime_refs, Mapping) else {}
                provided_refs = actor_context.get("refs", {})
                provided_refs = provided_refs if isinstance(provided_refs, Mapping) else {}
                actor_context["refs"] = {**runtime_refs, **provided_refs}
                if "capabilities" not in actor_context:
                    actor_context["capabilities"] = [
                        {
                            "instance_id": cap["id"],
                            "capability_ref": cap["capability_ref"],
                            "source_ref": cap["source_ref"],
                            "available": bool(cap["available"]),
                            **json_load(cap["state_json"], {}),
                        }
                        for cap in capability_rows
                    ]
                current_state = json_load(row["world_state_json"], {})
                run_context = {
                    **context,
                    "actor": actor_context,
                    "session": {
                        "refs": {
                            "custom:session_state": row["state"],
                            "counter:turn": int(row["turn_no"]),
                        }
                    },
                    "state": {
                        "world": current_state,
                        "actor": actor_runtime_state,
                    },
                }
                snapshot = connection.execute(
                    """
                    SELECT id FROM world_snapshots
                    WHERE world_id=? AND world_revision=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (row["world_id"], int(row["world_revision"])),
                ).fetchone()
                snapshot_id = str(snapshot["id"] if snapshot else "")
                runtime = RuleRuntime(world)
                result = runtime.resolve_action_intent(
                    intent, run_context, operation_id=operation_id,
                    world_snapshot_id=snapshot_id, dry_run=dry_run,
                )
                if dry_run:
                    return result

                now = utc_now()
                resolved_state = result["state"]
                resolved_state = resolved_state if isinstance(resolved_state, Mapping) else {}
                resolved_world_state = resolved_state.get("world", current_state)
                resolved_actor_state = resolved_state.get("actor", actor_runtime_state)
                connection.execute(
                    """
                    UPDATE sessions SET world_state_json=?, revision=revision+1,
                        updated_at=? WHERE id=?
                    """,
                    (json_dump(resolved_world_state), now, session_id),
                )
                if actor_state_row:
                    connection.execute(
                        """
                        UPDATE character_runtime_states
                        SET state_json=?, revision=revision+1, updated_at=?
                        WHERE id=?
                        """,
                        (json_dump(resolved_actor_state), now, actor_state_row["id"]),
                    )
                for operation in result.get("planned_operations", []):
                    if not isinstance(operation, Mapping):
                        continue
                    op = str(operation.get("op") or "")
                    target_ref = str(operation.get("target_ref") or "")
                    if target_ref.startswith("capability:") and op == "grant_reference":
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO actor_capability_instances(
                                id, session_id, actor_ref, capability_ref,
                                definition_version, source_ref, state_json,
                                persistence_scope, available, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 1, ?, '{}', ?, 1, ?, ?)
                            """,
                            (
                                new_id("capability_instance"), session_id, actor_ref,
                                target_ref, str(operation.get("source_ref") or "rule"),
                                str(operation.get("persistence_scope") or "campaign"), now, now,
                            ),
                        )
                    elif target_ref.startswith("capability:") and op == "revoke_reference":
                        connection.execute(
                            """
                            UPDATE actor_capability_instances SET available=0, updated_at=?
                            WHERE session_id=? AND actor_ref=? AND capability_ref=?
                            """,
                            (now, session_id, actor_ref, target_ref),
                        )
                receipt = result["receipt"]
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
                    """,
                    (
                        operation_id, session_id, request_hash, json_dump(result),
                        json_dump({
                            "world_state": current_state,
                            "actor_state": actor_runtime_state,
                            "session_revision": int(row["revision"]),
                        }),
                        now, now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO resolution_receipts(
                        receipt_id, operation_id, session_id, world_snapshot_id,
                        content_hash, receipt_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt["receipt_id"], operation_id, session_id, snapshot_id,
                        receipt["content_hash"], json_dump(receipt),
                        json_dump({"outcome_id": receipt["outcome_id"],
                                   "narrative_projection": receipt["narrative_projection"]}), now,
                    ),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "rules.action_commit",
                    operation_id, {"receipt_id": receipt["receipt_id"]},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                if not dry_run:
                    connection.execute("ROLLBACK")
                raise

    async def get_resolution_receipt(
        self, receipt_id: str, *, public_only: bool = False
    ) -> dict[str, Any]:
        return await self._run(
            self._get_resolution_receipt, receipt_id, bool(public_only)
        )

    def _get_resolution_receipt(
        self, receipt_id: str, public_only: bool
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("裁定凭证不存在")
            return json_load(
                row["public_projection_json"] if public_only else row["receipt_json"], {}
            )

    async def list_actor_capabilities(
        self, session_id: str, actor_ref: str, *, available_only: bool = True
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_actor_capabilities,
            session_id,
            actor_ref,
            bool(available_only),
        )

    def _list_actor_capabilities(
        self, session_id: str, actor_ref: str, available_only: bool
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actor_capability_instances
                WHERE session_id=? AND actor_ref=?
                  AND (?=0 OR available=1)
                ORDER BY created_at ASC, capability_ref ASC
                """,
                (session_id, actor_ref, int(available_only)),
            ).fetchall()
            return [
                {
                    "instance_id": row["id"],
                    "capability_ref": row["capability_ref"],
                    "definition_version": row["definition_version"],
                    "source_ref": row["source_ref"],
                    "state": json_load(row["state_json"], {}),
                    "persistence_scope": row["persistence_scope"],
                    "available": bool(row["available"]),
                }
                for row in rows
            ]

    async def get_instance_config(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_instance_config, session_id)

    def _get_instance_config(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本配置不存在")
            world_snapshot = json_load(
                row["world_snapshot_json"],
                {},
            )
            return {
                "session_id": row["session_id"],
                "world_revision": row["world_revision"],
                "world_snapshot": world_snapshot,
                "character_card_template": card_template(world_snapshot),
                "time_rules": normalize_time_rules(
                    json_load(row["time_rules_json"], {})
                ),
                "phase_meta": json_load(row["phase_meta_json"], {}),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def save_instance_time_rules(
        self,
        session_id: str,
        rules: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_time_rules(rules)
        await self._run(
            self._save_instance_time_rules,
            session_id,
            normalized,
            actor_id,
        )
        return await self.get_instance_config(session_id)

    def _save_instance_time_rules(
        self,
        session_id: str,
        rules: dict[str, Any],
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM instance_configs WHERE session_id = ?",
                    (session_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("副本配置不存在")
                self._assert_session_writable(connection, session_id)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET time_rules_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(rules), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timing.rules_update",
                    session_id,
                    {"rules": rules},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._get_session_archive, session_id)

    def _get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_archives WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "session_id": row["session_id"],
                "termination_type": row["termination_type"],
                "reason": row["reason"],
                "final_snapshot_id": row["final_snapshot_id"],
                "ended_by": row["ended_by"],
                "ended_at": row["ended_at"],
                "readonly": bool(row["readonly"]),
            }

    async def get_session_rule_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_session_rule_state, session_id)

    def _get_session_rule_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_rule_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                self._initialize_current_rows(connection)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本规则状态不存在")
            return {
                "session_id": row["session_id"],
                "progress": normalize_progress(
                    json_load(row["progress_json"], {})
                ),
                "content_boundaries": json_load(
                    row["content_boundaries_json"],
                    {},
                ),
                "npc_policy": json_load(row["npc_policy_json"], {}),
                "context_budget": json_load(
                    row["context_budget_json"],
                    {},
                ),
                "dice_rules": json_load(row["dice_rules_json"], {}),
                "recovery": json_load(row["recovery_json"], {}),
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def save_session_rule_state(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_rule_state,
            session_id,
            dict(payload),
            actor_id,
        )

    def _save_session_rule_state(
        self,
        session_id: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    self._initialize_current_rows(connection)
                    row = connection.execute(
                        "SELECT * FROM session_rule_states WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本规则状态不存在")
                expected = payload.get("revision")
                if expected not in {None, ""} and int(expected) != int(
                    row["revision"]
                ):
                    raise DatabaseConflictError("副本规则状态已被其他操作更新")
                progress = (
                    normalize_progress(payload["progress"])
                    if "progress" in payload
                    else normalize_progress(json_load(row["progress_json"], {}))
                )
                boundaries = (
                    dict(payload["content_boundaries"])
                    if isinstance(payload.get("content_boundaries"), Mapping)
                    else json_load(row["content_boundaries_json"], {})
                )
                npc_policy = (
                    dict(payload["npc_policy"])
                    if isinstance(payload.get("npc_policy"), Mapping)
                    else json_load(row["npc_policy_json"], {})
                )
                npc_policy["max_new_per_turn"] = bounded_int(
                    npc_policy.get("max_new_per_turn"),
                    3,
                    0,
                    3,
                )
                context_budget = (
                    dict(payload["context_budget"])
                    if isinstance(payload.get("context_budget"), Mapping)
                    else json_load(row["context_budget_json"], {})
                )
                dice_rules = (
                    dict(payload["dice_rules"])
                    if isinstance(payload.get("dice_rules"), Mapping)
                    else json_load(row["dice_rules_json"], {})
                )
                visibility = str(
                    dice_rules.get("visibility") or "public"
                ).lower()
                dice_rules["visibility"] = (
                    visibility
                    if visibility in {"public", "immersive", "hidden"}
                    else "public"
                )
                recovery = (
                    dict(payload["recovery"])
                    if isinstance(payload.get("recovery"), Mapping)
                    else json_load(row["recovery_json"], {})
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE session_rule_states SET
                        progress_json = ?, content_boundaries_json = ?,
                        npc_policy_json = ?, context_budget_json = ?,
                        dice_rules_json = ?, recovery_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json_dump(progress),
                        json_dump(boundaries),
                        json_dump(npc_policy),
                        json_dump(context_budget),
                        json_dump(dice_rules),
                        json_dump(recovery),
                        now,
                        session_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.rules.update",
                    session_id,
                    {
                        "progress": progress,
                        "npc_policy": npc_policy,
                        "dice_visibility": dice_rules["visibility"],
                    },
                )
                connection.execute("COMMIT")
                return self._get_session_rule_state(session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_session_characters(
        self,
        session_id: str,
        *,
        include_archived: bool = True,
        context_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_session_characters,
            session_id,
            include_archived,
            context_only,
        )

    def _list_session_characters(
        self,
        session_id: str,
        include_archived: bool,
        context_only: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            limit = 500
            if context_only:
                rules = connection.execute(
                    """
                    SELECT context_budget_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                budget = json_load(
                    rules["context_budget_json"] if rules else "",
                    {},
                )
                limit = bounded_int(budget.get("active_npcs"), 6, 0, 40)
            clauses = ["sc.session_id = ?"]
            params: list[Any] = [session_id]
            if not include_archived or context_only:
                clauses.append("sc.lifecycle_status = 'active'")
            if context_only:
                clauses.append("sc.review_status <> 'rejected'")
            rows = connection.execute(
                f"""
                SELECT sc.*, st.state_json,
                       st.revision AS state_revision
                FROM session_characters sc
                LEFT JOIN session_character_states st
                  ON st.character_id = sc.id
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE sc.source WHEN 'world_preset' THEN 0 ELSE 1 END,
                    sc.last_turn DESC, sc.updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._session_character(row) for row in rows]

    async def save_session_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_character,
            dict(payload),
            actor_id,
        )

    def _save_session_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        character_id = str(payload.get("id") or "").strip()
        name = clean_text(payload.get("name"), max_chars=80)
        if not session_id or not name:
            raise ValueError("副本 ID 与 NPC 名称不能为空")
        aliases = [
            clean_text(item, max_chars=80)
            for item in (
                payload.get("aliases")
                if isinstance(payload.get("aliases"), list)
                else []
            )[:12]
            if clean_text(item, max_chars=80)
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current = (
                    connection.execute(
                        "SELECT * FROM session_characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if character_id
                    else None
                )
                if character_id and not current:
                    raise DatabaseNotFoundError("副本 NPC 不存在")
                lowered_names = {
                    self._stable_key(name),
                    *(self._stable_key(item) for item in aliases),
                }
                candidates = connection.execute(
                    """
                    SELECT id, name, aliases_json FROM session_characters
                    WHERE session_id = ? AND id <> ?
                      AND lifecycle_status <> 'archived'
                    """,
                    (session_id, character_id),
                ).fetchall()
                for candidate in candidates:
                    candidate_names = {
                        self._stable_key(candidate["name"]),
                        *(
                            self._stable_key(item)
                            for item in json_load(
                                candidate["aliases_json"],
                                [],
                            )
                        ),
                    }
                    if lowered_names & candidate_names:
                        raise DatabaseConflictError(
                            f"NPC 名称或别名与「{candidate['name']}」重复"
                        )
                now = utc_now()
                profile = (
                    dict(payload.get("public_profile"))
                    if isinstance(payload.get("public_profile"), Mapping)
                    else {}
                )
                known_facts = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("known_facts")
                        if isinstance(payload.get("known_facts"), list)
                        else []
                    )[:30]
                    if clean_text(item, max_chars=400)
                ]
                misconceptions = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("misconceptions")
                        if isinstance(payload.get("misconceptions"), list)
                        else []
                    )[:20]
                    if clean_text(item, max_chars=400)
                ]
                state = (
                    dict(payload.get("state"))
                    if isinstance(payload.get("state"), Mapping)
                    else {}
                )
                role_type = clean_text(
                    payload.get("role_type") or "npc",
                    max_chars=40,
                )
                review_status = str(
                    payload.get("review_status") or "approved"
                ).lower()
                if review_status not in {
                    "pending",
                    "approved",
                    "rejected",
                    "duplicate",
                }:
                    review_status = "approved"
                lifecycle_status = str(
                    payload.get("lifecycle_status") or "active"
                ).lower()
                if lifecycle_status not in {
                    "active",
                    "departed",
                    "dead",
                    "archived",
                }:
                    lifecycle_status = "active"
                if current:
                    expected = payload.get("revision")
                    if expected not in {None, ""} and int(expected) != int(
                        current["revision"]
                    ):
                        raise DatabaseConflictError("NPC 已被其他操作更新")
                    connection.execute(
                        """
                        UPDATE session_characters SET
                            name = ?, aliases_json = ?, role_type = ?,
                            public_profile_json = ?, known_facts_json = ?,
                            misconceptions_json = ?, review_status = ?,
                            lifecycle_status = ?, persistent = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            character_id,
                        ),
                    )
                    action = "session_npc.update"
                else:
                    character_id = new_id("snpc")
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_turn,
                            last_turn, revision, created_at, updated_at
                        ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?,
                                 ?, ?, turn_no, turn_no, 1, ?, ?
                          FROM sessions WHERE id = ?
                        """,
                        (
                            character_id,
                            session_id,
                            f"admin:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            now,
                            session_id,
                        ),
                    )
                    action = "session_npc.create"
                connection.execute(
                    """
                    INSERT INTO session_character_states(
                        character_id, state_json, revision, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(character_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (character_id, json_dump(state), now),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    character_id,
                    {"name": name, "review_status": review_status},
                )
                row = connection.execute(
                    """
                    SELECT sc.*, st.state_json,
                           st.revision AS state_revision
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.id = ?
                    """,
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session_character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_story_ledger(
        self,
        session_id: str,
        *,
        include_host: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_story_ledger,
            session_id,
            include_host,
        )

    def _list_story_ledger(
        self,
        session_id: str,
        include_host: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM story_ledger
                WHERE session_id = ?
                  AND (? = 1 OR visibility = 'public')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    CASE kind WHEN 'main' THEN 0 WHEN 'objective' THEN 1
                              WHEN 'side' THEN 2 ELSE 3 END,
                    updated_at DESC
                """,
                (session_id, int(include_host)),
            ).fetchall()
            return [self._ledger_entry(row) for row in rows]

    async def list_scene_clocks(
        self,
        session_id: str,
        *,
        include_hidden: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_scene_clocks,
            session_id,
            include_hidden,
        )

    def _list_scene_clocks(
        self,
        session_id: str,
        include_hidden: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scene_clocks
                WHERE session_id = ?
                  AND (? = 1 OR visibility <> 'hidden')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    updated_at DESC
                """,
                (session_id, int(include_hidden)),
            ).fetchall()
            return [self._scene_clock(row) for row in rows]

    async def advance_scene_clock(
        self,
        session_id: str,
        clock_id: str,
        segments: int,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        """推进场景时钟（A14 通用接口）：校验、落库、审计。"""
        return await self._run(
            self._advance_scene_clock,
            session_id,
            clock_id,
            segments,
            actor_id,
            note,
        )

    def _advance_scene_clock(
        self,
        session_id: str,
        clock_id: str,
        segments: int,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        segments = max(-9999, min(9999, int(segments)))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM scene_clocks WHERE id = ? AND session_id = ?",
                    (clock_id, session_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("场景时钟不存在")
                current = int(row["current_value"]) + segments
                cap = int(row["segments"])
                if current < 0:
                    current = 0
                completed = current >= cap
                if completed:
                    current = cap
                connection.execute(
                    "UPDATE scene_clocks SET current_value = ?, updated_at = ? WHERE id = ?",
                    (current, now, clock_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "story.clock.advance",
                    clock_id,
                    {
                        "delta": segments,
                        "current": current,
                        "completed": completed,
                        "note": str(note)[:200],
                    },
                )
                connection.execute("COMMIT")
                return {
                    "id": clock_id,
                    "session_id": session_id,
                    "title": row["title"],
                    "current_value": current,
                    "segments": cap,
                    "completed": completed,
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._inspiration_status,
            session_id,
            user_id,
        )

    def _inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, crs.state_json
                FROM participants pt
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("当前玩家没有副本角色")
            state = json_load(row["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            return {
                "participant_id": row["participant_id"],
                "character_name": (
                    row["character_name"] or row["display_name"]
                ),
                "balance": bounded_int(
                    state.get("inspiration"),
                    1,
                    0,
                    3,
                ),
                "maximum": bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                ),
            }

    async def check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        *,
        proposed_advantages: Sequence[str] = (),
        proposed_disadvantages: Sequence[str] = (),
        locked_advantages: Sequence[str] = (),
        locked_disadvantages: Sequence[str] = (),
    ) -> dict[str, Any]:
        return await self._run(
            self._check_context,
            session_id,
            user_id,
            stat,
            list(proposed_advantages),
            list(proposed_disadvantages),
            list(locked_advantages),
            list(locked_disadvantages),
        )

    def _check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        proposed_advantages: list[str],
        proposed_disadvantages: list[str],
        locked_advantages: list[str],
        locked_disadvantages: list[str],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, ccv.profile_json,
                       crs.state_json, s.world_state_json
                FROM participants pt
                JOIN sessions s ON s.id = pt.session_id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError(
                    "当前玩家没有有效角色卡，无法取得权威检定修正"
                )
            profile = json_load(row["profile_json"], {})
            runtime = json_load(row["state_json"], {})
            world_state = json_load(row["world_state_json"], {})
            profile = profile if isinstance(profile, Mapping) else {}
            runtime = runtime if isinstance(runtime, Mapping) else {}
            world_state = (
                world_state if isinstance(world_state, Mapping) else {}
            )
            allowed_advantages: set[str] = set()
            allowed_disadvantages: set[str] = set()
            raw_specialties = profile.get("specialties")
            specialties = (
                raw_specialties
                if isinstance(raw_specialties, list)
                else str(raw_specialties or "").replace("，", ",").split(",")
            )
            for specialty in specialties:
                text = clean_text(specialty, max_chars=80)
                if text:
                    allowed_advantages.add(f"专长：{text}")
            for source in runtime.get("advantage_sources", []):
                text = clean_text(source, max_chars=120)
                if text:
                    allowed_advantages.add(text)
            for status in runtime.get("statuses", []):
                if not isinstance(status, Mapping):
                    continue
                affects = status.get("affects")
                affects = affects if isinstance(affects, list) else []
                reference = str(stat or "").casefold()
                if affects and not any(
                    reference in str(item).casefold()
                    or str(item).casefold() in reference
                    for item in affects
                ):
                    continue
                name = clean_text(status.get("name"), max_chars=100)
                if name:
                    allowed_disadvantages.add(f"状态：{name}")
            modifiers = world_state.get("check_modifiers")
            if isinstance(modifiers, Mapping):
                for source in modifiers.get("advantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_advantages.add(text)
                for source in modifiers.get("disadvantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_disadvantages.add(text)

            # Option generation may disclose an environmental or prepared
            # source before the check. It is accepted only when the source can
            # be matched to an already persisted character/scene fact; the
            # model cannot manufacture a bonus merely by writing it twice.
            trusted_texts: list[str] = []

            def collect_trusted(value: Any) -> None:
                if isinstance(value, Mapping):
                    for nested in value.values():
                        collect_trusted(nested)
                elif isinstance(value, Sequence) and not isinstance(
                    value,
                    (str, bytes),
                ):
                    for nested in value:
                        collect_trusted(nested)
                else:
                    text = truncate_text(value, max_chars=500)
                    if len(text) >= 2:
                        trusted_texts.append(text.casefold())

            collect_trusted(profile)
            collect_trusted(runtime)
            collect_trusted(
                {
                    "location": world_state.get("location"),
                    "time": world_state.get("time"),
                    "scene_summary": world_state.get("scene_summary"),
                    "facts": world_state.get("facts"),
                    "inventory": world_state.get("inventory"),
                    "check_modifiers": world_state.get("check_modifiers"),
                }
            )

            def source_is_proven(source: str) -> bool:
                probe = source
                for prefix in (
                    "专长：",
                    "装备：",
                    "情报：",
                    "环境：",
                    "准备：",
                    "状态：",
                ):
                    if probe.startswith(prefix):
                        probe = probe[len(prefix) :]
                        break
                normalized = " ".join(probe.casefold().split())
                if len(normalized) < 2:
                    return False
                return any(
                    normalized in fact
                    or (
                        len(fact) >= 4
                        and len(normalized) >= 4
                        and fact in normalized
                    )
                    for fact in trusted_texts
                )

            for raw in locked_advantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_advantages.add(source)
            for raw in locked_disadvantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_disadvantages.add(source)
            assist = connection.execute(
                """
                SELECT at.*, source.character_name AS source_name,
                       source.display_name AS source_display
                FROM assist_tokens at
                JOIN participants source ON source.id = at.source_participant_id
                WHERE at.session_id = ? AND at.target_participant_id = ?
                  AND at.status = 'active'
                  AND (at.stat = '' OR lower(at.stat) = lower(?))
                ORDER BY at.created_at
                LIMIT 1
                """,
                (session_id, row["participant_id"], stat),
            ).fetchone()
            assist_token_id = ""
            if assist:
                assist_token_id = str(assist["id"])
                allowed_advantages.add(
                    "协助："
                    + str(
                        assist["source_name"]
                        or assist["source_display"]
                        or "队友"
                    )
                )
            proposed_adv = {
                clean_text(item, max_chars=120)
                for item in [*proposed_advantages, *locked_advantages]
                if clean_text(item, max_chars=120)
            }
            proposed_dis = {
                clean_text(item, max_chars=120)
                for item in [*proposed_disadvantages, *locked_disadvantages]
                if clean_text(item, max_chars=120)
            }
            advantages = sorted(
                allowed_advantages & proposed_adv
                | {
                    item
                    for item in allowed_advantages
                    if item.startswith("协助：")
                }
            )
            disadvantages = sorted(
                allowed_disadvantages & proposed_dis
                | {
                    item
                    for item in allowed_disadvantages
                    if item.startswith("状态：")
                }
            )
            return {
                "participant_id": row["participant_id"],
                "advantages": advantages[:8],
                "disadvantages": disadvantages[:8],
                "assist_token_id": assist_token_id,
                "rejected_advantages": sorted(
                    proposed_adv - allowed_advantages
                ),
                "rejected_disadvantages": sorted(
                    proposed_dis - allowed_disadvantages
                ),
            }

    async def reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_token_usage,
            session_id,
            request_type,
            provider_id,
            expected_tokens,
        )

    def _reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        expected_tokens = bounded_int(
            expected_tokens,
            1,
            1,
            10_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id, group_id FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                stale = (
                    now_dt - timedelta(minutes=15)
                ).isoformat(timespec="seconds")
                connection.execute(
                    """
                    UPDATE token_usage SET status = 'failed',
                        settled_at = ?
                    WHERE status = 'reserved' AND created_at < ?
                    """,
                    (now, stale),
                )
                policies = connection.execute(
                    """
                    SELECT * FROM token_quota_policies
                    WHERE enabled = 1 AND (
                        (scope_type = 'group' AND scope_id = ?)
                        OR (scope_type = 'session' AND scope_id = ?)
                    )
                    """,
                    (session["group_id"], session_id),
                ).fetchall()
                quota_status: list[dict[str, Any]] = []
                for policy in policies:
                    cutoff = (
                        now_dt - timedelta(
                            seconds=int(policy["window_seconds"])
                        )
                    ).isoformat(timespec="seconds")
                    if policy["scope_type"] == "group":
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE group_id = ? AND created_at >= ?
                                """,
                                (session["group_id"], cutoff),
                            ).fetchone()[0]
                        )
                    else:
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE session_id = ? AND created_at >= ?
                                """,
                                (session_id, cutoff),
                            ).fetchone()[0]
                        )
                    remaining = max(0, int(policy["token_limit"]) - used)
                    quota_status.append(
                        {
                            "scope_type": policy["scope_type"],
                            "used": used,
                            "limit": int(policy["token_limit"]),
                            "remaining": remaining,
                            "window_seconds": int(
                                policy["window_seconds"]
                            ),
                        }
                    )
                    if expected_tokens > remaining:
                        label = (
                            "群"
                            if policy["scope_type"] == "group"
                            else "副本"
                        )
                        raise ValueError(
                            f"{label} Token 限额不足：当前窗口剩余 "
                            f"{remaining}，本次最多需要 {expected_tokens}"
                        )
                usage_id = new_id("usage")
                connection.execute(
                    """
                    INSERT INTO token_usage(
                        id, session_id, group_id, request_type, provider_id,
                        reserved_tokens, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                    """,
                    (
                        usage_id,
                        session_id,
                        session["group_id"],
                        clean_text(request_type, max_chars=64),
                        clean_text(provider_id, max_chars=200),
                        expected_tokens,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "id": usage_id,
                    "session_id": session_id,
                    "group_id": session["group_id"],
                    "reserved_tokens": expected_tokens,
                    "quotas": quota_status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def settle_token_usage(
        self,
        usage_id: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        await self._run(
            self._settle_token_usage,
            usage_id,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            usage_source,
        )

    def _settle_token_usage(
        self,
        usage_id: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        input_tokens = max(0, int(input_tokens or 0))
        cached_input_tokens = max(
            0,
            min(input_tokens, int(cached_input_tokens or 0)),
        )
        output_tokens = max(0, int(output_tokens or 0))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage SET
                    input_tokens = ?, cached_input_tokens = ?,
                    output_tokens = ?, total_tokens = ?,
                    usage_source = ?, status = 'completed',
                    settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    clean_text(usage_source, max_chars=32) or "estimated",
                    utc_now(),
                    usage_id,
                ),
            )

    async def fail_token_usage(self, usage_id: str) -> None:
        await self._run(self._fail_token_usage, usage_id)

    def _fail_token_usage(self, usage_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage
                SET status = 'failed', settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (utc_now(), usage_id),
            )

    async def token_usage_summary(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._token_usage_summary, session_id)

    def _token_usage_summary(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id, group_id FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            now_dt = datetime.now(timezone.utc)

            def total(where: str, value: str, seconds: int | None) -> int:
                parameters: list[Any] = [value]
                cutoff_sql = ""
                if seconds is not None:
                    cutoff_sql = " AND created_at >= ?"
                    parameters.append(
                        (
                            now_dt - timedelta(seconds=seconds)
                        ).isoformat(timespec="seconds")
                    )
                return int(
                    connection.execute(
                        f"""
                        SELECT COALESCE(SUM(total_tokens), 0)
                        FROM token_usage
                        WHERE {where} = ? AND status = 'completed'
                        {cutoff_sql}
                        """,
                        tuple(parameters),
                    ).fetchone()[0]
                )

            policies = connection.execute(
                """
                SELECT * FROM token_quota_policies
                WHERE (scope_type = 'group' AND scope_id = ?)
                   OR (scope_type = 'session' AND scope_id = ?)
                ORDER BY scope_type
                """,
                (session["group_id"], session_id),
            ).fetchall()
            quota_items: list[dict[str, Any]] = []
            for row in policies:
                scope_column = (
                    "group_id"
                    if row["scope_type"] == "group"
                    else "session_id"
                )
                scope_value = (
                    session["group_id"]
                    if row["scope_type"] == "group"
                    else session_id
                )
                used = total(
                    scope_column,
                    str(scope_value),
                    int(row["window_seconds"]),
                )
                quota_items.append(
                    {
                        "id": row["id"],
                        "scope_type": row["scope_type"],
                        "scope_id": row["scope_id"],
                        "window_seconds": int(row["window_seconds"]),
                        "token_limit": int(row["token_limit"]),
                        "enabled": bool(row["enabled"]),
                        "used": used,
                        "remaining": max(
                            0,
                            int(row["token_limit"]) - used,
                        ),
                        "revision": int(row["revision"]),
                    }
                )
            by_type = [
                {
                    "request_type": row["request_type"],
                    "tokens": int(row["tokens"]),
                    "requests": int(row["requests"]),
                }
                for row in connection.execute(
                    """
                    SELECT request_type, SUM(total_tokens) AS tokens,
                           COUNT(*) AS requests
                    FROM token_usage
                    WHERE session_id = ? AND status = 'completed'
                    GROUP BY request_type
                    ORDER BY tokens DESC
                    """,
                    (session_id,),
                ).fetchall()
            ]
            return {
                "session_id": session_id,
                "group_id": session["group_id"],
                "session": {
                    "hour": total("session_id", session_id, 3600),
                    "day": total("session_id", session_id, 86400),
                    "all": total("session_id", session_id, None),
                },
                "group": {
                    "hour": total(
                        "group_id",
                        str(session["group_id"]),
                        3600,
                    ),
                    "day": total(
                        "group_id",
                        str(session["group_id"]),
                        86400,
                    ),
                    "all": total(
                        "group_id",
                        str(session["group_id"]),
                        None,
                    ),
                },
                "quotas": quota_items,
                "by_type": by_type,
            }

    async def group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._group_token_usage_summary,
            platform_id,
            group_id,
        )

    def _group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id FROM sessions
                WHERE platform_id = ? AND group_id = ?
                ORDER BY selected DESC, updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        if not session:
            raise DatabaseNotFoundError("群会话不存在")
        usage = self._token_usage_summary(str(session["id"]))
        group_quota = next(
            (
                item
                for item in usage["quotas"]
                if item["scope_type"] == "group"
            ),
            None,
        )
        return {
            "platform_id": platform_id,
            "group_id": group_id,
            "session_id": str(session["id"]),
            "group": usage["group"],
            "quota": group_quota,
        }

    async def set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_token_quota,
            session_id,
            scope_type,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
        )

    def _set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        scope_type = str(scope_type or "").strip().lower()
        if scope_type not in {"group", "session"}:
            raise ValueError("限额范围必须为群或副本")
        window_seconds = bounded_int(
            window_seconds,
            3600,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            100_000,
            1,
            1_000_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT group_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                scope_id = (
                    str(session["group_id"])
                    if scope_type == "group"
                    else session_id
                )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        scope_type,
                        scope_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.quota",
                    scope_type,
                    {
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._token_usage_summary(session_id)

    async def set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_group_token_quota,
            platform_id,
            group_id,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
        )

    def _set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        window_seconds = bounded_int(
            window_seconds,
            86_400,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            500_000,
            1,
            1_000_000_000,
        )
        session_id = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    ORDER BY selected DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("群会话不存在")
                session_id = str(session["id"])
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, 'group', ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        group_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.group_quota",
                    group_id,
                    {
                        "platform_id": platform_id,
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._group_token_usage_summary(platform_id, group_id)

    async def ensure_default_token_quota(
        self,
        session_id: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        """为尚未配置任何配额策略的副本播种默认策略（v0.12.0）。

        只在该副本（及其所属群）都不存在任何已启用策略时写入默认值；
        已有策略时保持原样返回，绝不覆盖运行时的显式配置。
        返回当前配额摘要（与 ``set_token_quota`` 一致）。
        """
        return await self._run(
            self._ensure_default_token_quota,
            session_id,
            int(window_seconds),
            int(token_limit),
            bool(enabled),
            actor_id,
        )

    def _ensure_default_token_quota(
        self,
        session_id: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        window_seconds = bounded_int(
            window_seconds,
            86_400,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            500_000,
            1,
            1_000_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT id, group_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                existing = connection.execute(
                    """
                    SELECT 1 FROM token_quota_policies
                    WHERE enabled = 1 AND (
                        (scope_type = 'group' AND scope_id = ?)
                        OR (scope_type = 'session' AND scope_id = ?)
                    )
                    LIMIT 1
                    """,
                    (session["group_id"], session_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._token_usage_summary(session_id)
                if not enabled:
                    connection.execute("COMMIT")
                    return self._token_usage_summary(session_id)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, 'session', ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        new_id("quota"),
                        session_id,
                        window_seconds,
                        token_limit,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.quota_default",
                    session_id,
                    {
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._token_usage_summary(session_id)

    async def record_provider_result(
        self,
        provider_id: str,
        *,
        success: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._record_provider_result,
            provider_id,
            success,
            reason,
        )

    def _record_provider_result(
        self,
        provider_id: str,
        success: bool,
        reason: str,
    ) -> dict[str, Any]:
        provider_id = clean_text(provider_id, max_chars=200)
        if not provider_id:
            return {}
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            failures = 0 if success else int(
                row["consecutive_failures"] if row else 0
            ) + 1
            status = "healthy"
            circuit_until = ""
            if not success and failures >= 3:
                status = "open"
                minutes = min(60, 5 * (2 ** min(3, failures - 3)))
                circuit_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes)
                ).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider_id, status, consecutive_failures,
                    last_failure_reason, last_failure_at, last_success_at,
                    circuit_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    status = excluded.status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_failure_reason = excluded.last_failure_reason,
                    last_failure_at = excluded.last_failure_at,
                    last_success_at = excluded.last_success_at,
                    circuit_until = excluded.circuit_until,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    status,
                    failures,
                    "" if success else clean_text(reason, max_chars=500),
                    "" if success else now,
                    now if success else (row["last_success_at"] if row else ""),
                    circuit_until,
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return dict(updated)

    async def filter_healthy_providers(
        self,
        provider_ids: Sequence[str],
    ) -> list[str]:
        return await self._run(
            self._filter_healthy_providers,
            list(provider_ids),
        )

    def _filter_healthy_providers(
        self,
        provider_ids: list[str],
    ) -> list[str]:
        normalized = list(
            dict.fromkeys(
                clean_text(item, max_chars=200)
                for item in provider_ids
                if clean_text(item, max_chars=200)
            )
        )
        if not normalized:
            return []
        now = datetime.now(timezone.utc)
        result: list[str] = []
        blocked: list[str] = []
        with self._connect() as connection:
            for provider_id in normalized:
                row = connection.execute(
                    "SELECT * FROM provider_health WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
                if not row or row["status"] != "open":
                    result.append(provider_id)
                    continue
                try:
                    until = datetime.fromisoformat(row["circuit_until"])
                except (TypeError, ValueError):
                    until = now
                if until <= now:
                    connection.execute(
                        """
                        UPDATE provider_health
                        SET status = 'half_open', updated_at = ?
                        WHERE provider_id = ?
                        """,
                        (utc_now(), provider_id),
                    )
                    result.append(provider_id)
                else:
                    blocked.append(provider_id)
        return result or blocked[:1]

    async def list_provider_health(self) -> list[dict[str, Any]]:
        return await self._run(self._list_provider_health)

    def _list_provider_health(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM provider_health
                    ORDER BY
                        CASE status WHEN 'open' THEN 0
                                    WHEN 'half_open' THEN 1 ELSE 2 END,
                        updated_at DESC
                    """
                ).fetchall()
            ]

    async def record_configuration_revision(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_configuration_revision,
            dict(payload),
            actor_id,
        )

    def _record_configuration_revision(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        encoded = json_dump(payload)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO configuration_revisions(
                    fingerprint, payload_json, saved_by, saved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (fingerprint, encoded, actor_id, now),
            )
            row = connection.execute(
                """
                SELECT * FROM configuration_revisions
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            latest = connection.execute(
                """
                SELECT MAX(id) AS latest_id FROM configuration_revisions
                """
            ).fetchone()
            return {
                "revision": row["id"],
                "latest_revision": latest["latest_id"] or row["id"],
                "fingerprint": fingerprint,
                "saved_by": row["saved_by"],
                "saved_at": row["saved_at"],
                "current": row["id"] == latest["latest_id"],
            }

    async def get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_operation_receipt,
            operation_id,
        )

    def _get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE operation_id = ? AND status = 'completed'
                """,
                (clean_text(operation_id, max_chars=240),),
            ).fetchone()
            if not row:
                return None
            return {
                "operation_id": row["operation_id"],
                "session_id": row["session_id"],
                "operation_type": row["operation_type"],
                "request": json_load(row["request_json"], {}),
                "result": json_load(row["result_json"], {}),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def reserve_operation(
        self,
        operation_id: str,
        session_id: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_operation,
            operation_id,
            session_id,
            operation_type,
            dict(request_payload),
        )

    def _reserve_operation(
        self,
        operation_id: str,
        session_id: str,
        operation_type: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240)
        operation_type = clean_text(operation_type, max_chars=80)
        if not operation_id or not session_id or not operation_type:
            raise ValueError("事务 ID、副本 ID 与类型不能为空")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                created = False
                if not row:
                    connection.execute(
                        """
                        INSERT INTO operation_receipts(
                            operation_id, session_id, operation_type,
                            request_json, result_json, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            operation_id,
                            session_id,
                            operation_type,
                            json_dump(request_payload),
                            json_dump({"phase": "reserved"}),
                            now,
                            now,
                        ),
                    )
                    created = True
                    row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                elif (
                    row["session_id"] != session_id
                    or row["operation_type"] != operation_type
                    or json_load(row["request_json"], {}) != request_payload
                ):
                    raise DatabaseConflictError("事务 ID 已被不同请求占用")
                connection.execute("COMMIT")
                return {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": json_load(row["result_json"], {}),
                    "status": row["status"],
                    "created": created,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def update_operation(
        self,
        operation_id: str,
        *,
        status: str = "pending",
        phase: str = "",
        result: Mapping[str, Any] | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        return await self._run(
            self._update_operation,
            operation_id,
            status,
            phase,
            dict(result or {}),
            actor_id,
        )

    def _update_operation(
        self,
        operation_id: str,
        status: str,
        phase: str,
        result: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        status = str(status or "pending").lower()
        if status not in {"pending", "completed", "failed"}:
            raise ValueError("事务状态无效")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (clean_text(operation_id, max_chars=240),),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("事务不存在")
                merged = json_load(row["result_json"], {})
                merged = merged if isinstance(merged, dict) else {}
                merged.update(result)
                if phase:
                    merged["phase"] = clean_text(phase, max_chars=80)
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json = ?, status = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (json_dump(merged), status, now, row["operation_id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "operation.update",
                    row["operation_id"],
                    {"status": status, "phase": phase},
                )
                connection.execute("COMMIT")
                return {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": merged,
                    "status": status,
                    "created_at": row["created_at"],
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_session_operations(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_session_operations, session_id, limit)

    def _list_session_operations(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            return [
                {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": json_load(row["result_json"], {}),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    async def revoke_operation_receipt(
        self,
        operation_id: str,
    ) -> bool:
        """0.11.3：作废一个已锁定的操作回执（如本轮未提交时的骰值）。

        幂等：回执不存在时返回 False，不抛错。
        """
        return await self._run(
            self._revoke_operation_receipt,
            operation_id,
        )

    def _revoke_operation_receipt(
        self,
        operation_id: str,
    ) -> bool:
        operation_id = clean_text(operation_id, max_chars=240)
        if not operation_id:
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
                connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._lock_check_result,
            operation_id,
            session_id,
            dict(request_payload),
            dict(result_payload),
        )

    def _lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240)
        if not operation_id or not session_id:
            raise ValueError("检定操作 ID 与副本 ID 不能为空")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM operation_receipts
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["session_id"] != session_id
                        or existing["operation_type"] != "dice_check"
                    ):
                        raise DatabaseConflictError("检定操作 ID 已被其他请求使用")
                    connection.execute("COMMIT")
                    return {
                        "operation_id": existing["operation_id"],
                        "session_id": existing["session_id"],
                        "operation_type": existing["operation_type"],
                        "request": json_load(existing["request_json"], {}),
                        "result": json_load(existing["result_json"], {}),
                        "status": existing["status"],
                        "created_at": existing["created_at"],
                        "updated_at": existing["updated_at"],
                    }
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, created_at,
                        updated_at
                    ) VALUES (?, ?, 'dice_check', ?, ?, 'completed', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        json_dump(result_payload),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "operation_type": "dice_check",
                    "request": request_payload,
                    "result": result_payload,
                    "status": "completed",
                    "created_at": now,
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
