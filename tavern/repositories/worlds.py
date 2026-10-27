"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..entity_registry import EntityRegistry
from ..resolution_receipts import content_hash
from ..rule_runtime import enabled_feature_versions


class WorldRepositoryMixin:
    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_worlds, include_archived)

    def _list_worlds(self, include_archived: bool) -> list[dict[str, Any]]:
        with self._connect() as connection:
            condition = "" if include_archived else "WHERE archived = 0"
            rows = connection.execute(
                f"""
                SELECT * FROM worlds
                {condition}
                ORDER BY archived ASC, sort_order ASC, display_no ASC
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._world(row)
                count = connection.execute(
                    "SELECT COUNT(*) FROM characters WHERE world_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                item["character_count"] = count
                result.append(item)
            return result

    async def get_world(self, world_ref: str) -> dict[str, Any]:
        return await self._run(self._get_world, world_ref)

    def _get_world(self, world_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worlds WHERE id = ? OR slug = ?",
                (world_ref, world_ref),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("世界包不存在")
            world = self._world(row)
            character_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order ASC, name COLLATE NOCASE
                """,
                (row["id"],),
            ).fetchall()
            world["characters"] = [
                self._character(character) for character in character_rows
            ]
            return world

    async def save_world(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_world, dict(payload), actor_id)

    async def set_world_sort_order(
        self,
        world_id: str,
        sort_order: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_world_sort_order, world_id, int(sort_order), actor_id
        )

    def _set_world_sort_order(
        self, world_id: str, sort_order: int, actor_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    "UPDATE worlds SET sort_order=? WHERE id=?",
                    (max(1, sort_order), world_id),
                )
                self._insert_audit(
                    connection, "", actor_id, "world.reorder", world_id,
                    {"display_no": current["display_no"], "sort_order": max(1, sort_order)},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _allocate_world_display_no(connection: sqlite3.Connection) -> int:
        maximum = int(connection.execute(
            "SELECT COALESCE(MAX(display_no), 0) + 1 AS value FROM worlds"
        ).fetchone()["value"])
        row = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='next_world_display_no'"
        ).fetchone()
        try:
            counter = int(row["value"]) if row else 1
        except (TypeError, ValueError):
            counter = 1
        value = max(maximum, counter)
        connection.execute(
            """
            INSERT INTO tavern_meta(key, value) VALUES ('next_world_display_no', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(value + 1),),
        )
        return value

    def _persist_world_revision(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        world: Mapping[str, Any],
        now: str,
    ) -> None:
        revision = int(row["revision"])
        snapshot_hash = content_hash(world)
        connection.execute(
            """
            INSERT OR IGNORE INTO world_rule_revisions(
                id, world_id, world_revision, content_hash, rules_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_rule"), row["id"], revision, snapshot_hash,
                json_dump(world.get("rules") or {}), now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO world_snapshots(
                id, world_id, world_revision, content_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_snapshot"), row["id"], revision, snapshot_hash,
                json_dump(dict(world)), now,
            ),
        )
        features = enabled_feature_versions(world)
        required = {
            str(item).split("@", 1)[0]
            for item in world.get("required_features", [])
            if isinstance(item, str)
        }
        for feature, version in features.items():
            connection.execute(
                """
                INSERT OR REPLACE INTO world_feature_versions(
                    world_id, world_revision, feature_name, feature_version,
                    required, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], revision, feature, version, int(feature in required), now),
            )
        try:
            registry = EntityRegistry(world)
        except Exception:
            registry = None
        if registry is not None:
            for item in registry.export():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO world_entity_registry(
                        world_id, world_revision, entity_ref, entity_type,
                        label, definition_json, content_hash, visibility, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], revision, item["ref"], item["entity_type"],
                        item["label"], json_dump(item["definition"]),
                        content_hash(item["definition"]),
                        str(item["definition"].get("visibility") or "world"), now,
                    ),
                )

    def _save_world(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        world_id = str(payload.get("id") or "").strip()
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=400)
        if not name:
            raise ValueError("世界名称不能为空")
        description = clean_text(payload.get("description"), max_chars=20000)
        system_prompt = clean_text(
            payload.get("system_prompt"),
            max_chars=200000,
        )
        if not system_prompt:
            raise ValueError("世界设定不能为空")
        opening_scene = clean_text(
            payload.get("opening_scene"),
            max_chars=50000,
        )
        rules = payload.get("rules")
        initial_state = payload.get("initial_state")
        if not isinstance(rules, Mapping):
            raise ValueError("规则必须是 JSON 对象")
        if not isinstance(initial_state, Mapping):
            raise ValueError("初始状态必须是 JSON 对象")
        rules = dict(rules)
        if "world_schema_version" in payload:
            rules["world_schema_version"] = payload["world_schema_version"]
        if "capabilities" in payload:
            rules["capabilities"] = payload["capabilities"]
        validate_world_contract({**payload, "rules": rules})
        known_fields = {
            "id", "slug", "name", "description", "system_prompt", "rules",
            "display_no", "sort_order",
            "opening_scene", "initial_state", "archived", "revision",
            "created_at", "updated_at", "world_schema_version", "capabilities",
            "player_limits", "card_template", "time_rules", "choice_mode",
            "check_density",
        }
        provided_extensions = {
            str(key): value
            for key, value in payload.items()
            if key not in known_fields
        }
        if "character_card" in rules:
            raw_card = rules["character_card"]
            if isinstance(raw_card, Mapping) and "fields" not in raw_card:
                rules["character_card"] = card_template({"rules": rules})
            validate_card_template_config(rules["character_card"])
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if world_id:
                    current = connection.execute(
                        "SELECT * FROM worlds WHERE id = ?",
                        (world_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("世界包不存在")
                    current_extensions = json_load(
                        current["extensions_json"],
                        {},
                    )
                    extensions = (
                        {**dict(current_extensions), **provided_extensions}
                        if isinstance(current_extensions, Mapping)
                        else dict(provided_extensions)
                    )
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "世界包已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?, extensions_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            archived = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            slug,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(extensions),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            (
                                int(bool(payload["archived"]))
                                if "archived" in payload
                                else current["archived"]
                            ),
                            now,
                            world_id,
                        ),
                    )
                    action = "world.update"
                else:
                    world_id = new_id("world")
                    display_no = self._allocate_world_display_no(connection)
                    connection.execute(
                        """
                        INSERT INTO worlds(
                            id, slug, display_no, sort_order, name, description, system_prompt,
                            rules_json, extensions_json, opening_scene, initial_state_json,
                            archived, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                        """,
                        (
                            world_id,
                            slug,
                            display_no,
                            display_no,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(provided_extensions),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            now,
                            now,
                        ),
                    )
                    action = "world.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    world_id,
                    {"slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                self._persist_world_revision(
                    connection, row, self._world(row), now
                )
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _commit_vnext_workflow(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        next_turn_state: Mapping[str, Any],
        player_user_id: str,
        player_event_id: str,
        narrator_event_id: str,
        world_state: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        workflow: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Persist choices, votes, rolls, events and timers in the turn TX."""

        result: dict[str, Any] = {}
        if not workflow:
            return result

        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
            """,
            (session["id"], player_user_id),
        ).fetchone()
        if not participant:
            raise InvalidTransitionError("当前玩家没有有效的副本参与记录")

        choice_set_id = str(workflow.get("choice_set_id") or "")
        selected_key = str(workflow.get("selected_key") or "").upper()
        flavor_text = clean_text(
            workflow.get("flavor_text"),
            max_chars=160,
        )
        if not choice_set_id or selected_key not in CHOICE_KEYS:
            raise ValueError("缺少有效的选项提交信息")
        choice_row = connection.execute(
            """
            SELECT * FROM choice_sets
            WHERE id = ? AND session_id = ? AND status = 'active'
            """,
            (choice_set_id, session["id"]),
        ).fetchone()
        if not choice_row:
            raise DatabaseConflictError("当前选项已经失效，请重新查看回合")
        if choice_row["participant_id"] != participant["id"]:
            raise PermissionError("该选项不属于当前玩家")
        if int(choice_row["session_revision"]) != int(session["revision"]):
            raise DatabaseConflictError("场景已变化，旧选项不能继续使用")
        choices = normalize_choices(json_load(choice_row["choices_json"], []))
        selected = next(
            item for item in choices if item["key"] == selected_key
        )
        connection.execute(
            """
            UPDATE choice_sets SET
                status = 'selected', selected_key = ?,
                flavor_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (selected_key, flavor_text, now, choice_set_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'completed', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'turn' AND status = 'active'
            """,
            (now, session["id"], participant["id"]),
        )
        connection.execute(
            """
            UPDATE participants
            SET consecutive_timeouts = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, participant["id"]),
        )
        result["choice"] = {
            "choice_set_id": choice_set_id,
            "key": selected_key,
            "text": selected["text"],
        }

        if check_payload:
            roll_id = new_id("roll")
            connection.execute(
                """
                INSERT INTO rolls(
                    id, session_id, choice_set_id, participant_id,
                    roll_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    roll_id,
                    session["id"],
                    choice_set_id,
                    participant["id"],
                    json_dump(dict(check_payload)),
                    now,
                ),
            )
            result["roll_id"] = roll_id

        config = connection.execute(
            """
            SELECT * FROM instance_configs WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        time_rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        round_completed = int(next_turn_state["round_no"]) > acting_round
        if round_completed:
            selected_event = self._select_world_event(
                connection,
                session_id=session["id"],
                round_no=acting_round,
                world=world,
                turn_no=new_turn,
                now=now,
            )
            if selected_event:
                result["world_event"] = selected_event

        return_progress = workflow.get("return_progress")
        if isinstance(return_progress, Mapping):
            request_id = str(return_progress.get("request_id") or "")
            evidence = clean_text(
                return_progress.get("evidence"),
                max_chars=500,
            )
            if request_id and evidence:
                progress_result = self._record_return_progress(
                    connection,
                    session_id=session["id"],
                    request_id=request_id,
                    evidence=evidence,
                    completed=bool(return_progress.get("completed", False)),
                    round_no=int(next_turn_state["round_no"]),
                    turn_no=new_turn,
                    now=now,
                )
                if progress_result:
                    result["return_progress"] = progress_result

        next_user_id = str(next_turn_state["current_user_id"] or "")
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], next_user_id),
        ).fetchone()
        if not next_participant:
            result["next_choice_set_id"] = ""
            return result

        group_decision = workflow.get("group_decision")
        if isinstance(group_decision, Mapping):
            question = clean_text(
                group_decision.get("question"),
                max_chars=500,
            )
            options = self._normalize_vote_options(
                group_decision.get("options")
            )
            if question and len(options) >= 2:
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        ORDER BY MIN(created_at)
                        """,
                        (session["id"],),
                    ).fetchall()
                ]
                vote_id = new_id("vote")
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, source_event_id, question,
                        options_json, eligible_user_ids_json, stage,
                        status, suspended_user_id, deadline_at,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session["id"],
                        narrator_event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        next_user_id,
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session["id"],
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "stage": 1},
                )
                result["vote_id"] = vote_id
                return result

        next_choices_raw = workflow.get("next_choices")
        try:
            next_choices = normalize_choices(next_choices_raw)
        except ValueError:
            next_choices = fallback_choices(world_state)
            result["choice_fallback"] = True
        choice_id = new_id("choices")
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
                session["id"],
                next_participant["id"],
                next_turn_state["round_no"],
                int(session["revision"]) + 1,
                json_dump(next_choices),
                f"turn:{session['id']}:{new_turn + 1}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=time_rules["turn_timeout_seconds"],
            reminder_seconds=time_rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": next_user_id,
            },
        )
        result["next_choice_set_id"] = choice_id
        result["next_participant_id"] = next_participant["id"]
        return result

    def _apply_v05_turn_ops(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        participant: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        source_event_id: str,
        workflow: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Apply validated v0.5 state operations inside the turn transaction."""

        result: dict[str, Any] = {
            "npc": [],
            "clocks": [],
            "ledger": [],
            "statuses": [],
            "assists": [],
        }
        session_id = str(session["id"])

        inspiration_mode = str(
            workflow.get("inspiration_mode") or ""
        ).lower()
        if inspiration_mode in {"advantage", "reroll"} and check_payload:
            runtime = connection.execute(
                """
                SELECT * FROM character_runtime_states
                WHERE session_id = ? AND participant_id = ?
                """,
                (session_id, participant["id"]),
            ).fetchone()
            if not runtime:
                raise InvalidTransitionError("角色缺少副本运行状态")
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            balance = bounded_int(state.get("inspiration"), 1, 0, 3)
            if balance < 1:
                raise InvalidTransitionError("灵感点不足，本轮没有提交")
            operation_id = (
                f"inspiration:{workflow.get('choice_set_id')}:{inspiration_mode}"
            )
            existing = connection.execute(
                """
                SELECT balance_after FROM inspiration_transactions
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if not existing:
                balance -= 1
                state["inspiration"] = balance
                state["inspiration_max"] = bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                )
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO inspiration_transactions(
                        id, session_id, participant_id, delta,
                        balance_after, reason, operation_id, created_at
                    ) VALUES (?, ?, ?, -1, ?, ?, ?, ?)
                    """,
                    (
                        new_id("inspire"),
                        session_id,
                        participant["id"],
                        balance,
                        (
                            "投骰前取得优势"
                            if inspiration_mode == "advantage"
                            else "预授权重投完整骰池"
                        ),
                        operation_id,
                        now,
                    ),
                )
            else:
                balance = int(existing["balance_after"])
            result["inspiration"] = {
                "mode": inspiration_mode,
                "balance": balance,
            }

        assist_token_id = str(
            workflow.get("assist_token_id") or ""
        ).strip()
        if assist_token_id and check_payload:
            consumed = connection.execute(
                """
                UPDATE assist_tokens SET status = 'consumed',
                    consumed_at = ?
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (now, assist_token_id, session_id),
            )
            if consumed.rowcount:
                result["consumed_assist_id"] = assist_token_id

        status_ops = workflow.get("status_ops")
        if isinstance(status_ops, Sequence) and not isinstance(
            status_ops,
            (str, bytes),
        ):
            for operation in status_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                name = clean_text(operation.get("name"), max_chars=100)
                if not target_ref or not name:
                    continue
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target:
                    continue
                runtime = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id = ? AND participant_id = ?
                    """,
                    (session_id, target["id"]),
                ).fetchone()
                if not runtime:
                    continue
                state = json_load(runtime["state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                statuses = [
                    dict(item)
                    for item in state.get("statuses", [])
                    if isinstance(item, Mapping)
                ]
                op = str(operation.get("op") or "add").lower()
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(statuses)
                        if str(item.get("name") or "").casefold()
                        == name.casefold()
                    ),
                    -1,
                )
                if op == "remove":
                    if existing_index >= 0:
                        statuses.pop(existing_index)
                else:
                    entry = {
                        "name": name,
                        "severity": str(
                            operation.get("severity") or "minor"
                        ),
                        "affects": [
                            clean_text(item, max_chars=80)
                            for item in (
                                operation.get("affects")
                                if isinstance(
                                    operation.get("affects"),
                                    list,
                                )
                                else []
                            )[:12]
                            if clean_text(item, max_chars=80)
                        ],
                        "effect": clean_text(
                            operation.get("effect"),
                            max_chars=300,
                        ),
                        "removal": clean_text(
                            operation.get("removal"),
                            max_chars=300,
                        ),
                        "source_event_id": source_event_id,
                        "created_turn": new_turn,
                    }
                    if existing_index >= 0:
                        statuses[existing_index] = entry
                    else:
                        statuses.append(entry)
                state["statuses"] = statuses[:40]
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                result["statuses"].append(
                    {
                        "target_id": target["id"],
                        "name": name,
                        "op": op,
                    }
                )

        config = connection.execute(
            """
            SELECT npc_policy_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        npc_policy = json_load(
            config["npc_policy_json"] if config else "",
            {},
        )
        max_new_npcs = bounded_int(
            npc_policy.get("max_new_per_turn"),
            3,
            0,
            3,
        )
        if not bool(npc_policy.get("enabled", True)):
            max_new_npcs = 0
        created_count = 0
        npc_ops = workflow.get("npc_ops")
        if isinstance(npc_ops, Sequence) and not isinstance(
            npc_ops,
            (str, bytes),
        ):
            for operation in npc_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "").lower()
                name = clean_text(operation.get("name"), max_chars=80)
                npc_id = clean_text(operation.get("npc_id"), max_chars=128)
                aliases = [
                    clean_text(item, max_chars=80)
                    for item in (
                        operation.get("aliases")
                        if isinstance(operation.get("aliases"), list)
                        else []
                    )[:12]
                    if clean_text(item, max_chars=80)
                ]
                npc = None
                matched_by_name = False
                if npc_id:
                    npc = connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE id = ? AND session_id = ?
                        """,
                        (npc_id, session_id),
                    ).fetchone()
                if not npc and name:
                    normalized_names = {
                        self._stable_key(name),
                        *(self._stable_key(item) for item in aliases),
                    }
                    for candidate in connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall():
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
                        if normalized_names & candidate_names:
                            npc = candidate
                            matched_by_name = True
                            break
                if op == "create" and npc and matched_by_name:
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc["id"],),
                    ).fetchone()
                    raw_duplicate_state = json_load(
                        state_row["state_json"] if state_row else "",
                        {},
                    )
                    duplicate_state = (
                        dict(raw_duplicate_state)
                        if isinstance(raw_duplicate_state, Mapping)
                        else {}
                    )
                    proposals = list(
                        duplicate_state.get("duplicate_proposals") or []
                    )
                    proposals.append(
                        {
                            "name": name,
                            "aliases": aliases,
                            "public_profile": dict(
                                operation.get("public_profile") or {}
                            ),
                            "source_event_id": source_event_id,
                            "turn_no": new_turn,
                        }
                    )
                    duplicate_state["duplicate_proposals"] = proposals[-5:]
                    connection.execute(
                        """
                        UPDATE session_characters
                        SET review_status = 'duplicate',
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, npc["id"]),
                    )
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
                        (npc["id"], json_dump(duplicate_state), now),
                    )
                    result["npc"].append(
                        {
                            "id": npc["id"],
                            "op": "duplicate_suspected",
                            "name": name,
                        }
                    )
                    continue
                if op == "create" and not npc:
                    registration_reasons = {
                        str(item)
                        for item in (
                            operation.get("registration_reasons") or []
                        )
                        if str(item)
                        in {
                            "direct_interaction",
                            "important_clue",
                            "long_term_memory",
                        }
                    }
                    if (
                        created_count >= max_new_npcs
                        or not name
                        or not bool(operation.get("persistent", True))
                        or not registration_reasons
                    ):
                        continue
                    created_count += 1
                    npc_id = new_id("snpc")
                    review_status = (
                        "pending"
                        if bool(
                            npc_policy.get(
                                "generated_requires_review",
                                True,
                            )
                        )
                        else "approved"
                    )
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'model_generated', ?, 'active', 1,
                                  ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            npc_id,
                            session_id,
                            f"generated:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            clean_text(
                                operation.get("role_type") or "npc",
                                max_chars=40,
                            ),
                            json_dump(
                                dict(operation.get("public_profile") or {})
                            ),
                            json_dump(
                                list(operation.get("known_facts") or [])[:30]
                            ),
                            json_dump(
                                list(operation.get("misconceptions") or [])[:20]
                            ),
                            review_status,
                            source_event_id,
                            source_event_id,
                            new_turn,
                            new_turn,
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
                            npc_id,
                            json_dump(
                                dict(operation.get("runtime_state") or {})
                            ),
                            now,
                        ),
                    )
                    result["npc"].append(
                        {"id": npc_id, "op": "create", "name": name}
                    )
                    continue
                if not npc:
                    continue
                npc_id = str(npc["id"])
                lifecycle_status = str(npc["lifecycle_status"])
                if op == "archive":
                    lifecycle_status = "archived"
                elif op == "depart":
                    lifecycle_status = "departed"
                elif op == "kill":
                    lifecycle_status = "dead"
                elif op in {"update", "create"}:
                    lifecycle_status = "active"
                profile = dict(
                    json_load(npc["public_profile_json"], {})
                )
                if isinstance(operation.get("public_profile"), Mapping):
                    profile.update(dict(operation["public_profile"]))
                known = list(json_load(npc["known_facts_json"], []))
                for fact in list(operation.get("known_facts") or [])[:30]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in known:
                        known.append(text)
                misconceptions = list(
                    json_load(npc["misconceptions_json"], [])
                )
                for fact in list(
                    operation.get("misconceptions") or []
                )[:20]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in misconceptions:
                        misconceptions.append(text)
                connection.execute(
                    """
                    UPDATE session_characters SET
                        public_profile_json = ?, known_facts_json = ?,
                        misconceptions_json = ?, lifecycle_status = ?,
                        last_event_id = ?, last_turn = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(profile),
                        json_dump(known[:60]),
                        json_dump(misconceptions[:40]),
                        lifecycle_status,
                        source_event_id,
                        new_turn,
                        now,
                        npc_id,
                    ),
                )
                if isinstance(operation.get("runtime_state"), Mapping):
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc_id,),
                    ).fetchone()
                    state = dict(
                        json_load(
                            state_row["state_json"] if state_row else "",
                            {},
                        )
                    )
                    state.update(dict(operation["runtime_state"]))
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
                        (npc_id, json_dump(state), now),
                    )
                result["npc"].append(
                    {"id": npc_id, "op": op, "name": npc["name"]}
                )

        ledger_ops = workflow.get("ledger_ops")
        if isinstance(ledger_ops, Sequence) and not isinstance(
            ledger_ops,
            (str, bytes),
        ):
            for operation in ledger_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "update").lower()
                entry_id = clean_text(
                    operation.get("entry_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=160)
                row = None
                if entry_id:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE id = ? AND session_id = ?
                        """,
                        (entry_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                status = {
                    "complete": "completed",
                    "fail": "failed",
                    "archive": "archived",
                }.get(op, "active")
                kind = str(operation.get("kind") or "objective").lower()
                if kind not in {
                    "main",
                    "side",
                    "objective",
                    "clue",
                    "milestone",
                    "failed",
                }:
                    kind = "objective"
                if not row and op == "create" and title:
                    entry_id = new_id("ledger")
                    connection.execute(
                        """
                        INSERT INTO story_ledger(
                            id, session_id, stable_key, kind, title,
                            description, status, visibility,
                            source_event_id, completed_event_id,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, '',
                                  1, ?, ?)
                        """,
                        (
                            entry_id,
                            session_id,
                            self._stable_key(title),
                            kind,
                            title,
                            clean_text(
                                operation.get("description"),
                                max_chars=800,
                            ),
                            (
                                "host"
                                if str(
                                    operation.get("visibility") or ""
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            source_event_id,
                            now,
                            now,
                        ),
                    )
                elif row:
                    entry_id = str(row["id"])
                    connection.execute(
                        """
                        UPDATE story_ledger SET
                            kind = ?, title = ?, description = ?,
                            status = ?, visibility = ?,
                            completed_event_id = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            kind,
                            title or row["title"],
                            clean_text(
                                operation.get("description")
                                or row["description"],
                                max_chars=800,
                            ),
                            status,
                            (
                                "host"
                                if str(
                                    operation.get("visibility")
                                    or row["visibility"]
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            (
                                source_event_id
                                if status in {"completed", "failed"}
                                else row["completed_event_id"]
                            ),
                            now,
                            entry_id,
                        ),
                    )
                else:
                    continue
                result["ledger"].append(
                    {"id": entry_id, "op": op, "status": status}
                )

        clock_ops = workflow.get("clock_ops")
        if isinstance(clock_ops, Sequence) and not isinstance(
            clock_ops,
            (str, bytes),
        ):
            for operation in clock_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "advance").lower()
                clock_id = clean_text(
                    operation.get("clock_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=100)
                row = None
                if clock_id:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE id = ? AND session_id = ?
                        """,
                        (clock_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                if not row and op == "create" and title:
                    segments = bounded_int(
                        operation.get("segments"),
                        4,
                        4,
                        8,
                    )
                    if segments not in {4, 6, 8}:
                        segments = 4
                    clock_id = new_id("clock")
                    connection.execute(
                        """
                        INSERT INTO scene_clocks(
                            id, session_id, stable_key, title, segments,
                            current_value, visibility, trigger_text, status,
                            triggered_event_id, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'active', '',
                                  1, ?, ?)
                        """,
                        (
                            clock_id,
                            session_id,
                            self._stable_key(title),
                            title,
                            segments,
                            str(operation.get("visibility") or "public"),
                            clean_text(
                                operation.get("trigger"),
                                max_chars=500,
                            ),
                            now,
                            now,
                        ),
                    )
                    current_value = 0
                    status = "active"
                elif row:
                    clock_id = str(row["id"])
                    segments = int(row["segments"])
                    current_value = int(row["current_value"])
                    if op == "advance":
                        current_value += bounded_int(
                            operation.get("delta"),
                            1,
                            -8,
                            8,
                        )
                    elif op == "set":
                        current_value = bounded_int(
                            operation.get("value"),
                            current_value,
                            0,
                            segments,
                        )
                    elif op == "complete":
                        current_value = segments
                    current_value = max(0, min(segments, current_value))
                    status = (
                        "archived"
                        if op == "archive"
                        else "completed"
                        if current_value >= segments
                        else "active"
                    )
                    triggered_event_id = str(row["triggered_event_id"] or "")
                    trigger_text = clean_text(
                        operation.get("trigger") or row["trigger_text"],
                        max_chars=500,
                    )
                    if (
                        status == "completed"
                        and not triggered_event_id
                    ):
                        triggered_event_id = new_id("event")
                        connection.execute(
                            """
                            INSERT INTO events(
                                id, session_id, turn_no, role, actor_id,
                                actor_name, content, meta_json, created_at
                            ) VALUES (?, ?, ?, 'system', 'clock',
                                      '场景时钟', ?, ?, ?)
                            """,
                            (
                                triggered_event_id,
                                session_id,
                                new_turn,
                                trigger_text
                                or f"场景时钟「{row['title']}」已填满。",
                                json_dump(
                                    {
                                        "kind": "scene_clock_trigger",
                                        "clock_id": clock_id,
                                    }
                                ),
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE scene_clocks SET
                            current_value = ?, status = ?,
                            triggered_event_id = ?, trigger_text = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            current_value,
                            status,
                            triggered_event_id,
                            trigger_text,
                            now,
                            clock_id,
                        ),
                    )
                else:
                    continue
                result["clocks"].append(
                    {
                        "id": clock_id,
                        "op": op,
                        "current_value": current_value,
                        "segments": segments,
                        "status": status,
                    }
                )

        assist_ops = workflow.get("assist_ops")
        selected_text = str(
            (workflow.get("selected_choice") or {}).get("text")
            if isinstance(workflow.get("selected_choice"), Mapping)
            else ""
        )
        if (
            isinstance(assist_ops, Sequence)
            and not isinstance(assist_ops, (str, bytes))
            and any(word in selected_text for word in ("协助", "帮助", "支援"))
        ):
            for operation in assist_ops[:1]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                method = clean_text(
                    operation.get("method"),
                    max_chars=300,
                )
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    ) LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target or not method or target["id"] == participant["id"]:
                    continue
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND target_participant_id = ?
                      AND status = 'active'
                    """,
                    (session_id, target["id"]),
                )
                token_id = new_id("assist")
                expires_round = bounded_int(
                    operation.get("expires_round"),
                    acting_round + 1,
                    acting_round,
                    acting_round + 1,
                )
                connection.execute(
                    """
                    INSERT INTO assist_tokens(
                        id, session_id, source_participant_id,
                        target_participant_id, stat, method, status,
                        expires_round, source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        token_id,
                        session_id,
                        participant["id"],
                        target["id"],
                        clean_text(operation.get("stat"), max_chars=40),
                        method,
                        expires_round,
                        source_event_id,
                        now,
                    ),
                )
                result["assists"].append(
                    {"id": token_id, "target_id": target["id"]}
                )

        connection.execute(
            """
            UPDATE assist_tokens SET status = 'expired'
            WHERE session_id = ? AND status = 'active'
              AND expires_round > 0 AND expires_round < ?
            """,
            (session_id, acting_round),
        )

        milestone = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed
            FROM story_ledger
            WHERE session_id = ? AND kind = 'milestone'
              AND status <> 'archived'
            """,
            (session_id,),
        ).fetchone()
        objective = connection.execute(
            """
            SELECT title FROM story_ledger
            WHERE session_id = ? AND status = 'active'
              AND kind IN ('main', 'objective')
            ORDER BY CASE kind WHEN 'main' THEN 0 ELSE 1 END,
                     updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        rule_row = connection.execute(
            """
            SELECT progress_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if rule_row:
            progress = normalize_progress(
                json_load(rule_row["progress_json"], {})
            )
            if int(milestone["total"] or 0) > 0:
                progress["total_milestones"] = int(milestone["total"])
                progress["completed_milestones"] = int(
                    milestone["completed"] or 0
                )
            if objective:
                progress["current_objective"] = str(objective["title"])
            connection.execute(
                """
                UPDATE session_rule_states SET progress_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (json_dump(progress), now, session_id),
            )
            result["progress"] = progress
        return result

    @staticmethod
    def _normalize_vote_options(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes),
        ):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(value[:4]):
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or CHOICE_KEYS[index]).upper()
            text = clean_text(item.get("text"), max_chars=240)
            if key not in CHOICE_KEYS or key in seen or not text:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"key": key, "text": text}
            # 0.11.4：透传「同意执行」选项上声明的检定定义，
            # 供表决通过后按该检定执行（如全队行动的 魔力 DC17）。
            if isinstance(item.get("check"), Mapping):
                entry["check"] = dict(item["check"])
            result.append(entry)
        return result

    def _select_world_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        round_no: int,
        world: Mapping[str, Any],
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        if connection.execute(
            """
            SELECT 1 FROM selected_world_events
            WHERE session_id = ? AND round_no = ?
            """,
            (session_id, round_no),
        ).fetchone():
            return None
        rules = world.get("rules")
        rules = rules if isinstance(rules, Mapping) else {}
        pool = rules.get("event_pool")
        if not isinstance(pool, Sequence) or isinstance(pool, (str, bytes)):
            return None
        session_row = connection.execute(
            """
            SELECT world_state_json FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        state = json_load(
            session_row["world_state_json"] if session_row else "",
            {},
        )
        location = str(state.get("location") or "").casefold()
        facts = {
            str(item).casefold()
            for item in (
                state.get("facts")
                if isinstance(state.get("facts"), list)
                else []
            )
        }
        active_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM participants
                WHERE session_id = ? AND participation_status = 'active'
                  AND card_status = 'approved'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        candidates: list[tuple[dict[str, Any], int]] = []
        for raw in pool[:200]:
            if not isinstance(raw, Mapping):
                continue
            item_id = clean_text(raw.get("id"), max_chars=80)
            description = clean_text(
                raw.get("description"),
                max_chars=1000,
            )
            if not item_id or not description:
                continue
            minimum_round = bounded_int(
                raw.get("minimum_round"),
                1,
                1,
                1_000_000,
            )
            if round_no < minimum_round:
                continue
            conditions = raw.get("conditions")
            conditions = (
                conditions if isinstance(conditions, Mapping) else {}
            )
            allowed_locations = conditions.get("locations")
            if isinstance(allowed_locations, Sequence) and not isinstance(
                allowed_locations,
                (str, bytes),
            ):
                normalized_locations = {
                    str(item).casefold()
                    for item in allowed_locations
                    if str(item).strip()
                }
                if normalized_locations and location not in normalized_locations:
                    continue
            required_facts = conditions.get("required_facts")
            if isinstance(required_facts, Sequence) and not isinstance(
                required_facts,
                (str, bytes),
            ):
                required = {
                    str(item).casefold()
                    for item in required_facts
                    if str(item).strip()
                }
                if not required.issubset(facts):
                    continue
            excluded_facts = conditions.get("excluded_facts")
            if isinstance(excluded_facts, Sequence) and not isinstance(
                excluded_facts,
                (str, bytes),
            ):
                excluded = {
                    str(item).casefold()
                    for item in excluded_facts
                    if str(item).strip()
                }
                if excluded.intersection(facts):
                    continue
            minimum_players = bounded_int(
                conditions.get("minimum_players"),
                0,
                0,
                32,
            )
            if active_count < minimum_players:
                continue
            maximum_players = conditions.get("maximum_players")
            if (
                maximum_players not in {None, ""}
                and active_count
                > bounded_int(maximum_players, 32, 0, 32)
            ):
                continue
            previous = connection.execute(
                """
                SELECT round_no FROM selected_world_events
                WHERE session_id = ? AND pool_item_id = ?
                ORDER BY round_no DESC LIMIT 1
                """,
                (session_id, item_id),
            ).fetchone()
            if previous and bool(raw.get("once", False)):
                continue
            cooldown = bounded_int(
                raw.get("cooldown_rounds"),
                0,
                0,
                1_000_000,
            )
            if previous and round_no - int(previous["round_no"]) <= cooldown:
                continue
            weight = bounded_int(raw.get("weight"), 1, 1, 1000)
            candidates.append((dict(raw), weight))
        if not candidates:
            return None
        total = sum(weight for _, weight in candidates)
        pick = secrets.randbelow(total)
        selected = candidates[-1][0]
        for item, weight in candidates:
            if pick < weight:
                selected = item
                break
            pick -= weight
        event_id = new_id("worldevent")
        item_id = clean_text(selected.get("id"), max_chars=80)
        description = clean_text(
            selected.get("description"),
            max_chars=1000,
        )
        payload = {
            "id": item_id,
            "title": clean_text(selected.get("title"), max_chars=120),
            "description": description,
            "severity": clean_text(
                selected.get("severity") or "standard",
                max_chars=30,
            ),
        }
        connection.execute(
            """
            INSERT INTO selected_world_events(
                id, session_id, round_no, pool_item_id, payload_json,
                status, narrative, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'narrated', ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                round_no,
                item_id,
                json_dump(payload),
                description,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'world', '世界脉冲', ?, ?, ?)
            """,
            (
                new_id("event"),
                session_id,
                turn_no,
                description,
                json_dump(
                    {
                        "kind": "world_pulse",
                        "selected_world_event_id": event_id,
                        "round_no": round_no,
                    }
                ),
                now,
            ),
        )
        return {"id": event_id, **payload}

    async def archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._archive_world, world_id, actor_id)

    def _archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                active_sessions = connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE world_id = ? AND state != 'closed'
                    """,
                    (world_id,),
                ).fetchone()[0]
                if active_sessions:
                    raise ValueError("仍有运行中的会话使用该世界，不能归档")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 1, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.archive",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._restore_world, world_id, actor_id)

    def _restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 0, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.restore",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_characters(
        self,
        world_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_characters, world_id)

    def _list_characters(self, world_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if world_id:
                rows = connection.execute(
                    """
                    SELECT * FROM characters WHERE world_id = ?
                    ORDER BY sort_order ASC, name COLLATE NOCASE
                    """,
                    (world_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM characters
                    ORDER BY world_id, sort_order ASC, name COLLATE NOCASE
                    """
                ).fetchall()
            return [self._character(row) for row in rows]

    async def save_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_character,
            dict(payload),
            actor_id,
        )

    def _save_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        character_id = str(payload.get("id") or "").strip()
        world_id = validate_platform_id(
            payload.get("world_id"),
            label="世界 ID",
        )
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=100)
        if not name:
            raise ValueError("角色名称不能为空")
        role = clean_text(payload.get("role") or "npc", max_chars=40)
        prompt = clean_text(payload.get("prompt"), max_chars=20000)
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("角色资料必须是 JSON 对象")
        try:
            sort_order = max(-10000, min(10000, int(payload.get("sort_order", 0))))
        except (TypeError, ValueError):
            sort_order = 0
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("世界包不存在")
                if character_id:
                    current = connection.execute(
                        "SELECT * FROM characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("角色不存在")
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "角色已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE characters SET
                            world_id = ?, slug = ?, name = ?, role = ?,
                            profile_json = ?, prompt = ?, enabled = ?,
                            sort_order = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            character_id,
                        ),
                    )
                    action = "character.update"
                else:
                    character_id = new_id("char")
                    connection.execute(
                        """
                        INSERT INTO characters(
                            id, world_id, slug, name, role, profile_json,
                            prompt, enabled, sort_order, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            character_id,
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            now,
                        ),
                    )
                    action = "character.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    character_id,
                    {"world_id": world_id, "slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_character, character_id, actor_id)

    def _delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色不存在")
                connection.execute(
                    "DELETE FROM characters WHERE id = ?",
                    (character_id,),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "character.delete",
                    character_id,
                    {"name": row["name"], "world_id": row["world_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
