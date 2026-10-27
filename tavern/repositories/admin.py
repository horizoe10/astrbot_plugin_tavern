"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..constants import PLUGIN_VERSION


class AdminRepositoryMixin:
    async def grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        *,
        duration_seconds: int | None = None,
        permissions: list[str] | None = None,
        expiry_kind: str = "none",
        expires_round: int = 0,
        auto_restore: bool = False,
        source: str = "player",
    ) -> dict[str, Any]:
        """A16：授予角色代控权。

        source=player 仅允许本人授权；source=admin/dm 允许管理员/人工 DM
        强制托管（由上层权限判断决定）。
        """
        return await self._run(
            self._grant_delegation,
            session_id,
            owner_user_id,
            delegate_user_id,
            actor_id,
            duration_seconds,
            list(permissions or []) if permissions else None,
            str(expiry_kind or "none").strip(),
            int(expires_round or 0),
            bool(auto_restore),
            str(source or "player").strip(),
        )

    def _grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        duration_seconds: int | None,
        permissions: list[str] | None,
        expiry_kind: str,
        expires_round: int,
        auto_restore: bool,
        source: str,
    ) -> dict[str, Any]:
        owner_user_id = validate_platform_id(
            owner_user_id, label="角色拥有者 ID"
        )
        delegate_user_id = validate_platform_id(
            delegate_user_id, label="代控用户 ID"
        )
        if source not in {"player", "admin", "dm", "system"}:
            raise ValueError("托管来源必须为 player/admin/dm/system")
        if source == "player" and actor_id != owner_user_id:
            raise PermissionError("代控只能由角色本人授权")
        if owner_user_id == delegate_user_id:
            raise ValueError("不能把自己的角色授权给自己")
        if expiry_kind not in {"none", "datetime", "round", "instance"}:
            raise ValueError("托管期限类型必须为 none/datetime/round/instance")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, owner_user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    raise ValueError("已经退场的角色不能授权代控")
                if duration_seconds is None and expiry_kind == "datetime":
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
                    duration_seconds = rules["delegation_ttl_seconds"]
                now = utc_now()
                connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (now, participant["id"]),
                )
                grant_id = new_id("delegation")
                expires_at = (
                    deadline_after(duration_seconds)
                    if expiry_kind == "datetime" and duration_seconds
                    else ""
                )
                default_permissions = ["choose", "reroll", "skip"]
                granted = permissions if permissions else default_permissions
                allowed = {
                    "choose", "vote", "free_action", "check", "combat",
                    "view_private", "modify_temp", "modify_permanent",
                }
                granted = [p for p in granted if p in allowed]
                if not granted:
                    raise ValueError("托管权限列表为空或包含非法权限")
                connection.execute(
                    """
                    INSERT INTO delegation_grants(
                        id, session_id, participant_id, owner_user_id,
                        delegate_user_id, permissions_json, status,
                        expires_at, created_at, updated_at,
                        expiry_kind, expires_round, auto_restore, source,
                        granted_by
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        session_id,
                        participant["id"],
                        owner_user_id,
                        delegate_user_id,
                        json_dump(granted),
                        expires_at,
                        now,
                        now,
                        expiry_kind,
                        expires_round,
                        int(auto_restore),
                        source,
                        actor_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.grant",
                    grant_id,
                    {
                        "participant_id": participant["id"],
                        "owner_user_id": owner_user_id,
                        "delegate_user_id": delegate_user_id,
                        "permissions": granted,
                        "expiry_kind": expiry_kind,
                        "expires_at": expires_at,
                        "expires_round": expires_round,
                        "auto_restore": auto_restore,
                        "source": source,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM delegation_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = dict(row)
                result["permissions"] = json_load(
                    result.pop("permissions_json"), []
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
        *,
        force: bool = False,
    ) -> int:
        return await self._run(
            self._revoke_delegation,
            session_id,
            owner_user_id,
            actor_id,
            bool(force),
        )

    def _revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
        force: bool,
    ) -> int:
        if actor_id != owner_user_id and not force:
            raise PermissionError("代控只能由角色本人撤销")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND owner_user_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, owner_user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.revoke",
                    owner_user_id,
                    {"count": cursor.rowcount, "forced": force},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_owner_control(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
    ) -> int:
        """管理员/DM 恢复角色原玩家控制权（撤销全部活跃委托）。"""
        return await self._run(
            self._restore_owner_control, session_id, participant_id, actor_id
        )

    def _restore_owner_control(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.restore_owner",
                    participant_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def active_controller(
        self,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._active_controller, session_id, participant_id
        )

    def _active_controller(
        self,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            participant = connection.execute(
                """
                SELECT group_user_id, display_name, character_name
                FROM participants WHERE id = ? AND session_id = ?
                """,
                (participant_id, session_id),
            ).fetchone()
            if not participant:
                raise DatabaseNotFoundError("回合角色不存在")
            self._expire_delegations_locked(connection, session_id, utc_now())
            row = connection.execute(
                """
                SELECT * FROM delegation_grants
                WHERE participant_id = ? AND status = 'active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (participant_id,),
            ).fetchone()
        owner_user_id = str(participant["group_user_id"] or "")
        if not row:
            return {
                "participant_id": participant_id,
                "owner_user_id": owner_user_id,
                "controller_user_id": owner_user_id,
                "mode": "owner",
                "grant": None,
            }
        return {
            "participant_id": participant_id,
            "owner_user_id": owner_user_id,
            "controller_user_id": str(row["delegate_user_id"]),
            "mode": "delegate",
            "grant": dict(row),
        }

    async def expire_due_delegations(self, session_id: str) -> int:
        return await self._run(self._expire_due_delegations, session_id)

    def _expire_due_delegations(self, session_id: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = self._expire_delegations_locked(
                    connection, session_id, utc_now()
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return count

    def _expire_delegations_locked(
        self,
        connection: Any,
        session_id: str,
        now: str,
    ) -> int:
        cursor = connection.execute(
            """
            UPDATE delegation_grants SET status = 'expired', updated_at = ?
            WHERE session_id = ? AND status = 'active'
              AND expires_at <> '' AND expires_at <= ?
            """,
            (now, session_id, now),
        )
        return cursor.rowcount

    async def list_delegations(self, session_id: str) -> list[dict[str, Any]]:
        return await self._run(self._list_delegations, session_id)

    def _list_delegations(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, pt.display_name AS participant_display,
                       pt.character_name AS participant_character
                FROM delegation_grants d
                JOIN participants pt ON pt.id = d.participant_id
                WHERE d.session_id = ?
                ORDER BY d.created_at DESC
                """,
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["permissions"] = json_load(item.pop("permissions_json"), [])
            result.append(item)
        return result


    async def authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authorize_participant_control,
            session_id,
            participant_id,
            controller_user_id,
            permission,
        )

    def _authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE id = ? AND session_id = ?
                    """,
                    (participant_id, session_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("回合角色不存在")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'expired', updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                      AND expires_at <> '' AND expires_at <= ?
                    """,
                    (now, participant_id, now),
                )
                owner_id = str(participant["group_user_id"])
                # A16：回合级托管到期判定（expiry_kind=round）
                current_round = 0
                session_row = connection.execute(
                    "SELECT world_state_json FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session_row:
                    stored = json_load(session_row["world_state_json"], {})
                    try:
                        current_round = int(
                            turn_state_from_world(stored).get("round_no") or 0
                        )
                    except Exception:
                        current_round = 0
                if controller_user_id == owner_id:
                    connection.execute(
                        """
                        UPDATE delegation_grants
                        SET status = 'revoked', updated_at = ?
                        WHERE participant_id = ? AND status = 'active'
                        """,
                        (now, participant_id),
                    )
                    connection.execute("COMMIT")
                    return {
                        "authorized": True,
                        "mode": "owner",
                        "controller_user_id": controller_user_id,
                        "source": "owner",
                        "forced": False,
                    }
                rows = connection.execute(
                    """
                    SELECT * FROM delegation_grants
                    WHERE participant_id = ? AND delegate_user_id = ?
                      AND status = 'active'
                    ORDER BY created_at DESC
                    """,
                    (participant_id, controller_user_id),
                ).fetchall()
                active_rows = []
                for row in rows:
                    if (
                        str(row["expiry_kind"]) == "round"
                        and int(row["expires_round"] or 0) > 0
                        and current_round > int(row["expires_round"])
                    ):
                        continue
                    active_rows.append(row)
                active_row = active_rows[0] if active_rows else None
                authorized = bool(
                    active_row
                    and permission
                    in json_load(active_row["permissions_json"], [])
                )
                connection.execute("COMMIT")
                return {
                    "authorized": authorized,
                    "mode": "delegate" if authorized else "none",
                    "owner_user_id": owner_id,
                    "controller_user_id": controller_user_id,
                    "source": str(active_row["source"]) if active_row else "",
                    "forced": bool(
                        active_row
                        and str(active_row["source"]) in {"admin", "dm"}
                    ),
                    "expiry_kind": str(active_row["expiry_kind"]) if active_row else "",
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_permission_grants,
            session_id,
        )

    def _list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._grant_permission,
            session_id,
            user_id,
            role,
            actor_id,
        )

    def _grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if role not in {"host", "moderator"}:
            raise ValueError("权限角色必须是 host 或 moderator")
        user_id = validate_platform_id(user_id, label="用户 ID")
        with self._connect() as connection:
            self._assert_session_writable(connection, session_id)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO permission_grants(
                    id, session_id, user_id, role, granted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, user_id, role) DO UPDATE SET
                    granted_by = excluded.granted_by,
                    created_at = excluded.created_at
                """,
                (
                    new_id("permission"),
                    session_id,
                    user_id,
                    role,
                    actor_id,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? AND user_id = ? AND role = ?
                """,
                (session_id, user_id, role),
            ).fetchone()
            return dict(row)

    async def permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        return await self._run(
            self._permission_roles,
            session_id,
            user_id,
        )

    def _permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        with self._connect() as connection:
            return {
                str(row["role"])
                for row in connection.execute(
                    """
                    SELECT role FROM permission_grants
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchall()
            }

    async def list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_return_requests,
            session_id,
        )

    def _list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rr.*, pt.character_name, pt.display_name
                FROM return_requests rr
                JOIN participants pt ON pt.id = rr.participant_id
                WHERE rr.session_id = ?
                ORDER BY rr.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["progress"] = json_load(item.pop("progress_json"), {})
                result.append(item)
            return result

    async def list_audit(
        self,
        session_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_audit,
            session_id,
            limit,
            offset,
        )

    def _list_audit(
        self,
        session_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        with self._connect() as connection:
            if session_id:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    WHERE session_id = ?
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "target": row["target"],
                    "detail": json_load(row["detail_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    async def write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        await self._run(
            self._write_audit,
            session_id,
            actor_id,
            action,
            target,
            dict(detail),
        )

    def _write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            self._insert_audit(
                connection,
                session_id,
                actor_id,
                action,
                target,
                detail,
            )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs(
                session_id, actor_id, action, target, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                actor_id,
                action,
                target,
                json_dump(dict(detail)),
                utc_now(),
            ),
        )

    async def overview(self) -> dict[str, Any]:
        return await self._run(self._overview)

    async def global_token_usage(self, window_seconds: int) -> int:
        """全副本滚动窗口内的已完成 Token 用量合计（0.12.0-A3）。"""
        return await self._run(self._global_token_usage, window_seconds)

    def _global_token_usage(self, window_seconds: int) -> int:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(1, int(window_seconds)))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(total_tokens), 0)
                    FROM token_usage
                    WHERE status = 'completed' AND created_at >= ?
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )

    def _overview(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                "worlds": connection.execute(
                    "SELECT COUNT(*) FROM worlds WHERE archived = 0"
                ).fetchone()[0],
                "sessions": connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                "running": connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE state = 'running'"
                ).fetchone()[0],
                "players": connection.execute(
                    """
                    SELECT COUNT(*) FROM participants
                    WHERE participation_status IN (
                        'reserved', 'active', 'standby', 'away'
                    )
                    """
                ).fetchone()[0],
                "memories": connection.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0],
                "snapshots": connection.execute(
                    "SELECT COUNT(*) FROM snapshots"
                ).fetchone()[0],
                "preparing": connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions WHERE state = 'preparing'
                    """
                ).fetchone()[0],
                "open_votes": connection.execute(
                    """
                    SELECT COUNT(*) FROM group_votes WHERE status = 'open'
                    """
                ).fetchone()[0],
                "active_timers": connection.execute(
                    """
                    SELECT COUNT(*) FROM timer_instances
                    WHERE status = 'active'
                    """
                ).fetchone()[0],
                "pending_deliveries": connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE status='pending'"
                ).fetchone()[0],
            }
            catalog_size = (
                self.path.stat().st_size if self.path.exists() else 0
            )
            instance_paths = list(
                (self.data_dir / "groups").glob(
                    "*/stories/*/instance.sqlite3"
                )
            )
            instance_size = sum(
                item.stat().st_size
                for item in instance_paths
                if item.is_file()
            )
            storage_errors = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM story_storage
                    WHERE sync_status = 'error'
                    """
                ).fetchone()[0]
            )
            # 0.12.0-A3：近 24h 审计统计（供总览「完整性 / 群内指令」）。
            cutoff_24h = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat(timespec="seconds")
            audit_total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs WHERE created_at >= ?
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            audit_failed = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ?
                      AND (
                          action LIKE '%.failed'
                          OR action LIKE '%denied'
                          OR detail_json LIKE '%"error"%'
                      )
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            relaxed_hits = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ? AND action LIKE '%relaxed%'
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            invalid_transitions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ?
                      AND action IN ('state.failed', 'transition.failed')
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            database_ok = bool(
                connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()[0]
                == "ok"
            ) and storage_errors == 0
            return {
                "counts": counts,
                "database_size": catalog_size + instance_size,
                "catalog_size": catalog_size,
                "instance_size": instance_size,
                "instance_database_count": len(instance_paths),
                "storage_errors": storage_errors,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "database_ok": database_ok,
                # 0.12.0-A3：运行完整性（WebUI 总览「运行完整性」卡片）。
                "integrity": {
                    "schema_version": DATABASE_SCHEMA_VERSION,
                    "database_ok": database_ok,
                    "storage_errors": storage_errors,
                    "recovery_points": counts["snapshots"],
                    "failed_operations_24h": audit_failed,
                    "invalid_transitions_24h": invalid_transitions,
                },
                # 0.12.0-A3：群内指令统计（WebUI 总览「群内指令」卡片）。
                "commands": {
                    "jg_enabled": True,
                    "command_count_24h": audit_total,
                    "success_rate": (
                        round(
                            (1 - audit_failed / audit_total) * 100, 1
                        )
                        if audit_total
                        else 100.0
                    ),
                    "relaxed_parse_hits_24h": relaxed_hits,
                },
            }

    async def cleanup(self, audit_retention_days: int) -> dict[str, int]:
        return await self._run(self._cleanup, audit_retention_days)

    def _cleanup(self, audit_retention_days: int) -> dict[str, int]:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=max(1, audit_retention_days))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            audit_cursor = connection.execute(
                "DELETE FROM audit_logs WHERE created_at < ?",
                (cutoff,),
            )
            now = utc_now()
            code_cursor = connection.execute(
                """
                UPDATE card_binding_codes SET status = 'expired'
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now,),
            )
            draft_cursor = connection.execute(
                """
                UPDATE character_card_drafts SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            delegation_cursor = connection.execute(
                """
                UPDATE delegation_grants SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            ban_cursor = connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            return {
                "audit_logs": audit_cursor.rowcount,
                "card_codes": code_cursor.rowcount,
                "card_drafts": draft_cursor.rowcount,
                "delegations": delegation_cursor.rowcount,
                "bans": ban_cursor.rowcount,
            }

    async def export_bundle(self) -> dict[str, Any]:
        return await self._run(self._export_bundle)

    def _export_bundle(self) -> dict[str, Any]:
        with self._connect() as connection:
            tables = {
                "worlds": "SELECT * FROM worlds ORDER BY created_at",
                "characters": "SELECT * FROM characters ORDER BY created_at",
                "sessions": "SELECT * FROM sessions ORDER BY created_at",
                "players": "SELECT * FROM players ORDER BY created_at",
                "events": "SELECT * FROM events ORDER BY seq",
                "memories": "SELECT * FROM memories ORDER BY created_at",
                "snapshots": "SELECT * FROM snapshots ORDER BY created_at",
                "audit_logs": "SELECT * FROM audit_logs ORDER BY id",
                "instance_configs": (
                    "SELECT * FROM instance_configs ORDER BY created_at"
                ),
                "character_cards": (
                    "SELECT * FROM character_cards ORDER BY created_at"
                ),
                "character_card_versions": (
                    "SELECT * FROM character_card_versions ORDER BY created_at"
                ),
                "card_revision_requests": (
                    "SELECT * FROM card_revision_requests ORDER BY created_at"
                ),
                "participants": (
                    "SELECT * FROM participants ORDER BY created_at"
                ),
                "character_runtime_states": (
                    "SELECT * FROM character_runtime_states ORDER BY created_at"
                ),
                "character_card_drafts": (
                    "SELECT * FROM character_card_drafts ORDER BY created_at"
                ),
                "card_binding_codes": (
                    "SELECT * FROM card_binding_codes ORDER BY created_at"
                ),
                "choice_sets": (
                    "SELECT * FROM choice_sets ORDER BY created_at"
                ),
                "rolls": "SELECT * FROM rolls ORDER BY created_at",
                "group_votes": (
                    "SELECT * FROM group_votes ORDER BY created_at"
                ),
                "vote_ballots": (
                    "SELECT * FROM vote_ballots ORDER BY created_at"
                ),
                "selected_world_events": (
                    "SELECT * FROM selected_world_events ORDER BY created_at"
                ),
                "timer_instances": (
                    "SELECT * FROM timer_instances ORDER BY created_at"
                ),
                "delegation_grants": (
                    "SELECT * FROM delegation_grants ORDER BY created_at"
                ),
                "permission_grants": (
                    "SELECT * FROM permission_grants ORDER BY created_at"
                ),
                "ban_records": (
                    "SELECT * FROM ban_records ORDER BY created_at"
                ),
                "return_requests": (
                    "SELECT * FROM return_requests ORDER BY created_at"
                ),
                "snapshot_workflows": (
                    "SELECT * FROM snapshot_workflows ORDER BY snapshot_id"
                ),
                "session_archives": (
                    "SELECT * FROM session_archives ORDER BY ended_at"
                ),
                "session_rule_states": (
                    "SELECT * FROM session_rule_states ORDER BY created_at"
                ),
                "dm_control_states": (
                    "SELECT * FROM dm_control_states ORDER BY created_at"
                ),
                "session_characters": (
                    "SELECT * FROM session_characters ORDER BY created_at"
                ),
                "session_character_states": (
                    "SELECT * FROM session_character_states ORDER BY updated_at"
                ),
                "story_ledger": (
                    "SELECT * FROM story_ledger ORDER BY created_at"
                ),
                "scene_clocks": (
                    "SELECT * FROM scene_clocks ORDER BY created_at"
                ),
                "memory_governance": (
                    "SELECT * FROM memory_governance ORDER BY updated_at"
                ),
                "assist_tokens": (
                    "SELECT * FROM assist_tokens ORDER BY created_at"
                ),
                "roll_revisions": (
                    "SELECT * FROM roll_revisions ORDER BY created_at"
                ),
                "inspiration_transactions": (
                    "SELECT * FROM inspiration_transactions ORDER BY created_at"
                ),
                "provider_health": (
                    "SELECT * FROM provider_health ORDER BY updated_at"
                ),
                "configuration_revisions": (
                    "SELECT * FROM configuration_revisions ORDER BY id"
                ),
                "operation_receipts": (
                    "SELECT * FROM operation_receipts ORDER BY created_at"
                ),
                "world_feature_versions": (
                    "SELECT * FROM world_feature_versions ORDER BY world_id, world_revision, feature_name"
                ),
                "world_entity_registry": (
                    "SELECT * FROM world_entity_registry ORDER BY world_id, world_revision, entity_ref"
                ),
                "world_rule_revisions": (
                    "SELECT * FROM world_rule_revisions ORDER BY created_at"
                ),
                "world_snapshots": (
                    "SELECT * FROM world_snapshots ORDER BY created_at"
                ),
                "actor_capability_instances": (
                    "SELECT * FROM actor_capability_instances ORDER BY created_at"
                ),
                "runtime_effect_instances": (
                    "SELECT * FROM runtime_effect_instances ORDER BY created_at"
                ),
                "operation_commits": (
                    "SELECT * FROM operation_commits ORDER BY created_at"
                ),
                "resolution_receipts": (
                    "SELECT * FROM resolution_receipts ORDER BY created_at"
                ),
                "migration_receipts": (
                    "SELECT * FROM migration_receipts ORDER BY created_at"
                ),
                "group_registry": (
                    "SELECT * FROM group_registry ORDER BY created_at"
                ),
                "story_storage": (
                    "SELECT * FROM story_storage ORDER BY created_at"
                ),
                "timer_policies": (
                    "SELECT * FROM timer_policies ORDER BY updated_at"
                ),
                "token_usage": (
                    "SELECT * FROM token_usage ORDER BY created_at"
                ),
                "token_quota_policies": (
                    "SELECT * FROM token_quota_policies ORDER BY updated_at"
                ),
            }
            data: dict[str, list[dict[str, Any]]] = {}
            for name, query in tables.items():
                rows = connection.execute(query).fetchall()
                data[name] = [dict(row) for row in rows]
            return {
                "format": "astrbot-tavern-backup",
                "format_version": 1,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "exported_at": utc_now(),
                "data": data,
            }

    @staticmethod
    def validate_bundle(bundle: Mapping[str, Any]) -> None:
        if bundle.get("format") != "astrbot-tavern-backup":
            raise ValueError("不是有效的 AI 酒馆备份")
        if int(bundle.get("format_version", 0)) != 1:
            raise ValueError("不支持的备份格式版本")
        try:
            schema_version = int(bundle.get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("备份数据库版本无效") from exc
        if schema_version not in {9, 10, 11, DATABASE_SCHEMA_VERSION}:
            raise ValueError(
                f"v{PLUGIN_VERSION} 仅接受 Schema 9—{DATABASE_SCHEMA_VERSION} 备份；"
                f"当前为 Schema {schema_version}，请先升级插件或使用兼容版本转换"
            )
        data = bundle.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("备份缺少 data")
        required = {
            "worlds",
            "characters",
            "sessions",
            "players",
            "events",
            "memories",
            "snapshots",
            "audit_logs",
            "instance_configs",
            "character_cards",
            "character_card_versions",
            "card_revision_requests",
            "participants",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "choice_sets",
            "rolls",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "ban_records",
            "return_requests",
            "snapshot_workflows",
            "session_archives",
            "session_rule_states",
            "dm_control_states",
            "session_characters",
            "session_character_states",
            "story_ledger",
            "scene_clocks",
            "memory_governance",
            "assist_tokens",
            "roll_revisions",
            "inspiration_transactions",
            "provider_health",
            "configuration_revisions",
            "operation_receipts",
            "group_registry",
            "story_storage",
            "timer_policies",
            "token_usage",
            "token_quota_policies",
        }
        if not required.issubset(data.keys()):
            raise ValueError("备份数据表不完整")
        for table in required:
            if not isinstance(data[table], list):
                raise ValueError(f"备份表 {table} 格式错误")
            if len(data[table]) > 1_000_000:
                raise ValueError(f"备份表 {table} 记录数异常")
        if schema_version >= 10:
            required_v10 = {
                "world_feature_versions", "world_entity_registry",
                "world_rule_revisions", "world_snapshots",
                "actor_capability_instances", "runtime_effect_instances",
                "operation_commits", "resolution_receipts", "migration_receipts",
            }
            if not required_v10.issubset(data.keys()):
                raise ValueError("Schema 10 备份缺少规则与迁移数据表")

    async def import_bundle(
        self,
        bundle: Mapping[str, Any],
        mode: str,
        actor_id: str,
    ) -> dict[str, int]:
        return await self._run(
            self._import_bundle,
            dict(bundle),
            mode,
            actor_id,
        )

    def _import_bundle(self, bundle: dict[str, Any], mode: str, actor_id: str) -> dict[str, int]:
        self.validate_bundle(bundle)
        if mode not in {'merge', 'replace'}:
            raise ValueError('导入模式必须为 merge 或 replace')
        bundle_schema = int(bundle.get('schema_version', 0))
        data = {table: [dict(row) for row in rows] for table, rows in bundle['data'].items()}
        for table in (
            'world_feature_versions', 'world_entity_registry', 'world_rule_revisions',
            'world_snapshots', 'actor_capability_instances', 'runtime_effect_instances',
            'operation_commits', 'resolution_receipts', 'migration_receipts',
        ):
            data.setdefault(table, [])
        policy_tables = ('timer_policies', 'token_usage', 'token_quota_policies')
        counts: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if mode == 'merge':
                    self._validate_merge_conflicts(connection, data)
                if mode == 'replace':
                    connection.execute('DELETE FROM dm_control_states')
                    for table in ('migration_receipts', 'resolution_receipts', 'operation_commits', 'runtime_effect_instances', 'actor_capability_instances', 'world_entity_registry', 'world_feature_versions', 'world_snapshots', 'world_rule_revisions', 'audit_logs', 'token_usage', 'token_quota_policies', 'timer_policies', 'story_storage', 'group_registry', 'operation_receipts', 'card_revision_requests', 'configuration_revisions', 'provider_health', 'session_archives', 'inspiration_transactions', 'roll_revisions', 'assist_tokens', 'memory_governance', 'session_character_states', 'scene_clocks', 'story_ledger', 'session_characters', 'session_rule_states', 'snapshot_workflows', 'return_requests', 'ban_records', 'permission_grants', 'delegation_grants', 'timer_instances', 'selected_world_events', 'vote_ballots', 'group_votes', 'rolls', 'choice_sets', 'card_binding_codes', 'character_card_drafts', 'character_runtime_states', 'participants', 'character_card_versions', 'character_cards', 'instance_configs', 'snapshots', 'memories', 'events', 'players', 'sessions', 'characters', 'worlds'):
                        connection.execute(f'DELETE FROM {table}')
                used_numbers = {
                    int(row[0]) for row in connection.execute(
                        'SELECT display_no FROM worlds WHERE display_no IS NOT NULL'
                    ).fetchall()
                }
                next_number = max(used_numbers, default=0) + 1
                ordered_worlds = sorted(
                    data['worlds'], key=lambda item: (str(item.get('created_at') or ''), str(item.get('id') or ''))
                )
                for world_row in ordered_worlds:
                    desired = int(world_row.get('display_no') or 0)
                    existing_same = connection.execute(
                        'SELECT display_no FROM worlds WHERE id=?', (world_row.get('id'),)
                    ).fetchone()
                    if existing_same:
                        desired = int(existing_same[0])
                    elif desired <= 0 or (mode == 'merge' and desired in used_numbers):
                        while next_number in used_numbers:
                            next_number += 1
                        desired = next_number
                        next_number += 1
                    used_numbers.add(desired)
                    world_row['display_no'] = desired
                    world_row['sort_order'] = int(world_row.get('sort_order') or desired)
                self._import_rows(connection, 'worlds', data['worlds'], ('id', 'slug', 'display_no', 'sort_order', 'name', 'description', 'system_prompt', 'rules_json', 'extensions_json', 'opening_scene', 'initial_state_json', 'archived', 'revision', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'characters', data['characters'], ('id', 'world_id', 'slug', 'name', 'role', 'profile_json', 'prompt', 'enabled', 'sort_order', 'revision', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'sessions', data['sessions'], ('id', 'platform_id', 'group_id', 'unified_origin', 'instance_slug', 'instance_name', 'selected', 'world_id', 'state', 'turn_no', 'revision', 'world_state_json', 'history_floor_seq', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'players', data['players'], ('id', 'session_id', 'user_id', 'display_name', 'character_name', 'profile_json', 'enabled', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'events', data['events'], ('seq', 'id', 'session_id', 'turn_no', 'role', 'actor_id', 'actor_name', 'content', 'meta_json', 'created_at'), merge=mode == 'merge')
                self._import_rows(connection, 'memories', data['memories'], ('id', 'session_id', 'scope', 'scope_id', 'kind', 'content', 'importance', 'salience', 'tags_json', 'fingerprint', 'source_event_id', 'created_at', 'updated_at', 'last_accessed_at'), merge=mode == 'merge')
                self._import_rows(connection, 'snapshots', data['snapshots'], ('id', 'session_id', 'name', 'kind', 'turn_no', 'session_revision', 'world_id', 'world_state_json', 'created_by', 'created_at'), merge=mode == 'merge')
                runtime_columns: dict[str, tuple[str, ...]] = {'instance_configs': ('session_id', 'world_revision', 'world_snapshot_json', 'time_rules_json', 'phase_meta_json', 'created_at', 'updated_at'), 'character_cards': ('id', 'owner_user_id', 'world_id', 'display_name', 'archived', 'deleted', 'current_version', 'created_at', 'updated_at'), 'character_card_versions': ('id', 'character_card_id', 'version_no', 'template_version', 'profile_json', 'stats_json', 'status', 'review_note', 'reviewed_by', 'created_at'), 'participants': ('id', 'session_id', 'player_id', 'group_user_id', 'private_user_id', 'private_origin', 'display_name', 'character_card_id', 'character_version_id', 'character_name', 'character_code', 'aliases_json', 'card_status', 'ready', 'participation_status', 'seat_reserved_at', 'joined_round', 'consecutive_timeouts', 'exit_reason', 'created_at', 'updated_at'), 'character_runtime_states': ('id', 'session_id', 'participant_id', 'character_card_id', 'state_json', 'revision', 'created_at', 'updated_at'), 'character_card_drafts': ('id', 'participant_id', 'template_version', 'fields_json', 'current_step', 'status', 'expires_at', 'created_at', 'updated_at'), 'card_binding_codes': ('id', 'participant_id', 'code', 'status', 'expires_at', 'private_user_id', 'private_origin', 'created_at', 'used_at'), 'choice_sets': ('id', 'session_id', 'participant_id', 'round_no', 'session_revision', 'choices_json', 'status', 'reroll_count', 'selected_key', 'flavor_text', 'idempotency_key', 'created_at', 'updated_at'), 'rolls': ('id', 'session_id', 'choice_set_id', 'participant_id', 'roll_json', 'created_at'), 'group_votes': ('id', 'session_id', 'source_event_id', 'question', 'options_json', 'eligible_user_ids_json', 'stage', 'status', 'winner_key', 'suspended_user_id', 'deadline_at', 'result_json', 'created_at', 'updated_at'), 'vote_ballots': ('id', 'vote_id', 'user_id', 'option_key', 'created_at', 'updated_at'), 'selected_world_events': ('id', 'session_id', 'round_no', 'pool_item_id', 'payload_json', 'status', 'narrative', 'created_at', 'resolved_at'), 'timer_instances': ('id', 'session_id', 'participant_id', 'timer_type', 'status', 'deadline_at', 'remaining_seconds', 'reminder_at', 'reminder_sent', 'action_json', 'created_at', 'updated_at'), 'delegation_grants': ('id', 'session_id', 'participant_id', 'owner_user_id', 'delegate_user_id', 'permissions_json', 'status', 'expires_at', 'created_at', 'updated_at'), 'permission_grants': ('id', 'session_id', 'user_id', 'role', 'granted_by', 'created_at'), 'ban_records': ('id', 'session_id', 'platform_id', 'group_id', 'user_id', 'participant_id', 'scope', 'reason', 'actor_id', 'status', 'expires_at', 'created_at', 'updated_at'), 'return_requests': ('id', 'session_id', 'participant_id', 'requested_by', 'status', 'exit_type', 'objective', 'progress_json', 'vote_id', 'created_at', 'updated_at'), 'snapshot_workflows': ('snapshot_id', 'workflow_json')}
                for table in ('instance_configs', 'character_cards', 'character_card_versions', 'participants', 'character_runtime_states', 'character_card_drafts', 'card_binding_codes', 'choice_sets', 'rolls', 'group_votes', 'vote_ballots', 'selected_world_events', 'timer_instances', 'delegation_grants', 'permission_grants', 'ban_records', 'return_requests', 'snapshot_workflows'):
                    self._import_rows(connection, table, data[table], runtime_columns[table], merge=mode == 'merge')
                domain_columns: dict[str, tuple[str, ...]] = {'session_archives': ('session_id', 'termination_type', 'reason', 'final_snapshot_id', 'ended_by', 'ended_at', 'readonly'), 'session_rule_states': ('session_id', 'progress_json', 'content_boundaries_json', 'npc_policy_json', 'context_budget_json', 'dice_rules_json', 'recovery_json', 'revision', 'created_at', 'updated_at'), 'session_characters': ('id', 'session_id', 'stable_key', 'name', 'aliases_json', 'role_type', 'public_profile_json', 'known_facts_json', 'misconceptions_json', 'source', 'review_status', 'lifecycle_status', 'persistent', 'first_event_id', 'last_event_id', 'first_turn', 'last_turn', 'revision', 'created_at', 'updated_at'), 'session_character_states': ('character_id', 'state_json', 'revision', 'updated_at'), 'story_ledger': ('id', 'session_id', 'stable_key', 'kind', 'title', 'description', 'status', 'visibility', 'source_event_id', 'completed_event_id', 'revision', 'created_at', 'updated_at'), 'scene_clocks': ('id', 'session_id', 'stable_key', 'title', 'segments', 'current_value', 'visibility', 'trigger_text', 'status', 'triggered_event_id', 'revision', 'created_at', 'updated_at'), 'memory_governance': ('memory_id', 'visibility', 'locked', 'pinned', 'invalidated', 'supersedes_id', 'conflict_status', 'note', 'updated_by', 'updated_at'), 'assist_tokens': ('id', 'session_id', 'source_participant_id', 'target_participant_id', 'stat', 'method', 'status', 'expires_round', 'source_event_id', 'created_at', 'consumed_at'), 'roll_revisions': ('id', 'roll_id', 'revision_no', 'reason', 'previous_json', 'revised_json', 'actor_id', 'created_at'), 'inspiration_transactions': ('id', 'session_id', 'participant_id', 'delta', 'balance_after', 'reason', 'operation_id', 'created_at'), 'provider_health': ('provider_id', 'status', 'consecutive_failures', 'last_failure_reason', 'last_failure_at', 'last_success_at', 'circuit_until', 'updated_at'), 'configuration_revisions': ('id', 'fingerprint', 'payload_json', 'saved_by', 'saved_at'), 'operation_receipts': ('operation_id', 'session_id', 'operation_type', 'request_json', 'result_json', 'status', 'created_at', 'updated_at')}
                for table in ('session_rule_states', 'session_characters', 'session_character_states', 'story_ledger', 'scene_clocks', 'memory_governance', 'assist_tokens', 'roll_revisions', 'inspiration_transactions', 'provider_health', 'configuration_revisions', 'operation_receipts', 'session_archives'):
                    self._import_rows(connection, table, data[table], domain_columns[table], merge=mode == 'merge')
                v10_columns: dict[str, tuple[str, ...]] = {
                    'world_feature_versions': ('world_id', 'world_revision', 'feature_name', 'feature_version', 'required', 'created_at'),
                    'world_entity_registry': ('world_id', 'world_revision', 'entity_ref', 'entity_type', 'label', 'definition_json', 'content_hash', 'visibility', 'created_at'),
                    'world_rule_revisions': ('id', 'world_id', 'world_revision', 'content_hash', 'rules_json', 'created_at'),
                    'world_snapshots': ('id', 'world_id', 'world_revision', 'content_hash', 'snapshot_json', 'created_at'),
                    'actor_capability_instances': ('id', 'session_id', 'actor_ref', 'capability_ref', 'definition_version', 'source_ref', 'state_json', 'persistence_scope', 'available', 'created_at', 'updated_at'),
                    'runtime_effect_instances': ('id', 'session_id', 'target_ref', 'effect_ref', 'source_ref', 'state_json', 'duration_json', 'persistence_scope', 'status', 'created_at', 'updated_at'),
                    'operation_commits': ('operation_id', 'session_id', 'input_hash', 'status', 'result_json', 'rollback_json', 'created_at', 'updated_at'),
                    'resolution_receipts': ('receipt_id', 'operation_id', 'session_id', 'world_snapshot_id', 'content_hash', 'receipt_json', 'public_projection_json', 'created_at'),
                    'migration_receipts': ('id', 'migration_type', 'source_version', 'target_version', 'world_id', 'session_id', 'operation_id', 'receipt_json', 'confirmed_by', 'created_at'),
                }
                for table, columns in v10_columns.items():
                    self._import_rows(connection, table, data[table], columns, merge=mode == 'merge')
                self._import_rows(
                    connection,
                    'dm_control_states',
                    data['dm_control_states'],
                    (
                        'session_id', 'mode', 'active_dm_user_id', 'phase',
                        'directive', 'beat_no', 'current_actor_type',
                        'current_actor_ref', 'preserved_turn_json', 'revision',
                        'created_at', 'updated_at',
                    ),
                    merge=mode == 'merge',
                )
                self._import_rows(connection, 'group_registry', data['group_registry'], ('id', 'platform_id', 'group_id', 'remark', 'revision', 'created_at', 'updated_at'), merge=mode == 'merge')
                data['story_storage'] = []
                policy_columns: dict[str, tuple[str, ...]] = {'timer_policies': ('session_id', 'global_enabled', 'switches_json', 'revision', 'updated_by', 'updated_at'), 'token_usage': ('id', 'session_id', 'group_id', 'request_type', 'provider_id', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens', 'reserved_tokens', 'usage_source', 'status', 'created_at', 'settled_at'), 'token_quota_policies': ('id', 'scope_type', 'scope_id', 'window_seconds', 'token_limit', 'enabled', 'revision', 'updated_by', 'updated_at')}
                for table in policy_tables:
                    self._import_rows(connection, table, data[table], policy_columns[table], merge=mode == 'merge')
                self._import_rows(connection, 'card_revision_requests', data['card_revision_requests'], ('id', 'session_id', 'participant_id', 'character_card_id', 'base_version_id', 'candidate_version_id', 'status', 'request_note', 'review_note', 'requested_by', 'reviewed_by', 'created_at', 'updated_at'), merge=mode == 'merge')
                if mode == 'replace':
                    self._import_rows(connection, 'audit_logs', data['audit_logs'], ('id', 'session_id', 'actor_id', 'action', 'target', 'detail_json', 'created_at'))
                for table, rows in data.items():
                    counts[table] = len(rows) if table != 'audit_logs' or mode == 'replace' else 0
                self._seed_default_world(connection)
                self._insert_audit(connection, '', actor_id, 'backup.import', mode, counts)
                connection.execute('COMMIT')
                return counts
            except Exception:
                connection.execute('ROLLBACK')
                raise


    @staticmethod
    def _validate_merge_conflicts(
        connection: sqlite3.Connection,
        data: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        """Reject ambiguous identities before a non-destructive merge.

        A backup row may update an existing entity only when its stable ID is
        unchanged. If a natural unique key points at another ID, merging would
        silently join unrelated worlds, groups, players, or timeline records.
        """

        identity_specs: dict[str, tuple[str, ...]] = {
            "worlds": ("slug",),
            "characters": ("world_id", "slug"),
            "sessions": (
                "platform_id",
                "group_id",
                "instance_slug",
            ),
            "players": ("session_id", "user_id"),
            "events": ("seq",),
            "memories": ("session_id", "fingerprint"),
            "snapshots": ("session_id", "name"),
            "group_registry": ("platform_id", "group_id"),
        }
        for table, identity_columns in identity_specs.items():
            seen_ids: set[str] = set()
            seen_identities: dict[tuple[Any, ...], str] = {}
            for row in data.get(table, ()):
                if not isinstance(row, Mapping):
                    raise ValueError(f"备份表 {table} 含非法记录")
                required = ("id", *identity_columns)
                missing = [column for column in required if column not in row]
                if missing:
                    raise ValueError(
                        f"备份表 {table} 缺少字段 {missing[0]}"
                    )
                row_id = str(row["id"])
                if row_id in seen_ids:
                    raise ValueError(
                        f"备份表 {table} 含重复 ID：{row_id}"
                    )
                seen_ids.add(row_id)

                identity = tuple(row[column] for column in identity_columns)
                previous_id = seen_identities.get(identity)
                if previous_id and previous_id != row_id:
                    raise ValueError(
                        f"备份表 {table} 含重复唯一标识，"
                        "请检查备份或改用覆盖导入"
                    )
                seen_identities[identity] = row_id

                where = " AND ".join(
                    f"{column} = ?" for column in identity_columns
                )
                existing = connection.execute(
                    f"SELECT id FROM {table} WHERE {where}",
                    identity,
                ).fetchone()
                if existing and str(existing["id"]) != row_id:
                    raise ValueError(
                        f"备份表 {table} 的唯一标识已属于其他记录，"
                        "为避免串档已取消合并"
                    )

                if table == "events":
                    by_id = connection.execute(
                        "SELECT seq FROM events WHERE id = ?",
                        (row_id,),
                    ).fetchone()
                    if by_id and int(by_id["seq"]) != int(row["seq"]):
                        raise ValueError(
                            "时间线事件 ID 与序号不一致，"
                            "为避免历史错位已取消合并"
                        )

    @staticmethod
    def _import_rows(
        connection: sqlite3.Connection,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[str],
        *,
        merge: bool = False,
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        insert_sql = (
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
        )
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"备份表 {table} 含非法记录")
            values: list[Any] = []
            for column in columns:
                if column not in row:
                    raise ValueError(f"备份表 {table} 缺少字段 {column}")
                values.append(row[column])
            existing = None
            if merge:
                identity_columns = {
                    "instance_configs": "session_id",
                    "snapshot_workflows": "snapshot_id",
                    "session_archives": "session_id",
                    "session_rule_states": "session_id",
                    "dm_control_states": "session_id",
                    "session_character_states": "character_id",
                    "memory_governance": "memory_id",
                    "provider_health": "provider_id",
                    "configuration_revisions": "id",
                    "operation_receipts": "operation_id",
                    "group_registry": "id",
                    "story_storage": "session_id",
                    "timer_policies": "session_id",
                    "world_feature_versions": ("world_id", "world_revision", "feature_name"),
                    "world_entity_registry": ("world_id", "world_revision", "entity_ref"),
                    "operation_commits": "operation_id",
                    "resolution_receipts": "receipt_id",
                    "migration_receipts": "id",
                }.get(table, "id")
                if isinstance(identity_columns, str):
                    identity_columns = (identity_columns,)
                missing_identity = [column for column in identity_columns if column not in row]
                if missing_identity:
                    raise ValueError(f"备份表 {table} 缺少字段 {missing_identity[0]}")
                where = " AND ".join(f"{column}=?" for column in identity_columns)
                existing = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {where}",
                    tuple(row[column] for column in identity_columns),
                ).fetchone()
            if existing:
                # Merge is deliberately insert-only. Existing records are the
                # authoritative live copy; restoring an older backup must not
                # silently roll back a session, player profile, or event.
                continue
            connection.execute(insert_sql, values)
