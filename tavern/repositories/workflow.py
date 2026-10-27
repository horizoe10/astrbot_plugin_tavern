"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *


class WorkflowRepositoryMixin:
    async def opening_preflight(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._opening_preflight, session_id)

    def _opening_preflight(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            config = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not config:
                raise DatabaseNotFoundError("副本配置不存在")
            world = json_load(config["world_snapshot_json"], {})
            limits = player_limits(world)
            rows = connection.execute(
                """
                SELECT * FROM participants
                WHERE session_id = ?
                  AND participation_status IN (
                      'reserved', 'active', 'standby', 'away'
                  )
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            blockers: list[str] = []
            active: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            seen_codes: set[str] = set()
            for row in rows:
                item = self._participant(row)
                label = (
                    item["character_name"]
                    or item["display_name"]
                    or item["group_user_id"]
                )
                if item["participation_status"] == PARTICIPANT_AWAY:
                    continue
                if item["card_status"] != CARD_APPROVED:
                    status_labels = {
                        CARD_UNCREATED: "尚未建卡",
                        CARD_DRAFT: "角色卡仍是草稿",
                        CARD_PENDING: "角色卡待审核",
                        CARD_REJECTED: "角色卡未通过审核",
                    }
                    blockers.append(
                        f"{label}：{status_labels.get(item['card_status'], '角色卡无效')}"
                    )
                    continue
                if item["participation_status"] == PARTICIPANT_STANDBY:
                    continue
                if not item["ready"]:
                    blockers.append(f"{label}：尚未确认准备")
                name_key = item["character_name"].casefold()
                code_key = item["character_code"].casefold()
                if name_key in seen_names:
                    blockers.append(f"{label}：角色名重复")
                if code_key in seen_codes:
                    blockers.append(f"{label}：副本代号重复")
                seen_names.add(name_key)
                seen_codes.add(code_key)
                active.append(item)
            if len(active) < limits["minimum_start"]:
                blockers.append(
                    f"有效出场人数不足：{len(active)}/{limits['minimum_start']}"
                )
            if len(active) > limits["maximum"]:
                blockers.append(
                    f"有效出场人数超过上限：{len(active)}/{limits['maximum']}"
                )
            return {
                "ok": not blockers,
                "blockers": blockers,
                "participants": active,
                "limits": limits,
                "resume_mode": bool(session["turn_no"]),
                "state": session["state"],
            }

    async def activate_story(
        self,
        session_id: str,
        actor_id: str,
        *,
        resume: bool = False,
        choices: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        if not resume and int(session.get("turn_no") or 0) > 0:
            raise InvalidTransitionError(
                "该副本已有剧情进度，不能再次开演；请使用 /酒馆 继续"
            )
        preflight = await self.opening_preflight(session_id)
        if not preflight["ok"]:
            return {"started": False, **preflight}
        return await self._run(
            self._activate_story,
            session_id,
            actor_id,
            resume,
            [dict(item) for item in (choices or ())],
        )

    def _activate_story(
        self,
        session_id: str,
        actor_id: str,
        resume: bool,
        supplied_choices: list[dict[str, Any]],
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
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError("副本当前不在准备阶段")
                if not resume and int(session["turn_no"] or 0) > 0:
                    raise InvalidTransitionError(
                        "该副本已有剧情进度，不能再次开演；请使用 /酒馆 继续"
                    )
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
                limits = player_limits(world)
                participants = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND card_status = 'approved'
                      AND ready = 1 AND participation_status = 'active'
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                blockers: list[str] = []
                if len(participants) < limits["minimum_start"]:
                    blockers.append(
                        f"有效出场人数不足：{len(participants)}/{limits['minimum_start']}"
                    )
                if blockers:
                    connection.execute("ROLLBACK")
                    return {
                        "started": False,
                        "ok": False,
                        "blockers": blockers,
                        "participants": [
                            self._participant(row) for row in participants
                        ],
                        "limits": limits,
                    }

                order = [str(row["group_user_id"]) for row in participants]
                stored_state = json_load(session["world_state_json"], {})
                existing_turn = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=set(order),
                )
                if resume:
                    existing_order = [
                        item for item in existing_turn["order"] if item in order
                    ]
                    order = existing_order + [
                        item for item in order if item not in existing_order
                    ]
                turn_state = replace_turn_order(existing_turn, order)
                turn_state["current_user_id"] = (
                    existing_turn["current_user_id"]
                    if resume
                    and existing_turn["current_user_id"] in order
                    else order[0]
                )
                persisted_state = embed_turn_state(
                    public_world_state(stored_state),
                    turn_state,
                )
                current_user_id = turn_state["current_user_id"]
                current = next(
                    row
                    for row in participants
                    if row["group_user_id"] == current_user_id
                )
                preserved_choice = (
                    connection.execute(
                        """
                        SELECT * FROM choice_sets
                        WHERE session_id = ? AND participant_id = ?
                          AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id, current["id"]),
                    ).fetchone()
                    if resume
                    else None
                )
                preserved_vote = (
                    connection.execute(
                        """
                        SELECT * FROM group_votes
                        WHERE session_id = ? AND status = 'open'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    if resume
                    else None
                )
                selected_choices: list[dict[str, Any]] = []
                if not preserved_choice and not preserved_vote:
                    selected_choices = (
                        normalize_choices(supplied_choices)
                        if supplied_choices
                        else (
                            fallback_choices(stored_state)
                            if resume
                            else opening_choices(world)
                        )
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
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
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE sessions SET
                        state = 'running', selected = 1,
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), now, session_id),
                )
                new_revision = int(session["revision"]) + 1
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                choice_id = ""
                choice_row: sqlite3.Row | None = None
                if preserved_vote:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                elif preserved_choice:
                    choice_id = str(preserved_choice["id"])
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET session_revision = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_revision, now, choice_id),
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                else:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    choice_id = new_id("choices")
                    connection.execute(
                        """
                        INSERT INTO choice_sets(
                            id, session_id, participant_id, round_no,
                            session_revision, choices_json, status,
                            reroll_count, idempotency_key, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                        """,
                        (
                            choice_id,
                            session_id,
                            current["id"],
                            turn_state["round_no"],
                            new_revision,
                            json_dump(selected_choices),
                            f"opening:{session_id}:{new_revision}",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ?
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id=current["id"],
                        timer_type="turn",
                        timeout_seconds=time_rules["turn_timeout_seconds"],
                        reminder_seconds=time_rules["turn_reminder_seconds"],
                        action={
                            "choice_set_id": choice_id,
                            "user_id": current_user_id,
                        },
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                opening = (
                    clean_text(
                        world.get("opening_scene"),
                        max_chars=6000,
                    )
                    if not resume and session["turn_no"] == 0
                    else ""
                )
                if opening:
                    connection.execute(
                        """
                        INSERT INTO events(
                            id, session_id, turn_no, role, actor_id,
                            actor_name, content, meta_json, created_at
                        ) VALUES (?, ?, ?, 'system', 'system', '酒馆系统',
                                  ?, ?, ?)
                        """,
                        (
                            new_id("event"),
                            session_id,
                            session["turn_no"],
                            opening,
                            json_dump({"kind": "opening"}),
                            now,
                        ),
                    )
                phase_meta = json_load(config["phase_meta_json"], {})
                phase_meta.update(
                    {
                        "resume_mode": bool(resume),
                        "started_at": now,
                        "frozen_roster": [
                            str(row["id"]) for row in participants
                        ],
                    }
                )
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET phase_meta_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(phase_meta), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.perform" if not resume else "session.continue",
                    session_id,
                    {
                        "roster": order,
                        "choice_set_id": choice_id,
                    },
                )
                updated = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "started": True,
                    "ok": True,
                    "session": self._session(updated),
                    "choice_set": (
                        self._choice_set(choice_row)
                        if choice_row
                        else None
                    ),
                    "vote": (
                        self._vote(preserved_vote)
                        if preserved_vote
                        else None
                    ),
                    "current_participant": self._participant(current),
                    "participants": [
                        self._participant(row) for row in participants
                    ],
                    "opening": opening,
                }
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    async def active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_choice_set, session_id)

    def _active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cs.* FROM choice_sets cs
                WHERE cs.session_id = ? AND cs.status = 'active'
                ORDER BY cs.created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._choice_set(row)
            snapshot_row = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if snapshot_row:
                world_snapshot = json_load(
                    snapshot_row["world_snapshot_json"],
                    {},
                )
                try:
                    result["choices"] = normalize_choices(
                        json_load(row["choices_json"], []),
                        world_snapshot,
                    )
                except ValueError:
                    # Keep the structurally valid stored projection if the
                    # frozen snapshot is unavailable or was externally damaged.
                    pass
            participant = connection.execute(
                "SELECT * FROM participants WHERE id = ?",
                (row["participant_id"],),
            ).fetchone()
            result["participant"] = (
                self._participant(participant) if participant else None
            )
            return result

    async def replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: Sequence[Mapping[str, Any]],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_choices(choices)
        return await self._run(
            self._replace_active_choices,
            session_id,
            participant_id,
            normalized,
            actor_id,
        )

    def _replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("当前没有可重整的选项")
                if current["participant_id"] != participant_id:
                    raise PermissionError("只能重整自己当前回合的选项")
                if int(current["reroll_count"]) >= 1:
                    raise ValueError("本回合的免费重整次数已经用完")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, current["id"]),
                )
                new_id_value = new_id("choices")
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status,
                        reroll_count, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (
                        new_id_value,
                        session_id,
                        participant_id,
                        current["round_no"],
                        session["revision"],
                        json_dump(choices),
                        f"reroll:{current['id']}",
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "choice.reroll",
                    new_id_value,
                    {"previous_choice_set_id": current["id"]},
                )
                row = connection.execute(
                    "SELECT * FROM choice_sets WHERE id = ?",
                    (new_id_value,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._choice_set(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def emergency_replace_choices(
        self,
        session_id: str,
        choices: Sequence[Mapping[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_replace_choices,
            session_id,
            [dict(item) for item in choices],
            actor_id,
        )

    def _emergency_replace_choices(
        self,
        session_id: str,
        choices: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("当前没有可编辑的活动选项")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                now = utc_now()
                connection.execute(
                    "UPDATE choice_sets SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
                choice_id = new_id("choices")
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status,
                        reroll_count, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        choice_id,
                        session_id,
                        current["participant_id"],
                        current["round_no"],
                        session["revision"],
                        json_dump(choices),
                        current["reroll_count"],
                        f"admin-rescue:{choice_id}",
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.replace_choices",
                    choice_id,
                    {"previous_choice_set_id": current["id"]},
                )
                row = connection.execute(
                    "SELECT * FROM choice_sets WHERE id = ?", (choice_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._choice_set(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def emergency_edit_last_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_edit_last_narrative,
            session_id,
            narrative,
            actor_id,
        )

    def _emergency_edit_last_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
    ) -> dict[str, Any]:
        content = clean_text(narrative, max_chars=12000)
        if not content:
            raise ValueError("修订后的故事正文不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE session_id = ? AND role = 'narrator'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前副本还没有故事正文")
                meta = json_load(row["meta_json"], {})
                meta = meta if isinstance(meta, dict) else {}
                revisions = list(meta.get("admin_revisions") or [])
                revisions.append(
                    {
                        "actor_id": actor_id,
                        "previous": str(row["content"])[:1000],
                        "at": utc_now(),
                    }
                )
                meta["admin_revisions"] = revisions[-10:]
                connection.execute(
                    "UPDATE events SET content = ?, meta_json = ? WHERE id = ?",
                    (content, json_dump(meta), row["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.edit_narrative",
                    row["id"],
                    {"turn_no": row["turn_no"]},
                )
                updated = connection.execute(
                    "SELECT * FROM events WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.execute("COMMIT")
                return self._event(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def emergency_append_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_append_narrative,
            session_id,
            narrative,
            actor_id,
        )

    def _emergency_append_narrative(
        self,
        session_id: str,
        narrative: str,
        actor_id: str,
    ) -> dict[str, Any]:
        content = clean_text(narrative, max_chars=12000)
        if not content:
            raise ValueError("过渡剧情不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                now = utc_now()
                event_id = new_id("event")
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'narrator', ?, '管理员过渡', ?, ?, ?)
                    """,
                    (
                        event_id,
                        session_id,
                        session["turn_no"],
                        actor_id,
                        content,
                        json_dump({"admin_bridge": True}),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.bridge_narrative",
                    event_id,
                    {"turn_no": session["turn_no"]},
                )
                row = connection.execute(
                    "SELECT * FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._event(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def active_vote(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_vote, session_id)

    def _active_vote(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_votes
                WHERE session_id = ? AND status = 'open'
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._vote(row)
            ballots = connection.execute(
                """
                SELECT user_id, option_key, created_at, updated_at
                FROM vote_ballots WHERE vote_id = ?
                ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
            result["ballots"] = [dict(item) for item in ballots]
            tally = vote_result(
                eligible_count=len(result["eligible_user_ids"]),
                ballots=result["ballots"],
                option_keys=[
                    str(item.get("key")) for item in result["options"]
                ],
            )
            result["tally"] = tally
            return result

    async def create_group_vote(
        self,
        session_id: str,
        *,
        group_decision: Mapping[str, Any],
        suspended_user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """0.11.2：为「全队行动」选项直接发起集体表决。

        旧流程要求叙事模型在 resolve 里自行生成 group_decision，
        模型未生成时整轮被拒（“该选项影响全队，但模型没有生成集体表决”）。
        现在由引擎在选项被选中时调用本方法，立即进入全员投票。
        """
        return await self._run(
            self._create_group_vote,
            session_id,
            dict(group_decision),
            suspended_user_id,
            actor_id,
        )

    def _create_group_vote(
        self,
        session_id: str,
        group_decision: dict[str, Any],
        suspended_user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        question = clean_text(
            group_decision.get("question"),
            max_chars=500,
        )
        options = self._normalize_vote_options(
            group_decision.get("options")
        )
        if not question or len(options) < 2:
            raise ValueError("集体表决需要问题与至少两个选项")
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
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("酒馆当前不在运行状态")
                if connection.execute(
                    """
                    SELECT 1 FROM group_votes
                    WHERE session_id = ? AND status = 'open'
                    """,
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError("已有一场集体表决进行中")
                suspended_user_id = validate_platform_id(
                    suspended_user_id,
                    label="行动玩家 ID",
                )
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
                        (session_id,),
                    ).fetchall()
                ]
                config_row = connection.execute(
                    """
                    SELECT time_rules_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                time_rules = normalize_time_rules(
                    json_load(
                        config_row["time_rules_json"] if config_row else "",
                        {},
                    )
                )
                vote_id = new_id("vote")
                event_id = new_id("event")
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
                        session_id,
                        event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        suspended_user_id,
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', 'vote', '集体表决', ?, ?, ?)
                    """,
                    (
                        event_id,
                        session_id,
                        session["turn_no"],
                        f"【集体表决】{question}",
                        json_dump(
                            {
                                "kind": "group_vote",
                                "vote_id": vote_id,
                                "status": "open",
                            }
                        ),
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules[
                        "vote_round_one_seconds"
                    ],
                    reminder_seconds=time_rules[
                        "vote_reminder_seconds"
                    ],
                    action={"vote_id": vote_id, "stage": 1},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "vote.created",
                    vote_id,
                    {"question": question},
                )
                vote_row = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._vote(vote_row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._cast_vote,
            session_id,
            user_id,
            option_key,
        )

    def _cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        key = str(option_key or "").strip().upper()
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
                    raise DatabaseNotFoundError("当前没有进行中的集体投票")
                vote = self._vote(vote_row)
                # A16：代理投票——被托管人可代替角色投票，票记在角色名下。
                operator_id = user_id
                effective_user_id = user_id
                now_check = utc_now()
                grant = connection.execute(
                    """
                    SELECT d.owner_user_id, d.source, d.permissions_json
                    FROM delegation_grants d
                    WHERE d.session_id = ? AND d.delegate_user_id = ?
                      AND d.status = 'active'
                      AND d.expires_at IN ('', ?)
                      AND 'vote' IN (
                          SELECT value FROM json_each(d.permissions_json)
                      )
                    ORDER BY d.created_at DESC LIMIT 1
                    """,
                    (session_id, user_id, now_check),
                ).fetchone()
                if grant and str(grant["owner_user_id"]) in vote["eligible_user_ids"]:
                    effective_user_id = str(grant["owner_user_id"])
                if effective_user_id not in vote["eligible_user_ids"]:
                    raise PermissionError("你不在本次投票的有效成员名单中")
                if effective_user_id != user_id:
                    self._insert_audit(
                        connection,
                        session_id,
                        user_id,
                        "delegation.vote",
                        str(vote_row["id"]),
                        {
                            "operator_user_id": operator_id,
                            "ballot_user_id": effective_user_id,
                            "source": str(grant["source"]),
                        },
                    )
                valid_keys = {
                    str(item.get("key")) for item in vote["options"]
                }
                if key not in valid_keys:
                    raise ValueError(
                        "请选择：" + " / ".join(sorted(valid_keys))
                    )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO vote_ballots(
                        id, vote_id, user_id, option_key,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vote_id, user_id) DO UPDATE SET
                        option_key = excluded.option_key,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("ballot"),
                        vote["id"],
                        effective_user_id,
                        key,
                        now,
                        now,
                    ),
                )
                ballots = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT user_id, option_key FROM vote_ballots
                        WHERE vote_id = ?
                        """,
                        (vote["id"],),
                    ).fetchall()
                ]
                tally = vote_result(
                    eligible_count=len(vote["eligible_user_ids"]),
                    ballots=ballots,
                    option_keys=sorted(valid_keys),
                )
                status = "open"
                winner = str(tally["winner"] or "")
                stage = int(vote["stage"])
                options = vote["options"]
                if winner:
                    status = "passed"
                elif tally["all_voted"] and tally["quorum"]:
                    counts = tally["counts"]
                    ranking = sorted(
                        options,
                        key=lambda item: (
                            -int(counts.get(str(item.get("key")), 0)),
                            str(item.get("key")),
                        ),
                    )
                    if stage == 1 and len(ranking) > 2:
                        top_count = int(
                            counts.get(str(ranking[0].get("key")), 0)
                        )
                        tied_top = [
                            item
                            for item in ranking
                            if int(counts.get(str(item.get("key")), 0))
                            == top_count
                        ]
                        runoff = (
                            tied_top[:2]
                            if len(tied_top) >= 2
                            else ranking[:2]
                        )
                        connection.execute(
                            "DELETE FROM vote_ballots WHERE vote_id = ?",
                            (vote["id"],),
                        )
                        config = connection.execute(
                            """
                            SELECT time_rules_json FROM instance_configs
                            WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        time_rules = normalize_time_rules(
                            json_load(
                                config["time_rules_json"] if config else "",
                                {},
                            )
                        )
                        new_deadline = deadline_after(
                            time_rules["vote_round_two_seconds"]
                        )
                        connection.execute(
                            """
                            UPDATE group_votes SET
                                options_json = ?, stage = 2,
                                deadline_at = ?, result_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(runoff),
                                new_deadline,
                                json_dump(
                                    {
                                        "round_one": tally,
                                        "reason": "runoff",
                                    }
                                ),
                                now,
                                vote["id"],
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'completed', updated_at = ?
                            WHERE session_id = ? AND timer_type = 'vote'
                              AND status = 'active'
                            """,
                            (now, session_id),
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id="",
                            timer_type="vote",
                            timeout_seconds=time_rules[
                                "vote_round_two_seconds"
                            ],
                            reminder_seconds=time_rules[
                                "vote_reminder_seconds"
                            ],
                            action={"vote_id": vote["id"], "stage": 2},
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "vote.runoff",
                            vote["id"],
                            {"tally": tally},
                        )
                        updated_vote = connection.execute(
                            "SELECT * FROM group_votes WHERE id = ?",
                            (vote["id"],),
                        ).fetchone()
                        connection.execute("COMMIT")
                        return {
                            "vote": self._vote(updated_vote),
                            "tally": tally,
                            "resolved": False,
                            "runoff": True,
                        }
                    status = "rejected"

                if status != "open":
                    connection.execute(
                        """
                        UPDATE group_votes SET
                            status = ?, winner_key = ?,
                            result_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            winner,
                            json_dump(tally),
                            now,
                            vote["id"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'completed', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'vote'
                          AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    winning_text = ""
                    for option in vote["options"]:
                        if str(option.get("key")) == winner:
                            winning_text = str(option.get("text") or "")
                            break
                    event_text = (
                        f"【集体决定】{winning_text}"
                        if status == "passed"
                        else "【集体决定】本次表决未形成多数，队伍维持现状。"
                    )
                    session = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if status == "passed" and winning_text:
                        stored_state = json_load(
                            session["world_state_json"],
                            {},
                        )
                        public_state = public_world_state(stored_state)
                        facts = public_state.get("facts")
                        facts = list(facts) if isinstance(facts, list) else []
                        decision_fact = f"队伍多数决定：{winning_text}"
                        if decision_fact not in facts:
                            facts.append(decision_fact)
                        public_state["facts"] = facts[-200:]
                        public_state["scene_summary"] = decision_fact
                        connection.execute(
                            """
                            UPDATE sessions SET
                                world_state_json = ?,
                                revision = revision + 1,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(
                                    embed_turn_state(
                                        public_state,
                                        turn_state_from_world(
                                            stored_state
                                        ),
                                    )
                                ),
                                now,
                                session_id,
                            ),
                        )
                        session = connection.execute(
                            "SELECT * FROM sessions WHERE id = ?",
                            (session_id,),
                        ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO events(
                            id, session_id, turn_no, role, actor_id,
                            actor_name, content, meta_json, created_at
                        ) VALUES (?, ?, ?, 'system', 'vote', '集体表决',
                                  ?, ?, ?)
                        """,
                        (
                            new_id("event"),
                            session_id,
                            session["turn_no"],
                            event_text,
                            json_dump(
                                {
                                    "kind": "group_vote",
                                    "vote_id": vote["id"],
                                    "status": status,
                                    "winner": winner,
                                }
                            ),
                            now,
                        ),
                    )
                    self._resume_after_vote(
                        connection,
                        session=session,
                        vote=vote,
                        now=now,
                    )
                    self._apply_return_vote_result(
                        connection,
                        vote_id=vote["id"],
                        passed=status == "passed",
                        now=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "vote.cast",
                    vote["id"],
                    {
                        "option": key,
                        "status": status,
                        "tally": tally,
                    },
                )
                updated_vote = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "vote": self._vote(updated_vote),
                    "tally": tally,
                    "resolved": status != "open",
                    "runoff": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _resume_after_vote(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        vote: Mapping[str, Any],
        now: str,
    ) -> None:
        user_id = str(vote.get("suspended_user_id") or "")
        if not user_id:
            return
        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], user_id),
        ).fetchone()
        if not participant:
            return
        # 0.11.2：投票已结束，作废残留的 active 选项集。
        # 旧实现因残留选项存在而直接 return，导致表决通过后
        # 既不生成新选项、旧选项又一直挂在 WebUI 上。
        connection.execute(
            """
            UPDATE choice_sets SET status = 'superseded'
            WHERE session_id = ? AND status = 'active'
            """,
            (session["id"],),
        )
        latest = connection.execute(
            "SELECT status FROM group_votes WHERE id = ?",
            (vote["id"],),
        ).fetchone()
        if latest and latest["status"] == "passed":
            # 表决通过：新叙事与新选项由上层 process_vote_resolution 生成，
            # 这里只恢复行动权（行动指针未动，保持由被挂起玩家继续）。
            return
        state = json_load(session["world_state_json"], {})
        choices = fallback_choices(state)
        choice_id = new_id("choices")
        turn = turn_state_from_world(state)
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
                participant["id"],
                turn["round_no"],
                session["revision"],
                json_dump(choices),
                f"post-vote:{vote['id']}",
                now,
                now,
            ),
        )
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": user_id,
            },
        )

    async def commit_vote_resolution(
        self,
        session_id: str,
        *,
        expected_revision: int,
        narrative: str,
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]] = (),
        model_payload: Mapping[str, Any] | None = None,
        workflow: Mapping[str, Any] | None = None,
        vote_id: str = "",
    ) -> dict[str, Any]:
        """0.11.2：把「集体表决通过」的落实叙事落库，并生成下一组选项。

        与 DM 推进类似：不移动玩家指针、不消耗个人行动机会；
        表决结果作为已定事实推进剧情。
        """
        return await self._run(
            self._commit_vote_resolution,
            session_id,
            expected_revision,
            narrative,
            dict(world_state),
            [dict(item) for item in memories],
            dict(model_payload or {}),
            dict(workflow or {}),
            vote_id,
        )

    def _commit_vote_resolution(
        self,
        session_id: str,
        expected_revision: int,
        narrative: str,
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        model_payload: dict[str, Any],
        workflow: dict[str, Any],
        vote_id: str,
    ) -> dict[str, Any]:
        narrative = clean_text(narrative, max_chars=12000)
        if not narrative:
            raise ValueError("表决落实叙事不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if int(session["revision"]) != int(expected_revision):
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("酒馆当前不在运行状态")
                if vote_id:
                    vote_row = connection.execute(
                        "SELECT * FROM group_votes WHERE id = ?",
                        (vote_id,),
                    ).fetchone()
                    if vote_row and vote_row["status"] == "open":
                        raise InvalidTransitionError("集体投票尚未结束")
                now = utc_now()
                new_turn = int(session["turn_no"]) + 1
                stored_state = json_load(session["world_state_json"], {})
                turn = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(
                    public_world_state(world_state),
                    turn,
                )
                event_id = new_id("event")
                meta: dict[str, Any] = {
                    "vote_resolution": True,
                    "vote_id": vote_id,
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
                        event_id,
                        session_id,
                        new_turn,
                        "system",
                        "集体表决",
                        narrative,
                        json_dump(meta),
                        now,
                    ),
                )
                for memory in memories[:12]:
                    content = clean_text(
                        memory.get("content"),
                        max_chars=1200,
                    )
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
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            new_id("memory"),
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            max(1, min(5, int(memory.get("importance", 3)))),
                            json_dump(list(memory.get("tags") or [])),
                            fingerprint,
                            event_id,
                            now,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_turn,
                        json_dump(persisted_state),
                        now,
                        session_id,
                    ),
                )
                result: dict[str, Any] = {
                    "session_id": session_id,
                    "event_id": event_id,
                    "turn_no": new_turn,
                    "narrative": narrative,
                }
                next_choices = workflow.get("next_choices")
                next_choice_set_id = ""
                if next_choices:
                    # choice_sets 为每会话单集（UNIQUE(session_id)），
                    # 插入前必须先作废现有 active 集。
                    connection.execute(
                        """
                        UPDATE choice_sets SET status = 'superseded'
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (session_id,),
                    )
                    try:
                        normalized = normalize_choices(next_choices)
                    except ValueError:
                        normalized = fallback_choices(world_state)
                    participant = connection.execute(
                        """
                        SELECT * FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        ORDER BY CASE WHEN group_user_id = ? THEN 0 ELSE 1 END,
                                 created_at LIMIT 1
                        """,
                        (session_id, turn.get("current_user_id") or ""),
                    ).fetchone()
                    if participant:
                        choice_id = new_id("choices")
                        connection.execute(
                            """
                            INSERT INTO choice_sets(
                                id, session_id, participant_id, round_no,
                                session_revision, choices_json, status,
                                reroll_count, idempotency_key, created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                            """,
                            (
                                choice_id,
                                session_id,
                                participant["id"],
                                turn["round_no"],
                                int(session["revision"]) + 1,
                                json_dump(normalized),
                                f"vote-resolution:{vote_id or '?'}",
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
                            json_load(
                                config["time_rules_json"] if config else "",
                                {},
                            )
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=participant["id"],
                            timer_type="turn",
                            timeout_seconds=rules["turn_timeout_seconds"],
                            reminder_seconds=rules[
                                "turn_reminder_seconds"
                            ],
                            action={
                                "choice_set_id": choice_id,
                                "user_id": participant[
                                    "group_user_id"
                                ],
                            },
                        )
                        next_choice_set_id = choice_id
                result["next_choice_set_id"] = next_choice_set_id
                self._insert_audit(
                    connection,
                    session_id,
                    "system",
                    "vote.resolution_committed",
                    event_id,
                    {"vote_id": vote_id, "turn_no": new_turn},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def pending_vote_resolution(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """0.11.3：查询等待自动推进的表决。

        定时器结束且已通过但尚未落实叙事的投票会被标记
        result_json.pending_resolution=true，由下次输入自动推进。
        """
        return await self._run(
            self._pending_vote_resolution,
            session_id,
        )

    def _pending_vote_resolution(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_votes
                WHERE session_id = ? AND status = 'passed'
                  AND result_json LIKE '%"pending_resolution":true%'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return self._vote(row) if row else None

    async def clear_vote_resolution_pending(
        self,
        vote_id: str,
    ) -> None:
        """清除「待推进」标记（表决已落实叙事）。"""
        await self._run(
            self._clear_vote_resolution_pending,
            vote_id,
        )

    def _clear_vote_resolution_pending(
        self,
        vote_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT result_json FROM group_votes WHERE id = ?",
                    (vote_id,),
                ).fetchone()
                if row and row["result_json"]:
                    data = json_load(row["result_json"], {})
                    data.pop("pending_resolution", None)
                    connection.execute(
                        """
                        UPDATE group_votes
                        SET result_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(data), utc_now(), vote_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _apply_return_vote_result(
        connection: sqlite3.Connection,
        *,
        vote_id: str,
        passed: bool,
        now: str,
    ) -> None:
        request_row = connection.execute(
            """
            SELECT * FROM return_requests WHERE vote_id = ?
            """,
            (vote_id,),
        ).fetchone()
        if not request_row:
            return
        if passed:
            config = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (request_row["session_id"],),
            ).fetchone()
            world = json_load(
                config["world_snapshot_json"] if config else "",
                {},
            )
            limits = player_limits(world)
            placeholders = ",".join(
                "?" for _ in SEAT_HOLDING_STATUSES
            )
            occupied = connection.execute(
                f"""
                SELECT COUNT(*) FROM participants
                WHERE session_id = ?
                  AND participation_status IN ({placeholders})
                """,
                (
                    request_row["session_id"],
                    *sorted(SEAT_HOLDING_STATUSES),
                ),
            ).fetchone()[0]
            if occupied >= limits["maximum"]:
                connection.execute(
                    """
                    UPDATE return_requests
                    SET status = 'cancelled',
                        progress_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump({"reason": "seat_unavailable"}),
                        now,
                        request_row["id"],
                    ),
                )
                return
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'quest_active', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )
            connection.execute(
                """
                UPDATE participants
                SET participation_status = 'standby', ready = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["participant_id"]),
            )
        else:
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'rejected', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )

    @staticmethod
    def _record_return_progress(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        request_id: str,
        evidence: str,
        completed: bool,
        round_no: int,
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT rr.*, pt.character_name, pt.display_name
            FROM return_requests rr
            JOIN participants pt ON pt.id = rr.participant_id
            WHERE rr.id = ? AND rr.session_id = ?
              AND rr.status = 'quest_active'
            """,
            (request_id, session_id),
        ).fetchone()
        if not row:
            return None
        progress = json_load(row["progress_json"], {})
        entries = progress.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "turn_no": turn_no,
                "evidence": evidence,
                "created_at": now,
            }
        )
        progress["entries"] = entries[-20:]
        if completed:
            progress["completed_at"] = now
            progress["completion_evidence"] = evidence
            connection.execute(
                """
                UPDATE return_requests SET
                    status = 'completed', progress_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dump(progress), now, request_id),
            )
            connection.execute(
                """
                UPDATE participants SET
                    participation_status = 'active', ready = 1,
                    joined_round = ?, updated_at = ?
                WHERE id = ?
                """,
                (round_no + 1, now, row["participant_id"]),
            )
            name = row["character_name"] or row["display_name"]
            narrative = (
                f"众人完成了约定的寻找条件，并在合理的时机重新找到了{name}。"
                f"{name}将在下一轮队尾重新加入行动。"
            )
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'system', 'system', '返场幕间',
                          ?, ?, ?)
                """,
                (
                    new_id("event"),
                    session_id,
                    turn_no,
                    narrative,
                    json_dump(
                        {
                            "kind": "return_complete",
                            "return_request_id": request_id,
                            "participant_id": row["participant_id"],
                        }
                    ),
                    now,
                ),
            )
            return {
                "request_id": request_id,
                "completed": True,
                "narrative": narrative,
            }
        connection.execute(
            """
            UPDATE return_requests
            SET progress_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dump(progress), now, request_id),
        )
        return {
            "request_id": request_id,
            "completed": False,
            "evidence": evidence,
        }

    async def set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_away,
            session_id,
            user_id,
        )

    def _set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能暂离")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'away', ready = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, participant["id"]),
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                next_turn, removed = leave_turn(turn_state, user_id)
                if removed:
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(
                                embed_turn_state(
                                    public_world_state(stored_state),
                                    next_turn,
                                )
                            ),
                            now,
                            session_id,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.away",
                    participant["id"],
                    {"seat_released": False},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._return_to_queue,
            session_id,
            user_id,
        )

    def _return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                self._assert_session_writable(connection, session_id)
                if participant["participation_status"] != PARTICIPANT_AWAY:
                    raise ValueError("当前角色并非暂离状态")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                joined_round = int(turn_state["round_no"]) + 1
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'active',
                        joined_round = ?, ready = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (joined_round, now, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.return_queue",
                    participant["id"],
                    {"effective_round": joined_round},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["effective_round"] = joined_round
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def retire_participant(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        forced: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            actor_id,
            forced,
            reason,
        )

    async def retire_self(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            user_id=user_id,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            user_id,
            False,
            "player_exit",
        )

    def _retire_participant(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                result = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=forced,
                    reason=reason,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _retire_participant_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
    ) -> dict[str, Any]:
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        if not participant or participant["session_id"] != session_id:
            raise DatabaseNotFoundError("角色不存在")
        if participant["participation_status"] in {
            PARTICIPANT_RETIRED,
            PARTICIPANT_ARCHIVED,
        }:
            raise ValueError("该角色已经退场")
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        config = connection.execute(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        character_name = (
            participant["character_name"]
            or participant["display_name"]
        )
        narrative = safe_exit_narrative(
            world,
            character_name,
            forced=forced,
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE participants SET
                participation_status = 'retired', ready = 0,
                exit_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                clean_text(reason or "departure", max_chars=500),
                now,
                participant_id,
            ),
        )
        connection.execute(
            """
            UPDATE players SET enabled = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, participant["player_id"]),
        )
        stored_state = json_load(session["world_state_json"], {})
        turn_state = turn_state_from_world(stored_state)
        next_turn, removed = leave_turn(
            turn_state,
            participant["group_user_id"],
        )
        if removed:
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dump(
                        embed_turn_state(
                            public_world_state(stored_state),
                            next_turn,
                        )
                    ),
                    now,
                    session_id,
                ),
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ?
              AND status IN ('active', 'paused')
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE delegation_grants
            SET status = 'revoked', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE character_card_drafts
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE card_binding_codes
            SET status = 'expired'
            WHERE participant_id = ? AND status = 'active'
            """,
            (participant_id,),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'system', '退场幕间',
                      ?, ?, ?)
            """,
            (
                new_id("event"),
                session_id,
                session["turn_no"],
                narrative,
                json_dump(
                    {
                        "kind": "safe_exit",
                        "participant_id": participant_id,
                        "forced": forced,
                    }
                ),
                now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id,
            "participant.retire",
            participant_id,
            {
                "forced": forced,
                "reason": reason,
                "seat_released": True,
            },
        )
        return {
            "participant": self._participant(participant),
            "narrative": narrative,
            "turn_changed": removed,
        }

    async def create_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        scope: str = "instance",
        duration_seconds: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        retirement = await self._run(
            self._retire_and_ban,
            session_id,
            participant["id"],
            actor_id,
            scope,
            duration_seconds,
            reason,
        )
        return retirement

    def _retire_and_ban(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        scope: str,
        duration_seconds: int | None,
        reason: str,
    ) -> dict[str, Any]:
        if scope not in {"instance", "group", "global"}:
            raise ValueError("封禁范围必须是 instance、group 或 global")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                retirement = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=True,
                    reason=reason or "banned",
                )
                participant = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                expires_at = deadline_after(duration_seconds)
                ban_id = new_id("ban")
                connection.execute(
                    """
                    INSERT INTO ban_records(
                        id, session_id, platform_id, group_id, user_id,
                        participant_id, scope, reason, actor_id, status,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        ban_id,
                        session_id if scope == "instance" else "",
                        (
                            session["platform_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        (
                            session["group_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        participant["group_user_id"],
                        participant_id,
                        scope,
                        clean_text(reason, max_chars=500),
                        actor_id,
                        expires_at,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.create",
                    ban_id,
                    {
                        "scope": scope,
                        "duration_seconds": duration_seconds,
                        "participant_id": participant_id,
                    },
                )
                connection.execute("COMMIT")
                return {
                    **retirement,
                    "ban": {
                        "id": ban_id,
                        "scope": scope,
                        "expires_at": expires_at,
                        "reason": reason,
                    },
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
    ) -> int:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._revoke_ban,
            session_id,
            participant["group_user_id"],
            actor_id,
        )

    def _revoke_ban(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE ban_records
                    SET status = 'revoked', updated_at = ?
                    WHERE user_id = ? AND status = 'active'
                    """,
                    (now, user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.revoke",
                    user_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_bans(
        self,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_bans, session_id)

    def _list_bans(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
                """,
                (now, now),
            )
            if session_id:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' AND (
                           scope = 'global'
                        OR (scope = 'group'
                            AND platform_id = ? AND group_id = ?)
                        OR (scope = 'instance' AND session_id = ?)
                    )
                    ORDER BY created_at DESC
                    """,
                    (
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' ORDER BY created_at DESC
                    """
                ).fetchall()
            return [dict(row) for row in rows]

    async def request_return(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._request_return,
            session_id,
            user_id,
        )

    def _request_return(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("没有可返场的历史角色")
                if participant["participation_status"] != PARTICIPANT_RETIRED:
                    raise ValueError("只有已经正式退场的角色可以申请返场")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                ):
                    raise PermissionError("封禁尚未解除，不能申请返场")
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
                limits = player_limits(world)
                placeholders = ",".join(
                    "?" for _ in SEAT_HOLDING_STATUSES
                )
                occupied = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM participants
                    WHERE session_id = ?
                      AND participation_status IN ({placeholders})
                    """,
                    (session_id, *sorted(SEAT_HOLDING_STATUSES)),
                ).fetchone()[0]
                if occupied >= limits["maximum"]:
                    raise ValueError("当前没有空余席位，暂时无法申请返场")
                existing = connection.execute(
                    """
                    SELECT * FROM return_requests
                    WHERE participant_id = ?
                      AND status IN ('requested', 'voting', 'quest_active')
                    """,
                    (participant["id"],),
                ).fetchone()
                if existing:
                    raise ValueError("该角色已经有进行中的返场流程")
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        """,
                        (session_id,),
                    ).fetchall()
                ]
                if not eligible:
                    raise ValueError("当前没有可参与返场表决的在场玩家")
                name = (
                    participant["character_name"]
                    or participant["display_name"]
                )
                objective = (
                    f"沿着{name}离场时留下的线索，完成一次合理的寻找、"
                    "营救、解除困境或约定会合剧情。"
                )
                now = utc_now()
                vote_id = new_id("vote")
                options = [
                    {"key": "A", "text": f"同意开启{name}的返场支线"},
                    {"key": "B", "text": "暂不开启返场支线"},
                ]
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, question, options_json,
                        eligible_user_ids_json, stage, status,
                        suspended_user_id, deadline_at, result_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 'open', '', ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session_id,
                        f"是否为{name}开启一条需要通过剧情完成的返场支线？",
                        json_dump(options),
                        json_dump(eligible),
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                request_id = new_id("return")
                connection.execute(
                    """
                    INSERT INTO return_requests(
                        id, session_id, participant_id, requested_by,
                        status, exit_type, objective, progress_json,
                        vote_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'voting', 'departure', ?,
                              '{}', ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        participant["id"],
                        user_id,
                        objective,
                        vote_id,
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "return_request_id": request_id},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "return.request",
                    request_id,
                    {"vote_id": vote_id, "objective": objective},
                )
                connection.execute("COMMIT")
                return {
                    "request_id": request_id,
                    "vote_id": vote_id,
                    "objective": objective,
                    "character_name": name,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    # ── A17：后台调整回合顺序前作废旧活跃选项 ─────────────────────
    async def supersede_active_choices(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        """把当前活跃的 A–D 选项集标记为 superseded（防 actor_id 错位）。"""
        return await self._run(
            self._supersede_active_choices, session_id, actor_id
        )

    def _supersede_active_choices(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'superseded', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn.choices_superseded",
                    "",
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return cursor.rowcount
