"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *


class TimerRepositoryMixin:
    async def get_timer_policy(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_timer_policy, session_id)

    def _get_timer_policy(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            row = connection.execute(
                """
                SELECT * FROM timer_policies WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            switches = json_load(
                row["switches_json"] if row else "",
                {},
            )
            switches = switches if isinstance(switches, Mapping) else {}
            global_enabled = bool(row["global_enabled"] if row else False)
            return {
                "session_id": session_id,
                "global_enabled": global_enabled,
                "switches": {
                    timer_type: bool(switches.get(timer_type, False))
                    for timer_type in COUNTDOWN_TYPES
                },
                "effective": {
                    timer_type: bool(
                        global_enabled and switches.get(timer_type, False)
                    )
                    for timer_type in COUNTDOWN_TYPES
                },
                "revision": int(row["revision"] if row else 0),
                "updated_at": str(row["updated_at"] if row else ""),
            }

    async def set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_timer_policy,
            session_id,
            timer_type,
            enabled,
            actor_id,
        )

    def _set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        timer_type = str(timer_type or "").strip().lower()
        if timer_type not in {"all", *COUNTDOWN_TYPES}:
            raise ValueError("不支持的倒计时分类")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                row = connection.execute(
                    """
                    SELECT * FROM timer_policies WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                global_enabled = bool(
                    row["global_enabled"] if row else False
                )
                switches = json_load(
                    row["switches_json"] if row else "",
                    {},
                )
                switches = (
                    dict(switches)
                    if isinstance(switches, Mapping)
                    else {}
                )
                before = {
                    item: bool(
                        global_enabled and switches.get(item, False)
                    )
                    for item in COUNTDOWN_TYPES
                }
                if timer_type == "all":
                    global_enabled = bool(enabled)
                    for item in COUNTDOWN_TYPES:
                        switches[item] = bool(enabled)
                else:
                    switches[timer_type] = bool(enabled)
                after = {
                    item: bool(
                        global_enabled and switches.get(item, False)
                    )
                    for item in COUNTDOWN_TYPES
                }
                connection.execute(
                    """
                    INSERT INTO timer_policies(
                        session_id, global_enabled, switches_json,
                        revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        global_enabled = excluded.global_enabled,
                        switches_json = excluded.switches_json,
                        revision = timer_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        int(global_enabled),
                        json_dump(switches),
                        actor_id,
                        now,
                    ),
                )
                changed = [
                    item for item in COUNTDOWN_TYPES
                    if before[item] != after[item]
                ]
                if changed:
                    placeholders = ",".join("?" for _ in changed)
                    rows = connection.execute(
                        f"""
                        SELECT * FROM timer_instances
                        WHERE session_id = ?
                          AND timer_type IN ({placeholders})
                          AND status IN ('active', 'paused')
                        """,
                        (session_id, *changed),
                    ).fetchall()
                    for timer in rows:
                        payload = json_load(timer["action_json"], {})
                        payload = (
                            dict(payload)
                            if isinstance(payload, Mapping)
                            else {}
                        )
                        current_type = str(timer["timer_type"])
                        if not after[current_type]:
                            if timer["status"] != "active":
                                continue
                            remaining = timer["remaining_seconds"]
                            deadline = str(timer["deadline_at"] or "")
                            if deadline:
                                try:
                                    remaining = max(
                                        0,
                                        int(
                                            (
                                                datetime.fromisoformat(deadline)
                                                - now_dt
                                            ).total_seconds()
                                        ),
                                    )
                                except ValueError:
                                    pass
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'paused', deadline_at = '',
                                    remaining_seconds = ?, reminder_at = '',
                                    reminder_sent = 0, action_json = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    remaining,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                        elif (
                            timer["status"] == "paused"
                            and payload.get("paused_by_policy")
                        ):
                            if (
                                current_type != "card_completion"
                                and not payload.get(
                                    "reminder_interval_seconds"
                                )
                            ):
                                payload["reminder_interval_seconds"] = (
                                    TIMER_REMINDER_INTERVAL_SECONDS
                                )
                            seconds_left = max(
                                1,
                                int(timer["remaining_seconds"] or 1),
                            )
                            deadline_dt = now_dt + timedelta(
                                seconds=seconds_left
                            )
                            interval = timer_reminder_interval(
                                current_type,
                                payload,
                            )
                            next_reminder = deadline_dt - timedelta(
                                seconds=interval
                            )
                            payload.pop("paused_by_policy", None)
                            reminder_at = ""
                            if (
                                timer_reminder_enabled(
                                    current_type,
                                    payload,
                                )
                                and next_reminder < deadline_dt
                            ):
                                reminder_at = next_reminder.isoformat(
                                    timespec="seconds"
                                )
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'active', deadline_at = ?,
                                    reminder_at = ?, reminder_sent = 0,
                                    action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    deadline_dt.isoformat(
                                        timespec="seconds"
                                    ),
                                    reminder_at,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.policy",
                    timer_type,
                    {
                        "enabled": bool(enabled),
                        "changed_types": changed,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._get_timer_policy(session_id)

    async def list_timers(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_timers, session_id)

    def _list_timers(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, pt.character_name, pt.display_name
                FROM timer_instances t
                LEFT JOIN participants pt ON pt.id = t.participant_id
                WHERE t.session_id = ?
                ORDER BY
                    CASE t.status
                        WHEN 'active' THEN 0
                        WHEN 'paused' THEN 1
                        ELSE 2
                    END,
                    t.deadline_at, t.created_at
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["action"] = json_load(item.pop("action_json"), {})
                result.append(item)
            return result

    async def control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        *,
        seconds: int = 0,
    ) -> dict[str, Any]:
        return await self._run(
            self._control_timer,
            timer_id,
            action,
            actor_id,
            seconds,
        )

    def _control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        seconds: int,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in {
            "pause",
            "resume",
            "extend",
            "expire",
            "disable",
        }:
            raise ValueError("不支持的计时器操作")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("计时器不存在")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, Mapping):
                    payload = {}
                reminder_interval = timer_reminder_interval(
                    row["timer_type"],
                    payload,
                )
                reminders_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                status = row["status"]
                deadline = str(row["deadline_at"] or "")
                remaining = row["remaining_seconds"]
                reminder_at = str(row["reminder_at"] or "")
                reminder_sent = int(row["reminder_sent"] or 0)
                if action == "pause":
                    if status != "active":
                        raise ValueError("只有运行中的计时器可以暂停")
                    if deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        remaining = max(
                            0,
                            int((deadline_dt - now_dt).total_seconds()),
                        )
                    status = "paused"
                    deadline = ""
                    reminder_at = ""
                    reminder_sent = 0
                elif action == "resume":
                    if status != "paused":
                        raise ValueError("只有暂停中的计时器可以恢复")
                    status = "active"
                    seconds_left = max(1, int(remaining or 1))
                    deadline_dt = now_dt + timedelta(seconds=seconds_left)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    next_reminder = deadline_dt - timedelta(
                        seconds=reminder_interval
                    )
                    if reminders_enabled:
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    reminder_sent = 0
                elif action == "extend":
                    if seconds <= 0:
                        raise ValueError("延长时间必须大于 0")
                    if status == "active" and deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        deadline_dt += timedelta(seconds=seconds)
                        deadline = deadline_dt.isoformat(timespec="seconds")
                        next_reminder = deadline_dt - timedelta(
                            seconds=reminder_interval
                        )
                        if not reminders_enabled:
                            reminder_at = deadline
                            reminder_sent = 0
                        elif not reminder_at and next_reminder < deadline_dt:
                            reminder_at = next_reminder.isoformat(
                                timespec="seconds"
                            )
                            reminder_sent = 0
                    else:
                        remaining = max(0, int(remaining or 0)) + seconds
                elif action == "expire":
                    status = "expired"
                    deadline = now
                    remaining = 0
                    reminder_at = ""
                else:
                    status = "cancelled"
                    deadline = ""
                    reminder_at = ""
                connection.execute(
                    """
                    UPDATE timer_instances SET
                        status = ?, deadline_at = ?,
                        remaining_seconds = ?, reminder_at = ?,
                        reminder_sent = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        deadline,
                        remaining,
                        reminder_at,
                        reminder_sent,
                        now,
                        timer_id,
                    ),
                )
                if action == "expire":
                    if row["timer_type"] == "card_code":
                        connection.execute(
                            """
                            UPDATE card_binding_codes
                            SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (str(payload.get("code") or ""),),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            payload.get("timeout_action") or "standby"
                        )
                        if timeout_action != "remind":
                            next_status = (
                                "archived"
                                if row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                                else "standby"
                            )
                            connection.execute(
                                """
                                UPDATE participants SET
                                    participation_status = ?,
                                    ready = 0, updated_at = ?
                                WHERE id = ?
                                  AND participation_status IN (
                                      'reserved', 'active'
                                  )
                                """,
                                (
                                    next_status,
                                    now,
                                    row["participant_id"],
                                ),
                            )
                            if (
                                row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                            ):
                                connection.execute(
                                    """
                                    UPDATE card_drafts SET status = 'cancelled',
                                        updated_at = ?
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (now, row["participant_id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE card_binding_codes
                                    SET status = 'cancelled'
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (row["participant_id"],),
                                )
                            elif next_status == PARTICIPANT_STANDBY:
                                self._start_standby_timer(
                                    connection,
                                    session_id=row["session_id"],
                                    participant_id=row["participant_id"],
                                )
                    elif (
                        row["timer_type"] == "standby"
                        and row["participant_id"]
                    ):
                        self._retire_participant_in_tx(
                            connection,
                            session_id=row["session_id"],
                            participant_id=row["participant_id"],
                            actor_id=actor_id,
                            forced=False,
                            reason="standby_timeout",
                        )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    f"timer.{action}",
                    timer_id,
                    {"seconds": seconds},
                )
                updated = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                connection.execute("COMMIT")
                item = dict(updated)
                item["action"] = json_load(item.pop("action_json"), {})
                return item
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._extend_active_timer,
            session_id,
            target,
            seconds,
            actor_id,
        )

    def _extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        reference = str(target or "").strip()
        with self._connect() as connection:
            if reference in {"准备阶段", "准备", "preparation"}:
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            else:
                participant = self._get_participant(
                    session_id,
                    "",
                    reference,
                    True,
                )
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND participant_id = ?
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, participant["id"]),
                ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有找到对应的活动计时器")
        return self._control_timer(
            str(row["id"]),
            "extend",
            actor_id,
            seconds,
        )

    async def pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._pause_session_timers,
            session_id,
            actor_id,
        )

    def _pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                for row in rows:
                    remaining = row["remaining_seconds"]
                    if row["deadline_at"]:
                        remaining = max(
                            0,
                            int(
                                (
                                    datetime.fromisoformat(row["deadline_at"])
                                    - now_dt
                                ).total_seconds()
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'paused', deadline_at = '',
                            remaining_seconds = ?, reminder_at = '',
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (remaining, now, row["id"]),
                    )
                    payload = json_load(row["action_json"], {})
                    if (
                        row["timer_type"] == "vote"
                        and isinstance(payload, Mapping)
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = '', updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.pause_all",
                    session_id,
                    {"count": len(rows)},
                )
                connection.execute("COMMIT")
                return len(rows)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._resume_session_timers,
            session_id,
            actor_id,
        )

    def _resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'paused'
                    """,
                    (session_id,),
                ).fetchall()
                # 后台关掉的倒计时不能被「恢复/继续/读档」重新唤醒。
                # 旧实现无条件恢复全部 paused 行，导致管理员关掉倒计时后
                # 只要有人发 /酒馆 继续，提醒就会重新开始刷屏。
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
                global_enabled = bool(
                    policy["global_enabled"] if policy else 0
                )
                resumed = 0
                for row in rows:
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    timer_type = str(row["timer_type"])
                    countdown_enabled = bool(
                        global_enabled and switches.get(timer_type, False)
                    )
                    if not countdown_enabled:
                        # 策略仍为关闭：保持暂停，并补齐标记，
                        # 便于之后重新打开时由 _set_timer_policy 正确复活。
                        if not payload.get("paused_by_policy"):
                            payload = dict(payload)
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances
                                SET action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (json_dump(payload), now, row["id"]),
                            )
                        continue
                    if payload.get("paused_by_policy"):
                        # 策略已重新打开的场景由 _set_timer_policy 负责恢复，
                        # 这里不越权唤醒，避免与策略状态打架。
                        continue
                    resumed += 1
                    remaining = max(1, int(row["remaining_seconds"] or 1))
                    deadline_dt = now_dt + timedelta(seconds=remaining)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    interval = timer_reminder_interval(
                        row["timer_type"],
                        payload,
                    )
                    next_reminder = deadline_dt - timedelta(
                        seconds=interval
                    )
                    if timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'active', deadline_at = ?,
                            reminder_at = ?, reminder_sent = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (deadline, reminder_at, now, row["id"]),
                    )
                    if (
                        row["timer_type"] == "vote"
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = ?, updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (deadline, now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.resume_all",
                    session_id,
                    {
                        "count": resumed,
                        "skipped_by_policy": len(rows) - resumed,
                    },
                )
                connection.execute("COMMIT")
                return resumed
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def process_due_timers(self) -> list[dict[str, Any]]:
        return await self._run(self._process_due_timers)

    @staticmethod
    def _timer_notice_targets(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> list[dict[str, str]]:
        """Resolve the people who can still satisfy a running timer."""

        session_id = str(row["session_id"] or "")
        participant_id = str(row["participant_id"] or "")
        timer_type = str(row["timer_type"] or "")
        user_ids: list[str] = []

        if participant_id:
            participant = connection.execute(
                """
                SELECT group_user_id FROM participants
                WHERE id = ? AND session_id = ?
                """,
                (participant_id, session_id),
            ).fetchone()
            if participant and participant["group_user_id"]:
                user_ids.append(str(participant["group_user_id"]))
        elif timer_type == "vote":
            action = json_load(row["action_json"], {})
            vote_id = str(action.get("vote_id") or "")
            vote = (
                connection.execute(
                    """
                    SELECT eligible_user_ids_json FROM group_votes
                    WHERE id = ? AND session_id = ? AND status = 'open'
                    """,
                    (vote_id, session_id),
                ).fetchone()
                if vote_id
                else None
            )
            if vote:
                eligible = json_load(
                    vote["eligible_user_ids_json"],
                    [],
                )
                if not isinstance(eligible, list):
                    eligible = []
                voted = {
                    str(item["user_id"])
                    for item in connection.execute(
                        """
                        SELECT user_id FROM vote_ballots
                        WHERE vote_id = ?
                        """,
                        (vote_id,),
                    ).fetchall()
                }
                user_ids.extend(
                    str(user_id)
                    for user_id in eligible
                    if str(user_id) and str(user_id) not in voted
                )
        elif timer_type == "preparation":
            user_ids.extend(
                str(item["group_user_id"])
                for item in connection.execute(
                    """
                    SELECT group_user_id FROM participants
                    WHERE session_id = ? AND ready = 0
                      AND participation_status IN ('reserved', 'active')
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                if item["group_user_id"]
            )

        targets: list[dict[str, str]] = []
        seen: set[str] = set()
        for user_id in user_ids:
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            participant = connection.execute(
                """
                SELECT display_name, character_name,
                       private_user_id, private_origin
                FROM participants
                WHERE session_id = ? AND group_user_id = ?
                ORDER BY
                    CASE participation_status
                        WHEN 'active' THEN 0
                        WHEN 'reserved' THEN 1
                        WHEN 'standby' THEN 2
                        WHEN 'away' THEN 3
                        ELSE 4
                    END,
                    created_at DESC
                LIMIT 1
                """,
                (session_id, user_id),
            ).fetchone()
            display_name = user_id
            private_user_id = ""
            private_origin = ""
            if participant:
                display_name = str(
                    participant["character_name"]
                    or participant["display_name"]
                    or user_id
                )
                private_user_id = str(
                    participant["private_user_id"] or ""
                )
                private_origin = str(
                    participant["private_origin"] or ""
                )
            targets.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "private_user_id": private_user_id,
                    "private_origin": private_origin,
                }
            )
        return targets

    def _process_due_timers(self) -> list[dict[str, Any]]:
        notifications: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                now_dt = datetime.fromisoformat(now)
                reminder_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active'
                      AND reminder_at <> '' AND reminder_at <= ?
                      AND deadline_at <> '' AND deadline_at > ?
                    """,
                    (now, now),
                ).fetchall()
                # 分发前再查一次策略：即便有历史脏数据或并发写入
                # 让被关闭的计时器仍处于 active，也不会再推送提醒。
                policy_cache: dict[str, tuple[bool, Mapping[str, Any]]] = {}

                def _countdown_allowed(
                    session_key: str,
                    timer_type_key: str,
                ) -> bool:
                    cached = policy_cache.get(session_key)
                    if cached is None:
                        policy_row = connection.execute(
                            """
                            SELECT global_enabled, switches_json
                            FROM timer_policies WHERE session_id = ?
                            """,
                            (session_key,),
                        ).fetchone()
                        policy_switches = json_load(
                            policy_row["switches_json"]
                            if policy_row
                            else "",
                            {},
                        )
                        if not isinstance(policy_switches, Mapping):
                            policy_switches = {}
                        cached = (
                            bool(
                                policy_row["global_enabled"]
                                if policy_row
                                else 0
                            ),
                            policy_switches,
                        )
                        policy_cache[session_key] = cached
                    enabled, switch_map = cached
                    return bool(
                        enabled and switch_map.get(timer_type_key, False)
                    )

                for row in reminder_rows:
                    deadline = datetime.fromisoformat(row["deadline_at"])
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    # A24 以前的建卡计时器没有显式保存提醒开关和间隔。
                    # 首次轮询时迁移为 120 秒节奏，但不立即发送已经过时的
                    # 旧提醒，避免升级瞬间刷屏。
                    if (
                        row["timer_type"] == "card_completion"
                        and (
                            "reminder_enabled" not in payload
                            or "reminder_interval_seconds" not in payload
                        )
                    ):
                        migrated_payload = dict(payload)
                        migrated_payload["reminder_enabled"] = True
                        migrated_payload["reminder_interval_seconds"] = (
                            CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                        )
                        next_at = min(
                            deadline,
                            now_dt + timedelta(
                                seconds=CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET action_json = ?, reminder_at = ?,
                                reminder_sent = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(migrated_payload),
                                next_at.isoformat(timespec="seconds"),
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    reminder_interval = timer_reminder_interval(
                        row["timer_type"],
                        payload,
                    )
                    if not _countdown_allowed(
                        str(row["session_id"]),
                        str(row["timer_type"]),
                    ):
                        stale_payload = dict(payload)
                        stale_payload["paused_by_policy"] = True
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, action_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                max(
                                    0,
                                    int(
                                        (deadline - now_dt).total_seconds()
                                    ),
                                ),
                                json_dump(stale_payload),
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    if not timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET reminder_at = ?, reminder_sent = 0,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (row["deadline_at"], now, row["id"]),
                        )
                        continue
                    remaining = max(
                        1,
                        int((deadline - now_dt).total_seconds()),
                    )
                    next_reminder = now_dt + timedelta(
                        seconds=max(1, int(reminder_interval or 1))
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET reminder_at = ?, reminder_sent = 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            (
                                next_reminder.isoformat(timespec="seconds")
                                if next_reminder < deadline
                                else ""
                            ),
                            now,
                            row["id"],
                        ),
                    )
                    notifications.append(
                        {
                            "kind": "reminder",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": remaining,
                            "reminder_interval_seconds": (
                                reminder_interval
                            ),
                            "targets": self._timer_notice_targets(
                                connection,
                                row,
                            ),
                        }
                    )
                due_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active' AND deadline_at <> ''
                      AND deadline_at <= ?
                    ORDER BY deadline_at, created_at
                    """,
                    (now,),
                ).fetchall()
                for row in due_rows:
                    targets = self._timer_notice_targets(connection, row)
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'expired', remaining_seconds = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    action = json_load(row["action_json"], {})
                    if row["timer_type"] == "card_code":
                        code = str(action.get("code") or "")
                        connection.execute(
                            """
                            UPDATE card_binding_codes SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (code,),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            action.get("timeout_action") or "standby"
                        )
                        if timeout_action != "remind":
                            next_status = (
                                "archived"
                                if row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                                else "standby"
                            )
                            connection.execute(
                                """
                                UPDATE participants SET
                                    participation_status = ?,
                                    ready = 0, updated_at = ?
                                WHERE id = ?
                                  AND participation_status IN (
                                      'reserved', 'active'
                                  )
                                """,
                                (
                                    next_status,
                                    now,
                                    row["participant_id"],
                                ),
                            )
                            if next_status == "archived":
                                connection.execute(
                                    """
                                    UPDATE character_card_drafts
                                    SET status = 'expired', updated_at = ?
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (now, row["participant_id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE card_binding_codes
                                    SET status = 'expired'
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (row["participant_id"],),
                                )
                            elif next_status == PARTICIPANT_STANDBY:
                                self._start_standby_timer(
                                    connection,
                                    session_id=row["session_id"],
                                    participant_id=row["participant_id"],
                                )
                    elif (
                        row["timer_type"] == "standby"
                        and row["participant_id"]
                    ):
                        self._retire_participant_in_tx(
                            connection,
                            session_id=row["session_id"],
                            participant_id=row["participant_id"],
                            actor_id="system",
                            forced=False,
                            reason="standby_timeout",
                        )
                    notifications.append(
                        {
                            "kind": "expired",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": 0,
                            "targets": targets,
                        }
                    )
                running_rows = connection.execute(
                    """
                    SELECT s.id, s.updated_at, ic.time_rules_json,
                           ic.phase_meta_json
                    FROM sessions s
                    JOIN instance_configs ic ON ic.session_id = s.id
                    WHERE s.state = 'running'
                    """
                ).fetchall()
                for session_row in running_rows:
                    rules = normalize_time_rules(
                        json_load(session_row["time_rules_json"], {})
                    )
                    idle_seconds = rules["all_idle_pause_seconds"]
                    if idle_seconds is None:
                        continue
                    idle_policy = connection.execute(
                        """
                        SELECT global_enabled, switches_json
                        FROM timer_policies WHERE session_id = ?
                        """,
                        (session_row["id"],),
                    ).fetchone()
                    idle_switches = json_load(
                        idle_policy["switches_json"] if idle_policy else "",
                        {},
                    )
                    if not (
                        idle_policy
                        and bool(idle_policy["global_enabled"])
                        and isinstance(idle_switches, Mapping)
                        and bool(idle_switches.get("all_idle", False))
                    ):
                        continue
                    phase_meta = json_load(
                        session_row["phase_meta_json"],
                        {},
                    )
                    activity_values = [
                        str(
                            phase_meta.get("started_at")
                            or session_row["updated_at"]
                            or ""
                        )
                    ]
                    activity_values.extend(
                        str(item[0] or "")
                        for item in connection.execute(
                            """
                            SELECT MAX(created_at) FROM events
                            WHERE session_id = ? AND role = 'player'
                            UNION ALL
                            SELECT MAX(vb.updated_at)
                            FROM vote_ballots vb
                            JOIN group_votes gv ON gv.id = vb.vote_id
                            WHERE gv.session_id = ?
                            UNION ALL
                            SELECT MAX(updated_at) FROM choice_sets
                            WHERE session_id = ? AND reroll_count > 0
                            """,
                            (
                                session_row["id"],
                                session_row["id"],
                                session_row["id"],
                            ),
                        ).fetchall()
                    )
                    last_activity = max(
                        (value for value in activity_values if value),
                        default=now,
                    )
                    try:
                        last_dt = datetime.fromisoformat(last_activity)
                    except ValueError:
                        continue
                    if (now_dt - last_dt).total_seconds() < idle_seconds:
                        continue
                    timer_rows = connection.execute(
                        """
                        SELECT * FROM timer_instances
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (session_row["id"],),
                    ).fetchall()
                    for timer_row in timer_rows:
                        remaining = timer_row["remaining_seconds"]
                        deadline = str(timer_row["deadline_at"] or "")
                        if deadline:
                            try:
                                deadline_dt = datetime.fromisoformat(deadline)
                                remaining = max(
                                    0,
                                    int(
                                        (deadline_dt - now_dt).total_seconds()
                                    ),
                                )
                            except ValueError:
                                pass
                        connection.execute(
                            """
                            UPDATE timer_instances SET
                                status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (remaining, now, timer_row["id"]),
                        )
                    connection.execute(
                        """
                        UPDATE sessions
                        SET state = 'paused', revision = revision + 1,
                            updated_at = ?
                        WHERE id = ? AND state = 'running'
                        """,
                        (now, session_row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        session_row["id"],
                        "system",
                        "session.idle_pause",
                        session_row["id"],
                        {
                            "idle_seconds": idle_seconds,
                            "last_activity": last_activity,
                            "paused_timers": len(timer_rows),
                        },
                    )
                    notifications.append(
                        {
                            "kind": "idle_pause",
                            "session_id": session_row["id"],
                            "timer_type": "all_idle",
                            "participant_id": "",
                        }
                    )
                connection.execute("COMMIT")
                return notifications
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _expire_turn_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (row["session_id"],),
        ).fetchone()
        if not session or session["state"] != SESSION_RUNNING:
            return
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (row["participant_id"],),
        ).fetchone()
        if not participant:
            return
        state = json_load(session["world_state_json"], {})
        turn = turn_state_from_world(state)
        if turn["current_user_id"] != participant["group_user_id"]:
            return
        timeout_count = int(participant["consecutive_timeouts"]) + 1
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
        if rules["turn_timeout_action"] == "hold":
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = ?, updated_at = ?
                WHERE id = ?
                """,
                (timeout_count, now, participant["id"]),
            )
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'system', 'system', '回合计时',
                          ?, ?, ?)
                """,
                (
                    new_id("event"),
                    session["id"],
                    session["turn_no"],
                    (
                        f"{participant['character_name'] or participant['display_name']}"
                        "本回合超时；按副本规则保留行动权与原选项。"
                    ),
                    json_dump(
                        {
                            "kind": "turn_timeout",
                            "participant_id": participant["id"],
                            "consecutive": timeout_count,
                            "action": "hold",
                        }
                    ),
                    now,
                ),
            )
            return
        next_turn = advance_turn(turn, participant["group_user_id"])
        moved_to_standby = (
            timeout_count >= rules["max_consecutive_timeouts"]
        )
        if moved_to_standby:
            next_turn, _ = leave_turn(
                next_turn,
                participant["group_user_id"],
            )
        connection.execute(
            """
            UPDATE participants SET
                consecutive_timeouts = ?,
                participation_status = CASE
                    WHEN ? THEN 'standby' ELSE participation_status
                END,
                ready = CASE WHEN ? THEN 0 ELSE ready END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                timeout_count,
                int(moved_to_standby),
                int(moved_to_standby),
                now,
                participant["id"],
            ),
        )
        if moved_to_standby:
            self._start_standby_timer(
                connection,
                session_id=session["id"],
                participant_id=participant["id"],
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, str(action.get("choice_set_id") or "")),
        )
        connection.execute(
            """
            UPDATE sessions SET
                world_state_json = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json_dump(
                    embed_turn_state(public_world_state(state), next_turn)
                ),
                now,
                session["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'system', '回合计时',
                      ?, ?, ?)
            """,
            (
                new_id("event"),
                session["id"],
                session["turn_no"],
                (
                    f"{participant['character_name'] or participant['display_name']}"
                    "本回合超时，行动权已安全移交。"
                    + (
                        "连续超时达到上限，已转入候补席。"
                        if moved_to_standby
                        else ""
                    )
                ),
                json_dump(
                    {
                        "kind": "turn_timeout",
                        "participant_id": participant["id"],
                        "consecutive": timeout_count,
                        "standby": moved_to_standby,
                    }
                ),
                now,
            ),
        )
        next_user = str(next_turn["current_user_id"] or "")
        if not next_user:
            return
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
            """,
            (session["id"], next_user),
        ).fetchone()
        if not next_participant:
            return
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
                next_turn["round_no"],
                int(session["revision"]) + 1,
                json_dump(fallback_choices(state)),
                f"timeout:{row['id']}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={"choice_set_id": choice_id, "user_id": next_user},
        )

    def _expire_vote_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        vote_id = str(action.get("vote_id") or "")
        vote_row = connection.execute(
            """
            SELECT * FROM group_votes
            WHERE id = ? AND status = 'open'
            """,
            (vote_id,),
        ).fetchone()
        if not vote_row:
            return
        vote = self._vote(vote_row)
        ballots = [
            dict(item)
            for item in connection.execute(
                """
                SELECT user_id, option_key FROM vote_ballots
                WHERE vote_id = ?
                """,
                (vote_id,),
            ).fetchall()
        ]
        tally = vote_result(
            eligible_count=len(vote["eligible_user_ids"]),
            ballots=ballots,
            option_keys=[
                str(item.get("key")) for item in vote["options"]
            ],
        )
        # 0.11.3：超时结束时按实际票数判定——截止前已形成多数则通过，
        # 并标记 pending_resolution 供下次输入自动推进叙事；
        # 旧实现无条件判 rejected，多数票在截止前达成也会被否决。
        winner = str(tally.get("winner") or "")
        passed = bool(winner)
        status = "passed" if passed else "rejected"
        result_payload: dict[str, Any] = {**tally, "reason": "timeout"}
        if passed:
            result_payload["pending_resolution"] = True
        connection.execute(
            """
            UPDATE group_votes SET
                status = ?, winner_key = ?, result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                winner,
                json_dump(result_payload),
                now,
                vote_id,
            ),
        )
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (vote["session_id"],),
        ).fetchone()
        self._resume_after_vote(
            connection,
            session=session,
            vote=vote,
            now=now,
        )
        self._apply_return_vote_result(
            connection,
            vote_id=vote_id,
            passed=passed,
            now=now,
        )
