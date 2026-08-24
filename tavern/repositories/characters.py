"""Domain repository methods extracted from the SQLite store."""

from ..card_wizard import (
    LAST_MESSAGE_KEY,
    choose_option,
    choose_options,
    clear_field_and_dependents,
    field_visible,
    navigate_page,
    preset_options,
    store_preset_snapshot,
    store_preset_snapshots,
)
from ..database_support import *
from ..capability_service import CapabilityService
from ..entity_registry import EntityRegistry, module_value
from ..resolution_receipts import content_hash


class CharacterRepositoryMixin:
    async def list_roster(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_roster, session_id)

    def _list_roster(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    pt.*,
                    d.status AS draft_status,
                    d.current_step AS draft_step,
                    d.expires_at AS draft_expires_at,
                    d.fields_json AS draft_profile_json,
                    d.template_version AS draft_template_version,
                    (
                        SELECT c.code FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_code,
                    (
                        SELECT c.expires_at FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_expires_at,
                    rs.state_json AS runtime_state_json,
                    rs.revision AS runtime_revision,
                    ccv.profile_json AS card_profile_json,
                    ccv.stats_json AS card_stats_json,
                    ccv.version_no AS card_version_no,
                    ccv.template_version AS card_template_version,
                    ccv.status AS card_version_status,
                    ccv.review_note AS card_review_note,
                    ccv.reviewed_by AS card_reviewed_by,
                    ccv.created_at AS card_version_created_at
                FROM participants pt
                LEFT JOIN character_card_drafts d
                  ON d.participant_id = pt.id
                LEFT JOIN character_runtime_states rs
                  ON rs.participant_id = pt.id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ?
                ORDER BY
                    CASE pt.participation_status
                        WHEN 'active' THEN 0
                        WHEN 'reserved' THEN 1
                        WHEN 'standby' THEN 2
                        WHEN 'away' THEN 3
                        WHEN 'retired' THEN 4
                        ELSE 5
                    END,
                    pt.created_at
                """,
                (session_id,),
            ).fetchall()
            return [self._participant(row) for row in rows]

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
        include_retired: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._get_participant,
            session_id,
            user_id,
            participant_ref,
            include_retired,
        )

    def _get_participant(
        self,
        session_id: str,
        user_id: str,
        participant_ref: str,
        include_retired: bool,
    ) -> dict[str, Any]:
        reference = str(participant_ref or "").strip()
        with self._connect() as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家尚未加入当前副本")
                return self._participant(row)
            rows = connection.execute(
                """
                SELECT * FROM participants WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
            matches: list[sqlite3.Row] = []
            lowered = reference.casefold()
            for row in rows:
                if not include_retired and row["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    continue
                aliases = json_load(row["aliases_json"], [])
                names = {
                    str(row["id"]),
                    str(row["character_name"]),
                    str(row["character_code"]),
                    *(str(item) for item in aliases),
                }
                if any(item and item.casefold() == lowered for item in names):
                    matches.append(row)
            if not matches:
                raise DatabaseNotFoundError("未找到精确匹配的角色名或代号")
            if len(matches) > 1:
                raise ValueError("角色标识不唯一，请改用副本内唯一代号")
            return self._participant(matches[0])

    async def authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authoritative_modifier,
            session_id,
            user_id,
            stat_ref,
        )

    def _authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.character_name, pt.display_name,
                       ccv.stats_json
                FROM participants pt
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            stats = json_load(row["stats_json"], {})
            modifiers = stats.get("modifiers")
            labels = stats.get("labels")
            modifiers = modifiers if isinstance(modifiers, Mapping) else {}
            labels = labels if isinstance(labels, Mapping) else {}
            reference = clean_text(stat_ref, max_chars=40).casefold()
            matched_key = ""
            for key, label in labels.items():
                candidates = {
                    str(key).casefold(),
                    str(label).casefold(),
                    f"{label}检定".casefold(),
                }
                if reference in candidates:
                    matched_key = str(key)
                    break
            if not matched_key and reference in {
                str(key).casefold() for key in modifiers
            }:
                matched_key = next(
                    str(key)
                    for key in modifiers
                    if str(key).casefold() == reference
                )
            if not matched_key:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            return {
                "stat": str(labels.get(matched_key) or matched_key),
                "modifier": max(
                    -10,
                    min(10, int(modifiers.get(matched_key, 0))),
                ),
                "matched": True,
            }

    @staticmethod
    def _active_ban_for(
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        user_id: str,
    ) -> sqlite3.Row | None:
        now = utc_now()
        connection.execute(
            """
            UPDATE ban_records SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
            """,
            (now, now),
        )
        return connection.execute(
            """
            SELECT * FROM ban_records
            WHERE user_id = ? AND status = 'active'
              AND (
                    scope = 'global'
                 OR (scope = 'group' AND platform_id = ? AND group_id = ?)
                 OR (scope = 'instance' AND session_id = ?)
              )
            ORDER BY
                CASE scope
                    WHEN 'global' THEN 0
                    WHEN 'group' THEN 1
                    ELSE 2
                END
            LIMIT 1
            """,
            (
                user_id,
                session["platform_id"],
                session["group_id"],
                session["id"],
            ),
        ).fetchone()

    async def reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_participant,
            session_id,
            user_id,
            display_name,
        )

    def _reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
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
                    raise InvalidTransitionError(
                        "只有准备大厅开放加入；请先由主持人开启副本"
                    )
                ban = self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                )
                if ban:
                    reason = str(ban["reason"] or "未注明")
                    raise PermissionError(f"当前无法加入：{reason}")

                existing = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing and existing["participation_status"] not in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    code_row = connection.execute(
                        """
                        SELECT * FROM card_binding_codes
                        WHERE participant_id = ? AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (existing["id"],),
                    ).fetchone()
                    now = utc_now()
                    # expires_at 为空串表示「不限时」，永远不判过期。
                    if (
                        code_row
                        and code_row["expires_at"]
                        and code_row["expires_at"] <= now
                    ):
                        connection.execute(
                            "UPDATE card_binding_codes SET status = 'expired' WHERE id = ?",
                            (code_row["id"],),
                        )
                        code_row = None
                    renewed = False
                    if (
                        not code_row
                        and existing["card_status"] in {CARD_UNCREATED, CARD_DRAFT}
                        and not str(existing["private_origin"] or "")
                    ):
                        config_row = connection.execute(
                            "SELECT time_rules_json FROM instance_configs WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        time_rules = normalize_time_rules(
                            json_load(config_row["time_rules_json"] if config_row else "", {})
                        )
                        code = ""
                        for _ in range(20):
                            candidate = secrets.token_hex(3).upper()
                            if not connection.execute(
                                "SELECT 1 FROM card_binding_codes WHERE code = ?",
                                (candidate,),
                            ).fetchone():
                                code = candidate
                                break
                        if not code:
                            raise RuntimeError("无法生成唯一建卡码")
                        expires_at = deadline_after(time_rules["card_code_ttl_seconds"])
                        connection.execute(
                            """
                            INSERT INTO card_binding_codes(
                                id, participant_id, code, status, expires_at, created_at
                            ) VALUES (?, ?, ?, 'active', ?, ?)
                            """,
                            (new_id("cardcode"), existing["id"], code, expires_at, now),
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=existing["id"],
                            timer_type="card_code",
                            timeout_seconds=time_rules["card_code_ttl_seconds"],
                            reminder_seconds=None,
                            action={"code": code, "renewed": True},
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "card.code_reissued",
                            existing["id"],
                            {"reason": "join_after_expiry"},
                        )
                        code_row = {"code": code, "expires_at": expires_at}
                        renewed = True
                    connection.execute("COMMIT")
                    result = self._participant(existing)
                    result["joined"] = False
                    result["binding_code"] = (
                        code_row["code"] if code_row else ""
                    )
                    result["binding_expires_at"] = (
                        code_row["expires_at"] if code_row else ""
                    )
                    result["binding_code_reissued"] = renewed
                    return result
                if existing and existing["participation_status"] == PARTICIPANT_RETIRED:
                    raise ValueError(
                        "该角色已经正式退场；请使用 /酒馆 申请返场"
                    )
                reusable_cancelled = bool(
                    existing
                    and existing["participation_status"] == PARTICIPANT_ARCHIVED
                    and existing["exit_reason"] == "cancelled_card"
                    and existing["card_status"] == CARD_UNCREATED
                )
                if existing and not reusable_cancelled:
                    raise ValueError("该加入记录已归档；请联系主持人处理")

                config_row = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not config_row:
                    raise DatabaseNotFoundError("副本配置不存在")
                world = json_load(config_row["world_snapshot_json"], {})
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
                    raise ValueError(
                        f"当前副本已满（{occupied}/{limits['maximum']}）"
                    )

                now = utc_now()
                player = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            now,
                            now,
                        ),
                    )
                else:
                    player_id = player["id"]
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, enabled = 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (display_name, now, player_id),
                    )

                participant_id = (
                    existing["id"]
                    if reusable_cancelled
                    else new_id("participant")
                )
                if reusable_cancelled:
                    connection.execute(
                        """
                        UPDATE card_binding_codes SET status = 'expired'
                        WHERE participant_id = ?
                          AND status IN ('active', 'used')
                        """,
                        (participant_id,),
                    )
                    connection.execute(
                        """
                        UPDATE participants SET player_id = ?, display_name = ?,
                            private_user_id = '', private_origin = '', aliases_json = '[]',
                            character_card_id = NULL, character_version_id = NULL,
                            character_name = '', character_code = '',
                            card_status = 'uncreated', ready = 0, action_locked = 0,
                            participation_status = 'reserved', seat_reserved_at = ?,
                            joined_round = 1, consecutive_timeouts = 0,
                            exit_reason = '', updated_at = ?
                        WHERE id = ?
                        """,
                        (player_id, display_name, now, now, participant_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO participants(
                            id, session_id, player_id, group_user_id,
                            display_name, aliases_json, card_status, ready,
                            participation_status, seat_reserved_at, joined_round,
                            consecutive_timeouts, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, '[]', 'uncreated', 0,
                                  'reserved', ?, 1, 0, ?, ?)
                        """,
                        (participant_id, session_id, player_id, user_id,
                         display_name, now, now, now),
                    )
                template = card_template(world)
                time_rules = normalize_time_rules(
                    json_load(config_row["time_rules_json"], {})
                )
                draft_id = new_id("draft")
                draft_expires_at = deadline_after(
                    time_rules["card_draft_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO character_card_drafts(
                        id, participant_id, template_version, fields_json,
                        current_step, status, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, '{}', 0, 'active', ?, ?, ?)
                    ON CONFLICT(participant_id) DO UPDATE SET
                        template_version = excluded.template_version,
                        fields_json = '{}', current_step = 0, status = 'active',
                        expires_at = excluded.expires_at,
                        created_at = excluded.created_at, updated_at = excluded.updated_at
                    """,
                    (
                        draft_id,
                        participant_id,
                        template["version"],
                        draft_expires_at,
                        now,
                        now,
                    ),
                )
                code = ""
                for _ in range(20):
                    candidate = secrets.token_hex(3).upper()
                    if not connection.execute(
                        "SELECT 1 FROM card_binding_codes WHERE code = ?",
                        (candidate,),
                    ).fetchone():
                        code = candidate
                        break
                if not code:
                    raise RuntimeError("无法生成唯一建卡码")
                code_expires_at = deadline_after(
                    time_rules["card_code_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO card_binding_codes(
                        id, participant_id, code, status, expires_at,
                        created_at
                    ) VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        new_id("cardcode"),
                        participant_id,
                        code,
                        code_expires_at,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_code",
                    timeout_seconds=time_rules["card_code_ttl_seconds"],
                    reminder_seconds=None,
                    action={"code": code},
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_completion",
                    timeout_seconds=time_rules[
                        "card_completion_timeout_seconds"
                    ],
                    reminder_seconds=None,
                    action={
                        "timeout_action": time_rules[
                            "card_timeout_action"
                        ]
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.reserve",
                    participant_id,
                    {
                        "occupied": occupied + 1,
                        "maximum": limits["maximum"],
                    },
                )
                row = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result.update(
                    {
                        "joined": True,
                        "binding_code": code,
                        "binding_expires_at": code_expires_at,
                        "limits": limits,
                    }
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _create_timer(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        timer_type: str,
        timeout_seconds: int | None,
        reminder_seconds: int | None,
        action: Mapping[str, Any],
    ) -> str:
        timer_id = new_id("timer")
        now_dt = datetime.now(timezone.utc)
        action_payload = dict(action)
        reminder_interval = (
            CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
            if timer_type == "card_completion"
            else reminder_seconds
        )
        # 先作废同范围内仍存活的旧计时器，保证「一个范围一个计时器」。
        # 缺少这一步时，重复的 继续 / 读档 / 回合推进 会让 timer_instances
        # 里堆积多条 active 行，process_due_timers 便会一次性吐出多条提醒。
        supersede_sql = """
            UPDATE timer_instances
            SET status = 'cancelled', deadline_at = '',
                reminder_at = '', reminder_sent = 0, updated_at = ?
            WHERE session_id = ? AND timer_type = ?
              AND status IN ('active', 'paused')
        """
        supersede_args: tuple[Any, ...] = (
            now_dt.isoformat(timespec="seconds"),
            session_id,
            timer_type,
        )
        if timer_type not in SESSION_SINGLETON_TIMER_TYPES:
            supersede_sql += " AND participant_id = ?"
            supersede_args += (participant_id,)
        connection.execute(supersede_sql, supersede_args)
        policy = connection.execute(
            """
            SELECT global_enabled, switches_json FROM timer_policies
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        switches = json_load(
            policy["switches_json"] if policy else "",
            {},
        )
        switches = switches if isinstance(switches, Mapping) else {}
        countdown_enabled = bool(
            (policy["global_enabled"] if policy else 0)
            and switches.get(timer_type, False)
        )
        if not countdown_enabled:
            action_payload["paused_by_policy"] = True
        if timer_type == "card_completion":
            action_payload.setdefault("reminder_enabled", False)
            action_payload["reminder_interval_seconds"] = (
                CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
            )
        elif countdown_enabled:
            reminder_interval = (
                reminder_interval
                if reminder_interval is not None
                else TIMER_REMINDER_INTERVAL_SECONDS
            )
            action_payload["reminder_interval_seconds"] = reminder_interval
        deadline = (
            now_dt + timedelta(seconds=timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        # Reminder values mean "seconds before deadline" and fire once.
        reminder = (
            deadline - timedelta(seconds=reminder_interval)
            if deadline is not None
            and reminder_interval is not None
            and timeout_seconds > reminder_interval
            and timer_reminder_enabled(timer_type, action_payload)
            else None
        )
        status = "active" if countdown_enabled else "paused"
        now = now_dt.isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO timer_instances(
                id, session_id, participant_id, timer_type, status,
                deadline_at, remaining_seconds, reminder_at,
                reminder_sent, action_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                timer_id,
                session_id,
                participant_id,
                timer_type,
                status,
                (
                    deadline.isoformat(timespec="seconds")
                    if deadline is not None and countdown_enabled
                    else ""
                ),
                timeout_seconds,
                (
                    reminder.isoformat(timespec="seconds")
                    if reminder is not None and countdown_enabled
                    else ""
                ),
                json_dump(action_payload),
                now,
                now,
            ),
        )
        return timer_id

    def _start_standby_timer(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
    ) -> str | None:
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
        timeout_seconds = rules["standby_timeout_seconds"]
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'standby'
              AND status IN ('active', 'paused')
            """,
            (utc_now(), session_id, participant_id),
        )
        if timeout_seconds is None:
            return None
        return self._create_timer(
            connection,
            session_id=session_id,
            participant_id=participant_id,
            timer_type="standby",
            timeout_seconds=timeout_seconds,
            reminder_seconds=None,
            action={"reason": "standby_timeout"},
        )

    async def bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._bind_card_code,
            code,
            private_user_id,
            private_origin,
        )

    def _bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        normalized_code = str(code or "").strip().upper()
        private_user_id = validate_platform_id(
            private_user_id,
            label="私聊用户 ID",
        )
        private_origin = clean_text(private_origin, max_chars=500)
        if not normalized_code:
            raise ValueError("建卡码不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                code_row = connection.execute(
                    """
                    SELECT c.*, pt.session_id
                    FROM card_binding_codes c
                    JOIN participants pt ON pt.id = c.participant_id
                    WHERE c.code = ?
                    """,
                    (normalized_code,),
                ).fetchone()
                if not code_row or code_row["status"] != "active":
                    raise ValueError("建卡码不存在或已使用")
                # 空串表示「不限时」，不参与过期判定。
                if code_row["expires_at"] and code_row["expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE card_binding_codes
                        SET status = 'expired' WHERE id = ?
                        """,
                        (code_row["id"],),
                    )
                    config = connection.execute(
                        "SELECT time_rules_json FROM instance_configs WHERE session_id = ?",
                        (code_row["session_id"],),
                    ).fetchone()
                    time_rules = normalize_time_rules(
                        json_load(config["time_rules_json"] if config else "", {})
                    )
                    replacement = ""
                    for _ in range(20):
                        candidate = secrets.token_hex(3).upper()
                        if not connection.execute(
                            "SELECT 1 FROM card_binding_codes WHERE code = ?",
                            (candidate,),
                        ).fetchone():
                            replacement = candidate
                            break
                    if not replacement:
                        raise RuntimeError("无法生成唯一建卡码")
                    expires_at = deadline_after(time_rules["card_code_ttl_seconds"])
                    connection.execute(
                        """
                        INSERT INTO card_binding_codes(
                            id, participant_id, code, status, expires_at, created_at
                        ) VALUES (?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            new_id("cardcode"), code_row["participant_id"],
                            replacement, expires_at, now,
                        ),
                    )
                    self._create_timer(
                        connection,
                        session_id=code_row["session_id"],
                        participant_id=code_row["participant_id"],
                        timer_type="card_code",
                        timeout_seconds=time_rules["card_code_ttl_seconds"],
                        reminder_seconds=None,
                        action={"code": replacement, "renewed": True},
                    )
                    self._insert_audit(
                        connection,
                        code_row["session_id"],
                        private_user_id,
                        "card.code_reissued",
                        code_row["participant_id"],
                        {"reason": "expired_code_submitted"},
                    )
                    connection.execute("COMMIT")
                    return {
                        "session_id": code_row["session_id"],
                        "participant_id": code_row["participant_id"],
                        "binding_code_reissued": True,
                        "binding_code": replacement,
                        "binding_expires_at": expires_at,
                    }

                conflict = connection.execute(
                    """
                    SELECT pt.id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND pt.id <> ?
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                      AND pt.participation_status
                          NOT IN ('retired', 'archived')
                    LIMIT 1
                    """,
                    (private_origin, code_row["participant_id"]),
                ).fetchone()
                if conflict:
                    raise ValueError(
                        "当前私聊还有另一张未完成的角色卡；"
                        "请先完成或取消后再绑定"
                    )

                connection.execute(
                    """
                    UPDATE participants SET
                        private_user_id = ?, private_origin = ?,
                        card_status = CASE
                            WHEN card_status = 'uncreated' THEN 'draft'
                            ELSE card_status
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["participant_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes SET
                        status = 'used', private_user_id = ?,
                        private_origin = ?, used_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'card_code'
                      AND status = 'active'
                    """,
                    (now, code_row["participant_id"]),
                )
                self._insert_audit(
                    connection,
                    code_row["session_id"],
                    private_user_id,
                    "card.bind_private",
                    code_row["participant_id"],
                    {"private_origin_recorded": bool(private_origin)},
                )
                row = connection.execute(
                    """
                    SELECT pt.*, d.current_step AS draft_step,
                           d.status AS draft_status,
                           d.expires_at AS draft_expires_at
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    WHERE pt.id = ?
                    """,
                    (code_row["participant_id"],),
                ).fetchone()
                config = connection.execute(
                    """
                    SELECT world_snapshot_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (code_row["session_id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result["template"] = card_template(
                    json_load(config["world_snapshot_json"], {})
                )
                result["world"] = json_load(
                    config["world_snapshot_json"], {}
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._card_draft_for_private,
            private_origin,
        )

    def _card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.*, d.fields_json, d.current_step,
                       d.status AS draft_status,
                       d.expires_at AS draft_expires_at,
                       ic.world_snapshot_json
                FROM participants pt
                JOIN character_card_drafts d
                  ON d.participant_id = pt.id
                JOIN instance_configs ic
                  ON ic.session_id = pt.session_id
                JOIN sessions s ON s.id = pt.session_id
                WHERE pt.private_origin = ? AND d.status = 'active'
                  AND s.state <> 'finished'
                ORDER BY d.updated_at DESC LIMIT 1
                """,
                (private_origin,),
            ).fetchone()
            if not row:
                return None
            result = self._participant(row)
            result["fields"] = json_load(row["fields_json"], {})
            result["current_step"] = row["current_step"]
            result["template"] = card_template(
                json_load(row["world_snapshot_json"], {})
            )
            result["world"] = json_load(row["world_snapshot_json"], {})
            return result

    async def set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_card_completion_reminder,
            private_origin,
            enabled,
        )

    def _set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        private_origin = clean_text(private_origin, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT t.*, pt.private_user_id
                    FROM timer_instances t
                    JOIN participants pt ON pt.id = t.participant_id
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND t.timer_type = 'card_completion'
                      AND t.status IN ('active', 'paused')
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡创建倒计时"
                    )

                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, dict):
                    payload = {}
                current_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                desired = (
                    current_enabled if enabled is None else bool(enabled)
                )
                reminder_at = str(row["reminder_at"] or "")
                deadline_text = str(row["deadline_at"] or "")
                if enabled is not None:
                    payload["reminder_enabled"] = desired
                    payload["reminder_interval_seconds"] = (
                        CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                    )
                    if row["status"] == "active" and deadline_text:
                        deadline = datetime.fromisoformat(deadline_text)
                        if desired:
                            next_reminder = now_dt + timedelta(
                                seconds=(
                                    CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                                )
                            )
                            reminder_at = (
                                next_reminder.isoformat(timespec="seconds")
                                if next_reminder < deadline
                                else ""
                            )
                        else:
                            # Keep the expiry event active while preventing
                            # periodic reminder scans before the deadline.
                            reminder_at = deadline_text
                    else:
                        reminder_at = ""
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET action_json = ?, reminder_at = ?,
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(payload),
                            reminder_at,
                            now,
                            row["id"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        str(row["private_user_id"] or ""),
                        "card.reminder_toggle",
                        row["participant_id"],
                        {"enabled": desired},
                    )

                remaining_seconds: int | None
                if row["status"] == "active" and deadline_text:
                    remaining_seconds = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(deadline_text)
                                - now_dt
                            ).total_seconds()
                        ),
                    )
                elif row["status"] == "paused":
                    remaining_seconds = max(
                        0,
                        int(row["remaining_seconds"] or 0),
                    )
                else:
                    remaining_seconds = None
                connection.execute("COMMIT")
                return {
                    "timer_id": row["id"],
                    "session_id": row["session_id"],
                    "participant_id": row["participant_id"],
                    "enabled": desired,
                    "status": row["status"],
                    "remaining_seconds": remaining_seconds,
                    "has_deadline": bool(
                        deadline_text
                        or (
                            row["status"] == "paused"
                            and row["remaining_seconds"] is not None
                        )
                    ),
                    "next_reminder_at": reminder_at,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fill_card_draft(
        self,
        private_origin: str,
        value: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._fill_card_draft,
            private_origin,
            value,
            source_event_id,
        )

    def _allowed_option_values(
        self, definition: Mapping[str, Any]
    ) -> set[str]:
        result: set[str] = set()
        for option in definition.get("options") or []:
            if isinstance(option, Mapping):
                value = option.get("value")
            else:
                value = option
            text = str(value or "")
            if text:
                result.add(text)
        return result

    def _fill_card_draft(
        self,
        private_origin: str,
        value: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有进行中的建卡流程")
                now = utc_now()
                if row["draft_expires_at"] and row["draft_expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields_def = template["fields"]
                step = min(max(0, int(row["current_step"])), len(fields_def))
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                source_event_id = clean_text(source_event_id, max_chars=160)
                if (
                    source_event_id
                    and str(fields.get(LAST_MESSAGE_KEY) or "")
                    == source_event_id
                ):
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": step,
                        "complete": step >= len(fields_def),
                        "world": json_load(row["world_snapshot_json"], {}),
                        "duplicate": True,
                    }
                # Repair legacy drafts that still carry hand-filled stat_* fields
                # (doc §7): recompute from profession + primary/secondary and fix
                # the cursor to the first non-attribute field.
                fields, step = repair_profession_preset_draft(
                    template, fields, step
                )
                if uses_preset_stack_stats(template):
                    sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=False,
                    )
                stack_was_resolved = bool(
                    fields.get(STAT_GENERATION_SNAPSHOT_KEY)
                )
                if step >= len(fields_def):
                    raise ValueError("所有字段已填写，请发送 /酒馆 确认建卡")
                definition = fields_def[step]
                options = preset_options(template, definition, fields)
                if options and navigate_page(
                    fields,
                    definition,
                    value,
                    total_options=len(options),
                ):
                    if source_event_id:
                        fields[LAST_MESSAGE_KEY] = source_event_id
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET fields_json = ?, updated_at = ? WHERE id = ?
                        """,
                        (json_dump(fields), now, row["draft_id"]),
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": step,
                        "complete": False,
                        "world": json_load(row["world_snapshot_json"], {}),
                        "page_changed": True,
                    }
                multi_presets = (
                    choose_options(template, definition, fields, value)
                    if options and definition.get("type") == "multi_select"
                    else []
                )
                selected_preset = (
                    choose_option(template, definition, fields, value)
                    if options and definition.get("type") != "multi_select"
                    else None
                )
                raw_value = (
                    selected_preset["value"]
                    if selected_preset
                    else value
                )
                if definition.get("type") == "multi_select" and options:
                    stored_value = [str(item["value"]) for item in multi_presets]
                    if definition["required"] and not stored_value:
                        raise ValueError(f"{definition['label']}不能为空")
                    text = "、".join(stored_value)
                else:
                    text = clean_card_field(
                        raw_value,
                        label=str(definition["label"]),
                        max_chars=int(definition["max_chars"]),
                    )
                    if definition["required"] and not text:
                        raise ValueError(f"{definition['label']}不能为空")
                    stored_value = text
                if definition.get("type") == "integer":
                    try:
                        stored_value = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            f"{definition['label']}必须填写整数"
                        ) from exc
                    minimum = int(definition.get("minimum", -100))
                    maximum = int(definition.get("maximum", 100))
                    allocation = card_stat_allocation(
                        template,
                        fields,
                        step,
                    )
                    current_stat = allocation.get("current")
                    if isinstance(current_stat, Mapping):
                        maximum = int(
                            current_stat["effective_maximum"]
                        )
                        if maximum < minimum:
                            raise ValueError(
                                "当前属性预算无法满足模板最低值，"
                                "请让管理员检查角色卡模板"
                            )
                    if not minimum <= stored_value <= maximum:
                        suffix = ""
                        if isinstance(current_stat, Mapping):
                            suffix = (
                                f"（总预算 {allocation['budget']}，"
                                f"已使用 {current_stat['used_before']}，"
                                f"后续至少预留 "
                                f"{current_stat['reserved_minimum']}）"
                            )
                        raise ValueError(
                            f"{definition['label']}当前必须在 "
                            f"{minimum}—{maximum} 之间{suffix}"
                        )
                profession_mode = uses_profession_preset_stats(template)
                preset_stack_mode = uses_preset_stack_stats(template)
                field_key = str(definition["key"])
                if (profession_mode or preset_stack_mode) and field_key.startswith("stat_"):
                    raise ValueError(
                        "本世界的属性由预设自动生成，不支持手动填写。"
                    )
                previous_value = fields.get(field_key)
                if previous_value is not None and previous_value != stored_value:
                    clear_field_and_dependents(template, fields, field_key)
                different_from = str(definition.get("must_differ_from") or "")
                if different_from and fields.get(different_from) == stored_value:
                    raise ValueError(
                        f"{definition['label']}不能与"
                        f"{next((item.get('label') for item in fields_def if item.get('key') == different_from), different_from)}相同"
                    )
                fields[field_key] = stored_value
                if selected_preset:
                    store_preset_snapshot(fields, field_key, selected_preset)
                elif multi_presets:
                    store_preset_snapshots(fields, field_key, multi_presets)
                if template.get("preset_dimensions"):
                    validate_preset_selection(
                        template,
                        fields,
                        require_complete=False,
                    )
                stack_resolved = None
                if profession_mode and field_key == "profession":
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    fields.pop("primary_attribute", None)
                    fields.pop("secondary_attribute", None)
                elif profession_mode and field_key == "primary_attribute":
                    if fields.get("secondary_attribute") == fields.get(
                        "primary_attribute"
                    ):
                        fields.pop("secondary_attribute", None)
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                elif profession_mode and field_key == "secondary_attribute":
                    if fields.get("primary_attribute") == fields.get(
                        "secondary_attribute"
                    ):
                        raise ValueError("副属性不能与主属性相同")
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=True
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                    fields["resolved_stat_total"] = int(resolved["effective_total"])
                if preset_stack_mode:
                    stack_resolved = sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=False,
                    )
                if profession_mode or preset_stack_mode:
                    next_step = next_fillable_card_step(
                        template, fields_def, step + 1, fields
                    )
                else:
                    stat_values = [
                        int(fields[f"stat_{item['key']}"])
                        for item in template["stats"]["attributes"]
                        if f"stat_{item['key']}" in fields
                    ]
                    if (
                        len(stat_values)
                        == len(template["stats"]["attributes"])
                        and sum(stat_values)
                        > int(template["stats"]["budget"])
                    ):
                        raise ValueError(
                            f"属性总值 {sum(stat_values)} 超过预算 "
                            f"{template['stats']['budget']}，"
                            "请重新建卡或调整模板"
                        )
                    next_step = next_fillable_card_step(
                        template, fields_def, step + 1, fields
                    )
                if source_event_id:
                    fields[LAST_MESSAGE_KEY] = source_event_id
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        next_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.field_update",
                    row["id"],
                    {
                        "field": definition["key"],
                        "step": next_step,
                    },
                )
                connection.execute("COMMIT")
                result = {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": next_step,
                    "complete": next_step >= len(fields_def),
                    "world": json_load(row["world_snapshot_json"], {}),
                }
                if stack_resolved is not None and (
                    not stack_was_resolved
                    or field_key
                    in stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                ):
                    result["stat_generation_result"] = stack_resolved
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reset_card_draft_stats,
            private_origin,
        )

    def _reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有可调整的角色卡"
                    )
                now = utc_now()
                if (
                    row["draft_expires_at"]
                    and row["draft_expires_at"] <= now
                ):
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                world_obj = json_load(row["world_snapshot_json"], {})
                if uses_preset_stack_stats(template):
                    sources = stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                    raise ValueError(
                        "本世界的属性已由预设锁定；请使用 /酒馆 修改 "
                        + "、/酒馆 修改 ".join(str(item) for item in sources)
                        + " 调整属性来源"
                    )
                if uses_profession_preset_stats(template):
                    profession_name = str(fields.get("profession") or "")
                    if not profession_name:
                        raise ValueError("当前角色还没有选择职业")
                    # Keep profession, base stats and all text fields; only clear
                    # the primary/secondary choices and the derived total.
                    fields.pop("primary_attribute", None)
                    fields.pop("secondary_attribute", None)
                    fields.pop("resolved_stat_total", None)
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    primary_step = next(
                        index
                        for index, _d in enumerate(
                            template.get("fields") or []
                        )
                        if isinstance(_d, Mapping)
                        and str(_d.get("key") or "") == "primary_attribute"
                    )
                    target_step = primary_step
                    connection.execute(
                        """
                        UPDATE character_card_drafts SET
                            fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(fields), target_step, now, row["draft_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE participants
                        SET card_status = 'draft', ready = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        row["private_user_id"],
                        "card.stats_reset",
                        row["id"],
                        {
                            "profession_reset": True,
                            "profession": profession_name,
                        },
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": target_step,
                        "complete": False,
                        "profession_reset": True,
                        "profession": profession_name,
                        "base_stats": dict(resolved["base"]),
                        "world": world_obj,
                    }
                allocation = card_stat_allocation(template, fields)
                stat_fields = allocation["stat_fields"]
                if not stat_fields:
                    raise ValueError("当前角色卡模板没有可分配数值")
                first_step = int(allocation["first_step"])
                has_stat_values = any(
                    item["field_key"] in fields
                    for item in stat_fields
                )
                if (
                    int(row["current_step"]) < first_step
                    and not has_stat_values
                ):
                    raise ValueError("尚未开始填写角色数值")
                removed = []
                for item in stat_fields:
                    field_key = str(item.get("field_key") or "")
                    if field_key in fields:
                        removed.append(field_key)
                        fields.pop(field_key, None)
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        first_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.stats_reset",
                    row["id"],
                    {"removed_fields": removed},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": first_step,
                    "complete": False,
                    "world": json_load(row["world_snapshot_json"], {}),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def preview_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        draft = await self.card_draft_for_private(private_origin)
        if not draft:
            raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
        return draft

    async def previous_card_step(
        self, private_origin: str
    ) -> dict[str, Any]:
        return await self._run(
            self._reposition_card_draft, private_origin, ""
        )

    async def modify_card_field(
        self, private_origin: str, field_reference: str
    ) -> dict[str, Any]:
        return await self._run(
            self._reposition_card_draft,
            private_origin,
            field_reference,
        )

    def _reposition_card_draft(
        self, private_origin: str, field_reference: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d ON d.participant_id = pt.id
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡"
                    )
                world = json_load(row["world_snapshot_json"], {})
                template = card_template(world)
                definitions = template["fields"]
                fields = json_load(row["fields_json"], {})
                fields = fields if isinstance(fields, dict) else {}
                reference = str(field_reference or "").strip().casefold()
                if reference:
                    candidates = [
                        (index, item)
                        for index, item in enumerate(definitions)
                        if reference
                        in {
                            str(item.get("key") or "").casefold(),
                            str(item.get("label") or "").casefold(),
                            str(item.get("label") or "")
                            .removeprefix("选择")
                            .split("（", 1)[0]
                            .casefold(),
                        }
                    ]
                    if len(candidates) != 1:
                        raise ValueError(
                            "未找到唯一字段，请使用字段名称或稳定 key"
                        )
                    target_step, definition = candidates[0]
                else:
                    current = min(
                        max(0, int(row["current_step"])), len(definitions)
                    )
                    candidates = [
                        (index, item)
                        for index, item in enumerate(definitions[:current])
                        if field_visible(item, fields)
                    ]
                    if not candidates:
                        raise ValueError("已经是第一个建卡步骤")
                    target_step, definition = candidates[-1]
                if not field_visible(definition, fields):
                    raise ValueError("该字段在当前角色选择下不需要填写")
                field_key = str(definition.get("key") or "")
                clear_field_and_dependents(template, fields, field_key)
                if (
                    uses_preset_stack_stats(template)
                    and field_key
                    in stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                ):
                    clear_generated_stats(template, fields)
                if field_key == "profession":
                    fields.pop("profession_base_stats", None)
                    fields.pop("resolved_stat_total", None)
                    for attribute in template.get("stats", {}).get(
                        "attributes", []
                    ):
                        fields.pop(f"stat_{attribute.get('key')}", None)
                    fields.pop("primary_attribute", None)
                    fields.pop("secondary_attribute", None)
                fields.pop(LAST_MESSAGE_KEY, None)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(fields), target_step, now, row["draft_id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.step_reposition",
                    row["id"],
                    {"field": field_key, "step": target_step},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": target_step,
                    "complete": False,
                    "world": world,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def confirm_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._confirm_card_draft,
            private_origin,
        )

    def _confirm_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           ic.world_snapshot_json, ic.time_rules_json,
                           ic.world_revision
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有可确认的角色卡")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                fields.pop("_alloc", None)
                fields.pop("_wizard_pages", None)
                fields.pop(LAST_MESSAGE_KEY, None)
                for definition in template["fields"]:
                    key = str(definition["key"])
                    if key not in fields:
                        continue
                    if isinstance(fields[key], list):
                        minimum = int(definition.get("min_choices", 0) or 0)
                        maximum = int(definition.get("max_choices", 100) or 100)
                        if not minimum <= len(fields[key]) <= maximum:
                            raise ValueError(
                                f"{definition['label']}必须选择 {minimum}—{maximum} 项"
                            )
                        continue
                    if key in {"name", "code"}:
                        clean_card_field(
                            fields[key],
                            label=str(definition["label"]),
                            max_chars=12,
                        )
                    elif len(str(fields[key])) > int(definition["max_chars"]):
                        raise ValueError(
                            f"{definition['label']}超过 "
                            f"{int(definition['max_chars'])} 字符上限"
                        )
                missing = [
                    item["label"]
                    for item in template["fields"]
                    if item["required"]
                    and field_visible(item, fields)
                    and not str(
                        fields.get(item["key"], "")
                    ).strip()
                ]
                if missing:
                    raise ValueError("尚未填写：" + "、".join(missing))
                if template.get("preset_dimensions"):
                    validate_preset_selection(
                        template,
                        fields,
                        require_complete=True,
                    )
                    fields["_resolved_boundaries"] = resolve_character_presets(
                        world_snapshot,
                        fields,
                    )
                character_name = clean_card_field(
                    fields.get("name") or row["display_name"],
                    label="角色姓名",
                    max_chars=12,
                )
                character_code = clean_card_field(
                    fields.get("code") or character_name,
                    label="副本代号",
                    max_chars=12,
                )
                if not character_name or not character_code:
                    raise ValueError("角色姓名与副本代号不能为空")
                duplicate = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND id <> ?
                      AND participation_status NOT IN ('retired', 'archived')
                      AND (
                           lower(character_name) = lower(?)
                        OR lower(character_code) = lower(?)
                      )
                    LIMIT 1
                    """,
                    (
                        row["session_id"],
                        row["id"],
                        character_name,
                        character_code,
                    ),
                ).fetchone()
                if duplicate:
                    raise ValueError("角色姓名或副本代号已被使用")
                stat_definition = template["stats"]
                if uses_preset_stack_stats(template):
                    calculated_stats = calculate_preset_stack_stats(
                        template,
                        fields,
                        require_complete=True,
                    )
                    assert calculated_stats is not None
                    for key, expected_value in calculated_stats["raw"].items():
                        field_name = f"stat_{key}"
                        if field_name in fields and int(fields[field_name]) != expected_value:
                            raise ValueError(
                                f"{calculated_stats['labels'][key]}数值与预设来源不一致"
                            )
                    resolved_stats = sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=True,
                    )
                    assert resolved_stats is not None
                elif uses_profession_preset_stats(template):
                    resolved_stats = resolve_profession_stats(
                        template,
                        fields,
                        require_complete=True,
                    )
                    for key, expected_value in resolved_stats[
                        "raw"
                    ].items():
                        actual_value = int(
                            fields.get(f"stat_{key}", -999)
                        )
                        if actual_value != expected_value:
                            raise ValueError(
                                f"{resolved_stats['labels'][key]}"
                                "数值与职业基础属性及主副属性加成不一致，"
                                "请使用「重填数值」重新生成"
                            )
                    final_total = int((stat_definition.get("total_validation") or {}).get("final_total", stat_definition.get("budget", 0)))
                    if int(resolved_stats["effective_total"]) != final_total:
                        raise ValueError(f"角色最终属性总和必须为{final_total}")
                    resolved_stats["budget"] = final_total
                    fields["profession_base_stats"] = dict(
                        resolved_stats["base"]
                    )
                    fields["resolved_stat_total"] = int(
                        resolved_stats["effective_total"]
                    )
                else:
                    raw_stats: dict[str, int] = {}
                    labels: dict[str, str] = {}
                    modifiers: dict[str, int] = {}
                    for attribute in stat_definition["attributes"]:
                        key = str(attribute["key"])
                        value = int(
                            fields.get(
                                f"stat_{key}",
                                attribute.get("default", 0),
                            )
                        )
                        if not int(attribute["minimum"]) <= value <= int(
                            attribute["maximum"]
                        ):
                            raise ValueError(
                                f"{attribute['label']}超出模板允许范围"
                            )
                        raw_stats[key] = value
                        labels[key] = str(attribute["label"])
                        modifiers[key] = int(
                            stat_definition["modifier_table"].get(
                                str(value),
                                0,
                            )
                        )
                    allocation = card_stat_allocation(template, fields)
                    if not allocation.get("total_ok", True):
                        rule = allocation.get("allocation_rule", "maximum")
                        if rule == "exact": raise ValueError("角色属性总值必须刚好等于世界模板预算")
                        if rule == "range": raise ValueError("角色属性总值不在允许区间")
                        raise ValueError("角色属性总值超过世界模板预算")
                    resolved_stats = {
                        "raw": raw_stats,
                        "labels": labels,
                        "modifiers": modifiers,
                        "budget": int(stat_definition["budget"]),
                        "modifier_table": dict(
                            stat_definition["modifier_table"]
                        ),
                    }
                now = utc_now()
                card_id = row["character_card_id"] or new_id("pcard")
                existing_card = connection.execute(
                    "SELECT * FROM character_cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
                if not existing_card:
                    version_no = 1
                    connection.execute(
                        """
                        INSERT INTO character_cards(
                            id, owner_user_id, world_id, display_name,
                            archived, deleted, current_version,
                            created_at, updated_at
                        )
                        SELECT ?, ?, s.world_id, ?, 0, 0, 1, ?, ?
                        FROM sessions s WHERE s.id = ?
                        """,
                        (
                            card_id,
                            row["group_user_id"],
                            character_name,
                            now,
                            now,
                            row["session_id"],
                        ),
                    )
                else:
                    version_no = int(existing_card["current_version"]) + 1
                    connection.execute(
                        """
                        UPDATE character_cards SET
                            display_name = ?, current_version = ?,
                            archived = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (character_name, version_no, now, card_id),
                    )
                status = (
                    CARD_APPROVED
                    if template["auto_approve"]
                    else CARD_PENDING
                )
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        version_id,
                        card_id,
                        version_no,
                        template["version"],
                        json_dump(fields),
                        json_dump(resolved_stats),
                        status,
                        "system" if status == CARD_APPROVED else "",
                        now,
                    ),
                )
                initial_runtime_state: dict[str, Any] = {}
                protocol = world_snapshot.get("protocol")
                protocol = protocol if isinstance(protocol, Mapping) else {}
                features = protocol.get("features")
                features = features if isinstance(features, Mapping) else {}
                if int(world_snapshot.get("world_schema_version", 0) or 0) >= 5 and "resources" in features:
                    resource_module = module_value(world_snapshot, "resources", {})
                    resource_module = resource_module if isinstance(resource_module, Mapping) else {}
                    definitions = resource_module.get("definitions", resource_module.get("items", []))
                    if isinstance(definitions, Sequence) and not isinstance(definitions, (str, bytes)):
                        refs: dict[str, Any] = {}
                        for definition in definitions:
                            if not isinstance(definition, Mapping):
                                continue
                            resource_id = str(definition.get("resource_id") or definition.get("id") or "")
                            if resource_id and "initial_value" in definition:
                                refs[f"resource:{resource_id}"] = definition["initial_value"]
                        if refs:
                            initial_runtime_state["refs"] = refs
                if int(world_snapshot.get("world_schema_version", 0) or 0) >= 5 and "capabilities" in features:
                    registry = EntityRegistry(world_snapshot)
                    service = CapabilityService(world_snapshot, registry)
                    preset_values: dict[str, Any] = {}
                    preset_refs = fields.get("_preset_refs", {})
                    if isinstance(preset_refs, Mapping):
                        for dimension, selected in preset_refs.items():
                            if isinstance(selected, Mapping):
                                preset_values[f"custom:preset.{dimension}"] = str(
                                    selected.get("id")
                                    or selected.get("snapshot", {}).get("id")
                                    or ""
                                )
                    actor_ref = f"character:{row['id']}"
                    migration_operation_id = (
                        f"card_capabilities:{row['session_id']}:{row['id']}:{row['world_revision']}"
                    )
                    grants = service.initial_grants(preset_values)
                    granted: list[str] = []
                    for grant in grants:
                        capability_ref = str(grant.get("capability_ref") or grant.get("target_ref") or "")
                        source_ref = str(grant.get("source_ref") or "character_card")
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO actor_capability_instances(
                                id, session_id, actor_ref, capability_ref,
                                definition_version, source_ref, state_json,
                                persistence_scope, available, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 1, ?, '{}', ?, 1, ?, ?)
                            """,
                            (
                                new_id("capability_instance"), row["session_id"], actor_ref,
                                capability_ref, source_ref,
                                str(grant.get("persistence_scope") or "campaign"), now, now,
                            ),
                        )
                        granted.append(capability_ref)
                    migration_payload = {
                        "character_card_version_id": version_id,
                        "world_revision": int(row["world_revision"]),
                        "actor_ref": actor_ref,
                        "preset_values": preset_values,
                        "granted_capabilities": granted,
                    }
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO migration_receipts(
                            id, migration_type, source_version, target_version,
                            session_id, operation_id, receipt_json, confirmed_by, created_at
                        ) VALUES (?, 'character_capabilities', ?, 'v5', ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("migration"), str(template.get("version") or ""),
                            row["session_id"], migration_operation_id,
                            json_dump(migration_payload), row["private_user_id"], now,
                        ),
                    )
                participation_status = (
                    PARTICIPANT_ACTIVE
                    if status == CARD_APPROVED
                    else PARTICIPANT_RESERVED
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        character_card_id = ?, character_version_id = ?,
                        character_name = ?, character_code = ?,
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        card_id,
                        version_id,
                        character_name,
                        character_code,
                        status,
                        participation_status,
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'confirmed', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE players SET
                        character_name = ?, profile_json = ?,
                        enabled = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        character_name,
                        json_dump(fields),
                        now,
                        row["player_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO character_runtime_states(
                        id, session_id, participant_id, character_card_id,
                        state_json, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id, participant_id) DO UPDATE SET
                        character_card_id = excluded.character_card_id,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("runtime"),
                        row["session_id"],
                        row["id"],
                        card_id,
                        json_dump(initial_runtime_state),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ?
                      AND timer_type = 'card_completion'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if status == CARD_APPROVED:
                    time_rules = normalize_time_rules(
                        json_load(row["time_rules_json"], {})
                    )
                    self._create_timer(
                        connection,
                        session_id=row["session_id"],
                        participant_id=row["id"],
                        timer_type="ready",
                        timeout_seconds=time_rules["ready_timeout_seconds"],
                        reminder_seconds=None,
                        action={
                            "timeout_action": time_rules[
                                "ready_timeout_action"
                            ]
                        },
                    )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.confirm",
                    row["id"],
                    {
                        "version": version_no,
                        "status": status,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["auto_approved"] = status == CARD_APPROVED
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cancel_card_draft(
        self,
        private_origin: str,
    ) -> None:
        await self._run(self._cancel_card_draft, private_origin)

    def _cancel_card_draft(self, private_origin: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前没有进行中的角色卡")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET participation_status = 'archived',
                        card_status = 'uncreated', ready = 0,
                        exit_reason = 'cancelled_card', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes
                    SET status = 'expired'
                    WHERE participant_id = ? AND status IN ('active', 'used')
                    """,
                    (row["id"],),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', deadline_at = '',
                        reminder_at = '', reminder_sent = 0, updated_at = ?
                    WHERE participant_id = ?
                      AND timer_type IN ('card_code', 'card_completion')
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.cancel",
                    row["id"],
                    {},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._review_character_card,
            session_id,
            participant_ref,
            approved,
            actor_id,
            note,
        )

    def _review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        participant = self._get_participant(
            session_id,
            "",
            participant_ref,
            True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                if not row or not row["character_version_id"]:
                    raise ValueError("该玩家尚未提交角色卡")
                status = CARD_APPROVED if approved else CARD_REJECTED
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_versions SET
                        status = ?, review_note = ?, reviewed_by = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        clean_text(note, max_chars=500),
                        actor_id,
                        row["character_version_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        (
                            PARTICIPANT_ACTIVE
                            if approved
                            else PARTICIPANT_RESERVED
                        ),
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if approved:
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
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id=row["id"],
                        timer_type="ready",
                        timeout_seconds=time_rules["ready_timeout_seconds"],
                        reminder_seconds=None,
                        action={
                            "timeout_action": time_rules[
                                "ready_timeout_action"
                            ]
                        },
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "card.review",
                    row["id"],
                    {"approved": approved, "note": note[:500]},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def request_card_revision(
        self,
        session_id: str,
        participant_ref: str,
        profile_patch: Mapping[str, Any],
        stats_patch: Mapping[str, Any],
        requester_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._request_card_revision,
            session_id,
            participant_ref,
            dict(profile_patch),
            dict(stats_patch),
            requester_id,
            note,
        )

    def _request_card_revision(
        self,
        session_id: str,
        participant_ref: str,
        profile_patch: dict[str, Any],
        stats_patch: dict[str, Any],
        requester_id: str,
        note: str,
    ) -> dict[str, Any]:
        participant = self._get_participant(
            session_id, "", participant_ref, True
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT pt.*, ccv.profile_json, ccv.stats_json,
                           ccv.version_no, ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_versions ccv
                      ON ccv.id = pt.character_version_id
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    WHERE pt.id = ?
                    """,
                    (participant["id"],),
                ).fetchone()
                if not row or not row["character_card_id"]:
                    raise ValueError("该玩家没有可修订的有效角色卡")
                pending = connection.execute(
                    """
                    SELECT id FROM card_revision_requests
                    WHERE participant_id = ? AND status = 'pending'
                    """,
                    (row["id"],),
                ).fetchone()
                if pending:
                    raise DatabaseConflictError("该角色已有待审核的修改申请")
                profile = json_load(row["profile_json"], {})
                profile = profile if isinstance(profile, dict) else {}
                profile.update(profile_patch)
                stats = json_load(row["stats_json"], {})
                stats = stats if isinstance(stats, dict) else {}
                stats.update(stats_patch)
                validated = validate_card_revision(
                    json_load(row["world_snapshot_json"], {}),
                    profile,
                    stats,
                )
                card = connection.execute(
                    "SELECT * FROM character_cards WHERE id = ?",
                    (row["character_card_id"],),
                ).fetchone()
                version_no = int(card["current_version"]) + 1
                now = utc_now()
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending_review', '', '', ?)
                    """,
                    (
                        version_id,
                        row["character_card_id"],
                        version_no,
                        validated["template_version"],
                        json_dump(validated["profile"]),
                        json_dump(validated["stats"]),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE character_cards SET current_version = ?, updated_at = ? WHERE id = ?",
                    (version_no, now, row["character_card_id"]),
                )
                request_id = new_id("cardedit")
                connection.execute(
                    """
                    INSERT INTO card_revision_requests(
                        id, session_id, participant_id, character_card_id,
                        base_version_id, candidate_version_id, status,
                        request_note, review_note, requested_by,
                        reviewed_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, '', ?, '', ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        row["id"],
                        row["character_card_id"],
                        row["character_version_id"],
                        version_id,
                        clean_text(note, max_chars=500),
                        requester_id,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    requester_id,
                    "card.revision.request",
                    request_id,
                    {"base_version": row["version_no"], "candidate_version": version_no},
                )
                connection.execute("COMMIT")
                return {
                    "id": request_id,
                    "session_id": session_id,
                    "participant_id": row["id"],
                    "base_version_id": row["character_version_id"],
                    "candidate_version_id": version_id,
                    "candidate_version": version_no,
                    "status": "pending",
                    "created_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def review_card_revision(
        self,
        request_id: str,
        approved: bool,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._review_card_revision,
            request_id,
            approved,
            actor_id,
            note,
        )

    def _review_card_revision(
        self,
        request_id: str,
        approved: bool,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM card_revision_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色卡修改申请不存在")
                if row["status"] != "pending":
                    raise DatabaseConflictError("该修改申请已经处理")
                self._assert_session_writable(connection, row["session_id"])
                status = "approved" if approved else "rejected"
                version_status = CARD_APPROVED if approved else CARD_REJECTED
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_versions
                    SET status = ?, review_note = ?, reviewed_by = ?
                    WHERE id = ?
                    """,
                    (
                        version_status,
                        clean_text(note, max_chars=500),
                        actor_id,
                        row["candidate_version_id"],
                    ),
                )
                if approved:
                    candidate = connection.execute(
                        "SELECT profile_json FROM character_card_versions WHERE id = ?",
                        (row["candidate_version_id"],),
                    ).fetchone()
                    profile = json_load(candidate["profile_json"], {})
                    connection.execute(
                        """
                        UPDATE participants SET character_version_id = ?,
                            character_name = ?, character_code = ?,
                            ready = 0, updated_at = ? WHERE id = ?
                        """,
                        (
                            row["candidate_version_id"],
                            str(profile.get("name") or "")[:12],
                            str(profile.get("code") or "")[:12],
                            now,
                            row["participant_id"],
                        ),
                    )

                    # A15：角色卡修订（含改名/改卡）批准后同步 players 表，
                    # 避免回合状态（get_turn_status 读取 players.character_name）
                    # 与行动选项（读取 participants.character_name）显示不一致。
                    connection.execute(
                        """
                        UPDATE players SET character_name = ?,
                            profile_json = ?, updated_at = ?
                        WHERE id = (
                            SELECT player_id FROM participants WHERE id = ?
                        )
                        """,
                        (
                            str(profile.get("name") or "")[:12],
                            json_dump(profile),
                            now,
                            row["participant_id"],
                        ),
                    )
                connection.execute(
                    """
                    UPDATE card_revision_requests SET status = ?,
                        review_note = ?, reviewed_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, clean_text(note, max_chars=500), actor_id, now, request_id),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "card.revision.review",
                    request_id,
                    {"approved": approved, "candidate_version_id": row["candidate_version_id"]},
                )
                connection.execute("COMMIT")
                return {
                    "id": request_id,
                    "session_id": row["session_id"],
                    "participant_id": row["participant_id"],
                    "status": status,
                    "candidate_version_id": row["candidate_version_id"],
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_card_revisions(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_card_revisions, session_id)

    def _list_card_revisions(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rr.*, pt.display_name, pt.character_name,
                       base.version_no AS base_version,
                       candidate.version_no AS candidate_version,
                       candidate.profile_json, candidate.stats_json
                FROM card_revision_requests rr
                JOIN participants pt ON pt.id = rr.participant_id
                JOIN character_card_versions base ON base.id = rr.base_version_id
                JOIN character_card_versions candidate ON candidate.id = rr.candidate_version_id
                WHERE rr.session_id = ? ORDER BY rr.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["profile"] = json_load(item.pop("profile_json"), {})
                item["stats"] = json_load(item.pop("stats_json"), {})
                result.append(item)
            return result

    async def set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_ready,
            session_id,
            user_id,
            ready,
        )

    async def force_all_ready(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._force_all_ready,
            session_id,
            actor_id,
        )

    def _force_all_ready(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session or session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有准备大厅可以强制全员准备"
                    )
                now = utc_now()
                eligible = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND card_status = 'approved'
                      AND participation_status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                ids = [str(row["id"]) for row in eligible]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""
                        UPDATE participants SET ready = 1, updated_at = ?
                        WHERE id IN ({placeholders})
                        """,
                        (now, *ids),
                    )
                    connection.execute(
                        f"""
                        UPDATE timer_instances
                        SET status = 'completed', deadline_at = '',
                            reminder_at = '', updated_at = ?
                        WHERE participant_id IN ({placeholders})
                          AND timer_type = 'ready'
                          AND status IN ('active', 'paused')
                        """,
                        (now, *ids),
                    )
                skipped = connection.execute(
                    """
                    SELECT display_name, character_name, card_status,
                           participation_status
                    FROM participants
                    WHERE session_id = ?
                      AND NOT (
                        card_status = 'approved'
                        AND participation_status = 'active'
                      )
                      AND participation_status NOT IN ('retired', 'archived')
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "participant.force_ready_all",
                    session_id,
                    {
                        "ready_count": len(ids),
                        "skipped_count": len(skipped),
                    },
                )
                connection.execute("COMMIT")
                return {
                    "session_id": session_id,
                    "ready_count": len(ids),
                    "skipped": [
                        {
                            "name": row["character_name"]
                            or row["display_name"],
                            "card_status": row["card_status"],
                            "participation_status": row[
                                "participation_status"
                            ],
                        }
                        for row in skipped
                    ],
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool,
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
                    raise InvalidTransitionError("只能在准备大厅确认准备")
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if row["card_status"] != CARD_APPROVED:
                    raise ValueError("角色卡尚未通过审核")
                if row["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能进入本次阵容")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        ready = ?, participation_status = 'active',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (int(bool(ready)), now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status = 'active'
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.ready",
                    row["id"],
                    {"ready": bool(ready)},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise
