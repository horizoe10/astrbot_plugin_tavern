"""SQLite store facade composed from domain repository mixins."""

from .database_support import *
from .repositories import (
    CurrentStateRepositoryMixin,
    WorldRepositoryMixin,
    SessionRepositoryMixin,
    StoryRepositoryMixin,
    RuleRepositoryMixin,
    CharacterRepositoryMixin,
    WorkflowRepositoryMixin,
    TimerRepositoryMixin,
    AdminRepositoryMixin,
    ControlRepositoryMixin,
    EconomyRepositoryMixin,
    DmRepositoryMixin,
    DeliveryRepositoryMixin,
)


class _ManagedConnection:
    """把 sqlite3.Connection 的 with 语义补全为「退出即关闭」。

    sqlite3 连接作为上下文管理器只管理事务、不会关闭连接；全仓
    ``with self._connect() as connection:`` 约 140 处都依赖该写法。
    此前连接要等 GC 才释放句柄，Windows 下临时目录清理（测试/备份）
    会因文件仍被占用抛 PermissionError；这里在退出时显式 commit/rollback
    并 close，行为等价且确定性关闭。

    注意（v0.12.0 评估记录）：曾尝试按线程复用连接以降低连接创建开销，
    但任何“复用窗口”都会让连接在操作结束后短暂滞留，导致 Windows 下
    临时目录 / 备份文件清理（严格模式）抛 WinError 32——这是 0.9.x
    引入「退出即关闭」的原始原因，属于承重设计。因此保持即开即关，
    性能优化改为：列表聚合用单条 SQL 汇总（见 dashboard_sessions）、
    RuleRuntime 缓存补强，以及提供 execute_read/execute_write 统一访问层。
    """

    __slots__ = ("connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is not None:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
            else:
                self.connection.commit()
        finally:
            self.connection.close()
        return False

    def close(self) -> None:
        self.connection.close()


class TavernDatabase(
    CurrentStateRepositoryMixin,
    WorldRepositoryMixin,
    SessionRepositoryMixin,
    StoryRepositoryMixin,
    RuleRepositoryMixin,
    CharacterRepositoryMixin,
    WorkflowRepositoryMixin,
    TimerRepositoryMixin,
    AdminRepositoryMixin,
    ControlRepositoryMixin,
    EconomyRepositoryMixin,
    DmRepositoryMixin,
    DeliveryRepositoryMixin,
):
    """SQLite persistence with short, explicit transactions.

    A fresh connection is used for each operation so methods can safely run in
    worker threads. Per-session async locks in the engine serialize story turns;
    optimistic revisions provide a second line of defense.
    """

    _SESSION_MUTATIONS = {
        "_ensure_session",
        "_clone_session",
        "_transition_session",
        "_finalize_session",
        "_save_manual_state",
        "_ensure_player",
        "_join_turn_order",
        "_leave_turn_order",
        "_skip_turn",
        "_set_turn_order",
        "_designate_turn",
        "_save_player",
        "_delete_player",
        "_append_ooc",
        "_save_memory",
        "_delete_memory",
        "_create_snapshot",
        "_restore_snapshot",
        "_restore_latest_auto",
        "_delete_snapshot",
        "_commit_turn_sync",
        "_save_instance_time_rules",
        "_save_session_rule_state",
        "_save_session_character",
        "_lock_check_result",
        "_reserve_participant",
        "_bind_card_code",
        "_fill_card_draft",
        "_reset_card_draft_stats",
        "_confirm_card_draft",
        "_cancel_card_draft",
        "_review_character_card",
        "_set_participant_ready",
        "_force_all_ready",
        "_activate_story",
        "_replace_active_choices",
        "_cast_vote",
        "_set_participant_away",
        "_return_to_queue",
        "_retire_participant",
        "_retire_and_ban",
        "_revoke_ban",
        "_request_return",
        "_control_timer",
        "_set_timer_policy",
        "_extend_active_timer",
        "_pause_session_timers",
        "_resume_session_timers",
        "_process_due_timers",
        "_grant_delegation",
        "_revoke_delegation",
        "_grant_permission",
        "_set_token_quota",
        "_set_group_token_quota",
        "_delete_session",
        "_write_audit",
        "_cleanup",
        "_import_bundle",
        "_enable_dm_mode",
        "_set_dm_directive",
        "_set_dm_handoff",
        "_finish_dm_handoff",
        "_disable_dm_mode",
        "_commit_dm_beat",
        "_resolve_action_intent",
        "_queue_delivery",
        "_finish_delivery",
        "_dismiss_delivery",
    }
    _ALL_SESSION_MUTATIONS = {
        "_process_due_timers",
        "_cleanup",
        "_import_bundle",
    }
    _ENTITY_SESSION_LOOKUPS = {
        "_delete_player": ("players", "id"),
        "_delete_memory": ("memories", "id"),
        "_delete_snapshot": ("snapshots", "id"),
        "_control_timer": ("timer_instances", "id"),
    }

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # v0.9.x is a deliberate clean baseline. It never opens or mutates
        # catalog.sqlite3/tavern.sqlite3 created by older releases.
        self.path = self.data_dir / "catalog_v090.sqlite3"
        self.migration_backup_path: Path | None = None
        self._prepare_migration_backup()
        self._schema_lock = threading.Lock()
        self._initialize()
        self.storage = InstanceStorage(
            data_dir=self.data_dir,
            catalog_path=self.path,
            connect_catalog=self._connect,
            schema_version=DATABASE_SCHEMA_VERSION,
        )
        self.storage.bootstrap()

    def _prepare_migration_backup(self) -> None:
        if not self.path.exists():
            return
        try:
            # closing：sqlite3 连接上下文管理器不关闭连接，Windows 下
            # 会让备份文件残留句柄导致目录清理失败。
            with closing(sqlite3.connect(self.path)) as source:
                row = source.execute(
                    "SELECT value FROM tavern_meta WHERE key='schema_version'"
                ).fetchone()
                current = int(row[0]) if row else 0
                if current >= DATABASE_SCHEMA_VERSION:
                    return
                target = self.data_dir / (
                    f"catalog_v090.schema{current}.before_schema{DATABASE_SCHEMA_VERSION}."
                    f"{uuid.uuid4().hex[:8]}.sqlite3"
                )
                with closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
                self.migration_backup_path = target
        except (sqlite3.Error, OSError, TypeError, ValueError):
            self.migration_backup_path = None

    def _connect(self) -> _ManagedConnection:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return _ManagedConnection(connection)

    # ── v0.12.0（结构优化）：统一 DB 访问助手 ──────────────────────────
    # 新增模块（dashboard / world_market 等只读聚合）优先使用这两个助手，
    # 收敛「自行 with self._connect()」的散点写法；语义与既有 _run 一致：
    # 在 worker 线程执行、读操作退出即提交（无副作用）、写操作显式提交。
    async def execute_read(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """执行只读 SQL 并返回 dict 行列表（在 worker 线程中运行）。"""
        return await self._run(self._execute_read, sql, tuple(params))

    def _execute_read(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(str(sql), params).fetchall()
        return [dict(row) for row in rows]

    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """执行单条写入 SQL（隐式提交）；返回受影响行数。"""
        return await self._run(self._execute_write, sql, tuple(params))

    def _execute_write(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(str(sql), params)
        return cursor.rowcount if cursor.rowcount >= 0 else 0

    def _initialize(self) -> None:
        with self._schema_lock:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tavern_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS worlds (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        display_no INTEGER NOT NULL,
                        sort_order INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        rules_json TEXT NOT NULL DEFAULT '{}',
                        extensions_json TEXT NOT NULL DEFAULT '{}',
                        opening_scene TEXT NOT NULL DEFAULT '',
                        initial_state_json TEXT NOT NULL DEFAULT '{}',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS characters (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id)
                            ON DELETE CASCADE,
                        slug TEXT NOT NULL,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'npc',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        prompt TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(world_id, slug)
                    );

                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        instance_slug TEXT NOT NULL,
                        instance_name TEXT NOT NULL,
                        selected INTEGER NOT NULL DEFAULT 0,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        state TEXT NOT NULL DEFAULT 'closed'
                            CHECK(state IN (
                                'closed', 'preparing', 'running', 'paused',
                                'finished', 'maintenance'
                            )),
                        turn_no INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        world_state_json TEXT NOT NULL DEFAULT '{}',
                        history_floor_seq INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id, instance_slug)
                    );

                    CREATE TABLE IF NOT EXISTS players (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        turn_no INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        actor_name TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_session_seq
                        ON events(session_id, seq DESC);

                    -- 0.11.1：恢复存档的 MAX(seq)…AND created_at<=? 查询
                    -- 此前无法利用 session 索引，会整段全扫。
                    CREATE INDEX IF NOT EXISTS idx_events_created_at
                        ON events(created_at);

                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        scope TEXT NOT NULL DEFAULT 'world',
                        scope_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'fact',
                        content TEXT NOT NULL,
                        importance INTEGER NOT NULL DEFAULT 3,
                        salience REAL NOT NULL DEFAULT 1,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        fingerprint TEXT NOT NULL,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_accessed_at TEXT NOT NULL,
                        UNIQUE(session_id, fingerprint)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memories_session
                        ON memories(session_id, importance DESC, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS snapshots (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'manual',
                        turn_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        world_id TEXT NOT NULL,
                        world_state_json TEXT NOT NULL,
                        created_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, name)
                    );

                    CREATE INDEX IF NOT EXISTS idx_snapshots_session
                        ON snapshots(session_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        target TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_audit_session
                        ON audit_logs(session_id, id DESC);
                    """
                )
                connection.executescript(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_selected_group
                    ON sessions(platform_id, group_id)
                    WHERE selected = 1;

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_running_group
                    ON sessions(platform_id, group_id)
                    WHERE state = 'running';

                    CREATE INDEX IF NOT EXISTS idx_sessions_group
                    ON sessions(platform_id, group_id, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS instance_configs (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL DEFAULT 1,
                        world_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        time_rules_json TEXT NOT NULL DEFAULT '{}',
                        phase_meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS character_cards (
                        id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        display_name TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        current_version INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_character_cards_owner
                    ON character_cards(owner_user_id, world_id, archived);

                    CREATE TABLE IF NOT EXISTS character_card_versions (
                        id TEXT PRIMARY KEY,
                        character_card_id TEXT NOT NULL
                            REFERENCES character_cards(id) ON DELETE CASCADE,
                        version_no INTEGER NOT NULL,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending_review'
                            CHECK(status IN (
                                'draft', 'pending_review', 'approved',
                                'rejected'
                            )),
                        review_note TEXT NOT NULL DEFAULT '',
                        reviewed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(character_card_id, version_no)
                    );

                    CREATE TABLE IF NOT EXISTS participants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        player_id TEXT REFERENCES players(id)
                            ON DELETE SET NULL,
                        group_user_id TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        display_name TEXT NOT NULL,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        character_version_id TEXT
                            REFERENCES character_card_versions(id)
                            ON DELETE SET NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        character_code TEXT NOT NULL DEFAULT '',
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        card_status TEXT NOT NULL DEFAULT 'uncreated'
                            CHECK(card_status IN (
                                'uncreated', 'draft', 'pending_review',
                                'approved', 'rejected'
                            )),
                        ready INTEGER NOT NULL DEFAULT 0,
                        participation_status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(participation_status IN (
                                'reserved', 'active', 'standby', 'away',
                                'retired', 'archived'
                            )),
                        seat_reserved_at TEXT NOT NULL,
                        joined_round INTEGER NOT NULL DEFAULT 1,
                        consecutive_timeouts INTEGER NOT NULL DEFAULT 0,
                        exit_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, group_user_id)
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_participant_code_session
                    ON participants(session_id, character_code)
                    WHERE character_code <> ''
                      AND participation_status NOT IN ('retired', 'archived');

                    CREATE TABLE IF NOT EXISTS character_runtime_states (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, participant_id)
                    );

                    CREATE TABLE IF NOT EXISTS character_card_drafts (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        fields_json TEXT NOT NULL DEFAULT '{}',
                        current_step INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'confirmed', 'cancelled', 'expired'
                            )),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(participant_id)
                    );

                    CREATE TABLE IF NOT EXISTS card_binding_codes (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        code TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'used', 'expired')),
                        expires_at TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        used_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS choice_sets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        round_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        choices_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'selected', 'superseded',
                                'cancelled'
                            )),
                        reroll_count INTEGER NOT NULL DEFAULT 0,
                        selected_key TEXT NOT NULL DEFAULT '',
                        flavor_text TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_choice_active_session
                    ON choice_sets(session_id)
                    WHERE status = 'active';

                    CREATE TABLE IF NOT EXISTS rolls (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        choice_set_id TEXT REFERENCES choice_sets(id)
                            ON DELETE SET NULL,
                        participant_id TEXT REFERENCES participants(id)
                            ON DELETE SET NULL,
                        roll_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS group_votes (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        question TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        eligible_user_ids_json TEXT NOT NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'open'
                            CHECK(status IN (
                                'open', 'passed', 'rejected', 'cancelled'
                            )),
                        winner_key TEXT NOT NULL DEFAULT '',
                        suspended_user_id TEXT NOT NULL DEFAULT '',
                        deadline_at TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_group_vote_open_session
                    ON group_votes(session_id)
                    WHERE status = 'open';

                    CREATE TABLE IF NOT EXISTS vote_ballots (
                        id TEXT PRIMARY KEY,
                        vote_id TEXT NOT NULL REFERENCES group_votes(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        option_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(vote_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS selected_world_events (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        round_no INTEGER NOT NULL,
                        pool_item_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'selected'
                            CHECK(status IN (
                                'selected', 'narrated', 'cancelled'
                            )),
                        narrative TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        resolved_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(session_id, round_no)
                    );

                    CREATE TABLE IF NOT EXISTS timer_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        participant_id TEXT NOT NULL DEFAULT '',
                        timer_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'paused', 'expired', 'cancelled',
                                'completed'
                            )),
                        deadline_at TEXT NOT NULL DEFAULT '',
                        remaining_seconds INTEGER,
                        reminder_at TEXT NOT NULL DEFAULT '',
                        reminder_sent INTEGER NOT NULL DEFAULT 0,
                        action_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_timers_due
                    ON timer_instances(status, deadline_at);

                    CREATE TABLE IF NOT EXISTS delegation_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        owner_user_id TEXT NOT NULL,
                        delegate_user_id TEXT NOT NULL,
                        permissions_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'expired', 'revoked'
                            )),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS permission_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL
                            CHECK(role IN ('host', 'moderator')),
                        granted_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id, role)
                    );

                    CREATE TABLE IF NOT EXISTS ban_records (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        platform_id TEXT NOT NULL DEFAULT '',
                        group_id TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL
                            CHECK(scope IN ('instance', 'group', 'global')),
                        reason TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'expired', 'revoked')),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_bans_target
                    ON ban_records(user_id, status, scope);

                    CREATE TABLE IF NOT EXISTS return_requests (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        requested_by TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'requested'
                            CHECK(status IN (
                                'requested', 'voting', 'quest_active',
                                'completed', 'rejected', 'cancelled'
                            )),
                        exit_type TEXT NOT NULL DEFAULT 'departure',
                        objective TEXT NOT NULL DEFAULT '',
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        vote_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS snapshot_workflows (
                        snapshot_id TEXT PRIMARY KEY REFERENCES snapshots(id)
                            ON DELETE CASCADE,
                        workflow_json TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE TABLE IF NOT EXISTS session_archives (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        termination_type TEXT NOT NULL
                            CHECK(termination_type IN ('completed', 'aborted')),
                        reason TEXT NOT NULL DEFAULT '',
                        final_snapshot_id TEXT NOT NULL
                            REFERENCES snapshots(id),
                        ended_by TEXT NOT NULL,
                        ended_at TEXT NOT NULL,
                        readonly INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS session_rule_states (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        content_boundaries_json TEXT NOT NULL DEFAULT '{}',
                        npc_policy_json TEXT NOT NULL DEFAULT '{}',
                        context_budget_json TEXT NOT NULL DEFAULT '{}',
                        dice_rules_json TEXT NOT NULL DEFAULT '{}',
                        recovery_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS dm_control_states (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        mode TEXT NOT NULL DEFAULT 'auto'
                            CHECK(mode IN ('auto', 'dm')),
                        active_dm_user_id TEXT NOT NULL DEFAULT '',
                        phase TEXT NOT NULL DEFAULT 'auto'
                            CHECK(phase IN (
                                'auto', 'awaiting_dm', 'generating',
                                'player_handoff', 'npc_handoff'
                            )),
                        directive TEXT NOT NULL DEFAULT '',
                        beat_no INTEGER NOT NULL DEFAULT 0,
                        current_actor_type TEXT NOT NULL DEFAULT '',
                        current_actor_ref TEXT NOT NULL DEFAULT '',
                        preserved_turn_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS session_characters (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        name TEXT NOT NULL,
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        role_type TEXT NOT NULL DEFAULT 'npc',
                        public_profile_json TEXT NOT NULL DEFAULT '{}',
                        known_facts_json TEXT NOT NULL DEFAULT '[]',
                        misconceptions_json TEXT NOT NULL DEFAULT '[]',
                        source TEXT NOT NULL DEFAULT 'model_generated'
                            CHECK(source IN (
                                'world_preset', 'model_generated', 'admin'
                            )),
                        review_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(review_status IN (
                                'pending', 'approved', 'rejected',
                                'duplicate'
                            )),
                        lifecycle_status TEXT NOT NULL DEFAULT 'active'
                            CHECK(lifecycle_status IN (
                                'active', 'departed', 'dead', 'archived'
                            )),
                        persistent INTEGER NOT NULL DEFAULT 1,
                        first_event_id TEXT NOT NULL DEFAULT '',
                        last_event_id TEXT NOT NULL DEFAULT '',
                        first_turn INTEGER NOT NULL DEFAULT 0,
                        last_turn INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_session_characters_context
                    ON session_characters(
                        session_id, lifecycle_status, last_turn DESC
                    );

                    CREATE TABLE IF NOT EXISTS session_character_states (
                        character_id TEXT PRIMARY KEY
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS story_ledger (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'objective'
                            CHECK(kind IN (
                                'main', 'side', 'objective', 'clue',
                                'milestone', 'failed'
                            )),
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'failed', 'archived'
                            )),
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host')),
                        source_event_id TEXT NOT NULL DEFAULT '',
                        completed_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE TABLE IF NOT EXISTS scene_clocks (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        segments INTEGER NOT NULL
                            CHECK(segments IN (4, 6, 8)),
                        current_value INTEGER NOT NULL DEFAULT 0,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'vague', 'hidden')),
                        trigger_text TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'archived'
                            )),
                        triggered_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE TABLE IF NOT EXISTS memory_governance (
                        memory_id TEXT PRIMARY KEY REFERENCES memories(id)
                            ON DELETE CASCADE,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host', 'private')),
                        locked INTEGER NOT NULL DEFAULT 0,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        invalidated INTEGER NOT NULL DEFAULT 0,
                        supersedes_id TEXT NOT NULL DEFAULT '',
                        conflict_status TEXT NOT NULL DEFAULT 'clear'
                            CHECK(conflict_status IN (
                                'clear', 'conflict', 'resolved'
                            )),
                        note TEXT NOT NULL DEFAULT '',
                        updated_by TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS assist_tokens (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        target_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        stat TEXT NOT NULL DEFAULT '',
                        method TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'consumed', 'expired')),
                        expires_round INTEGER NOT NULL DEFAULT 0,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        consumed_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS roll_revisions (
                        id TEXT PRIMARY KEY,
                        roll_id TEXT NOT NULL REFERENCES rolls(id)
                            ON DELETE CASCADE,
                        revision_no INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        previous_json TEXT NOT NULL,
                        revised_json TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(roll_id, revision_no)
                    );

                    CREATE TABLE IF NOT EXISTS inspiration_transactions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        delta INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        operation_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'healthy'
                            CHECK(status IN ('healthy', 'open', 'half_open')),
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_failure_reason TEXT NOT NULL DEFAULT '',
                        last_failure_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        circuit_until TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS configuration_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        saved_by TEXT NOT NULL,
                        saved_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        operation_type TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'completed'
                            CHECK(status IN (
                                'pending', 'completed', 'failed'
                            )),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_operations_session_status
                    ON operation_receipts(session_id, status, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS world_feature_versions (
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        feature_name TEXT NOT NULL,
                        feature_version TEXT NOT NULL,
                        required INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(world_id, world_revision, feature_name)
                    );

                    CREATE TABLE IF NOT EXISTS world_entity_registry (
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        entity_ref TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        definition_json TEXT NOT NULL DEFAULT '{}',
                        content_hash TEXT NOT NULL,
                        visibility TEXT NOT NULL DEFAULT 'world',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(world_id, world_revision, entity_ref)
                    );

                    CREATE TABLE IF NOT EXISTS world_rule_revisions (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        rules_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(world_id, world_revision)
                    );

                    CREATE TABLE IF NOT EXISTS world_snapshots (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE RESTRICT,
                        world_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(world_id, world_revision, content_hash)
                    );

                    CREATE TABLE IF NOT EXISTS actor_capability_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        actor_ref TEXT NOT NULL,
                        capability_ref TEXT NOT NULL,
                        definition_version INTEGER NOT NULL DEFAULT 1,
                        source_ref TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        persistence_scope TEXT NOT NULL DEFAULT 'campaign',
                        available INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, actor_ref, capability_ref, source_ref)
                    );

                    CREATE TABLE IF NOT EXISTS runtime_effect_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        target_ref TEXT NOT NULL,
                        effect_ref TEXT NOT NULL,
                        source_ref TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        duration_json TEXT NOT NULL DEFAULT '{}',
                        persistence_scope TEXT NOT NULL DEFAULT 'session',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS operation_commits (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        input_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        rollback_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS resolution_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL DEFAULT '',
                        world_snapshot_id TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL,
                        receipt_json TEXT NOT NULL,
                        public_projection_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS migration_receipts (
                        id TEXT PRIMARY KEY,
                        migration_type TEXT NOT NULL,
                        source_version TEXT NOT NULL DEFAULT '',
                        target_version TEXT NOT NULL DEFAULT '',
                        world_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        operation_id TEXT NOT NULL DEFAULT '',
                        receipt_json TEXT NOT NULL,
                        confirmed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_world_features_revision
                    ON world_feature_versions(world_id, world_revision);
                    CREATE INDEX IF NOT EXISTS idx_world_entities_type
                    ON world_entity_registry(world_id, world_revision, entity_type);
                    CREATE INDEX IF NOT EXISTS idx_actor_capabilities
                    ON actor_capability_instances(session_id, actor_ref, available);
                    CREATE INDEX IF NOT EXISTS idx_runtime_effect_target
                    ON runtime_effect_instances(session_id, target_ref, status);
                    CREATE INDEX IF NOT EXISTS idx_resolution_receipts_session
                    ON resolution_receipts(session_id, created_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_migration_operation
                    ON migration_receipts(operation_id) WHERE operation_id <> '';

                    CREATE TABLE IF NOT EXISTS card_revision_requests (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        character_card_id TEXT NOT NULL
                            REFERENCES character_cards(id) ON DELETE CASCADE,
                        base_version_id TEXT NOT NULL
                            REFERENCES character_card_versions(id),
                        candidate_version_id TEXT NOT NULL
                            REFERENCES character_card_versions(id),
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
                        request_note TEXT NOT NULL DEFAULT '',
                        review_note TEXT NOT NULL DEFAULT '',
                        requested_by TEXT NOT NULL,
                        reviewed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_card_revision_session
                    ON card_revision_requests(session_id, status, created_at DESC);

                    CREATE TABLE IF NOT EXISTS group_registry (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        remark TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_group_registry_remark
                    ON group_registry(remark, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS story_storage (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_registry_id TEXT NOT NULL
                            REFERENCES group_registry(id) ON DELETE CASCADE,
                        relative_path TEXT NOT NULL UNIQUE,
                        playthrough_no INTEGER NOT NULL DEFAULT 1,
                        created_stamp TEXT NOT NULL,
                        last_synced_revision INTEGER NOT NULL DEFAULT 0,
                        last_checksum TEXT NOT NULL DEFAULT '',
                        sync_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(sync_status IN (
                                'pending', 'ready', 'error'
                            )),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_story_storage_group
                    ON story_storage(group_registry_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS timer_policies (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        global_enabled INTEGER NOT NULL DEFAULT 0,
                        switches_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL DEFAULT 'system',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS token_usage (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_id TEXT NOT NULL,
                        request_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL DEFAULT '',
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        reserved_tokens INTEGER NOT NULL DEFAULT 0,
                        usage_source TEXT NOT NULL DEFAULT 'estimated',
                        status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(status IN (
                                'reserved', 'completed', 'failed'
                            )),
                        created_at TEXT NOT NULL,
                        settled_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_token_usage_session_time
                    ON token_usage(session_id, created_at DESC);

                    CREATE INDEX IF NOT EXISTS idx_token_usage_group_time
                    ON token_usage(group_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS token_quota_policies (
                        id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL
                            CHECK(scope_type IN ('group', 'session')),
                        scope_id TEXT NOT NULL,
                        window_seconds INTEGER NOT NULL,
                        token_limit INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(scope_type, scope_id)
                    );

                    -- v0.12.0-A16：经济系统（可选，世界包 economy 块驱动）
                    CREATE TABLE IF NOT EXISTS economy_state (
                        session_id TEXT PRIMARY KEY
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS economy_currencies (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        currency_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        short_name TEXT NOT NULL DEFAULT '',
                        icon TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        precision INTEGER NOT NULL DEFAULT 0,
                        allow_negative INTEGER NOT NULL DEFAULT 0,
                        transferable INTEGER NOT NULL DEFAULT 1,
                        exchangeable INTEGER NOT NULL DEFAULT 0,
                        public INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        extensions_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, currency_id)
                    );

                    CREATE TABLE IF NOT EXISTS economy_wallets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        owner_type TEXT NOT NULL,
                        owner_ref TEXT NOT NULL,
                        currency_id TEXT NOT NULL,
                        balance INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, owner_type, owner_ref, currency_id)
                    );

                    CREATE TABLE IF NOT EXISTS economy_transactions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        operation_id TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        currency_id TEXT NOT NULL,
                        from_owner_type TEXT NOT NULL DEFAULT '',
                        from_owner_ref TEXT NOT NULL DEFAULT '',
                        to_owner_type TEXT NOT NULL DEFAULT '',
                        to_owner_ref TEXT NOT NULL DEFAULT '',
                        amount INTEGER NOT NULL DEFAULT 0,
                        balance_before INTEGER NOT NULL DEFAULT 0,
                        balance_after INTEGER NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL DEFAULT '',
                        target_ref TEXT NOT NULL DEFAULT '',
                        event_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'committed'
                            CHECK(status IN (
                                'committed', 'reverted', 'failed'
                            )),
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS economy_exchange_rules (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        from_currency TEXT NOT NULL,
                        to_currency TEXT NOT NULL,
                        rate_numerator INTEGER NOT NULL DEFAULT 1,
                        rate_denominator INTEGER NOT NULL DEFAULT 1,
                        fee INTEGER NOT NULL DEFAULT 0,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, from_currency, to_currency)
                    );

                    CREATE INDEX IF NOT EXISTS idx_economy_tx_session_time
                    ON economy_transactions(session_id, created_at DESC);

                    -- v0.12.0-A16：统一行动操作上下文（幂等/审计/重放防护）
                    CREATE TABLE IF NOT EXISTS action_operations (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        operator_id TEXT NOT NULL DEFAULT '',
                        context_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'committed', 'failed', 'cancelled'
                            )),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_action_ops_session
                    ON action_operations(session_id, created_at DESC);

                    -- B1：跨平台主动通知失败后的持久化补偿队列。
                    CREATE TABLE IF NOT EXISTS notification_outbox (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        origin TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'notice',
                        text TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'sent', 'delivered_on_reply',
                                'dismissed'
                            )),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        dedupe_key TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        delivered_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
                    ON notification_outbox(status, origin, created_at);

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedupe
                    ON notification_outbox(dedupe_key)
                    WHERE dedupe_key <> '' AND status = 'pending';
                    """
                )
                self._migrate_schema_10(connection)
                self._migrate_schema_11(connection)
                self._migrate_schema_12(connection)
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(DATABASE_SCHEMA_VERSION),),
                )
                self._seed_default_world(connection)
                self._backfill_world_revisions(connection)

    def _migrate_schema_10(self, connection: sqlite3.Connection) -> None:
        """Idempotently add stable world numbering and record deterministic backfill."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(worlds)").fetchall()
        }
        if "display_no" not in columns:
            connection.execute("ALTER TABLE worlds ADD COLUMN display_no INTEGER")
        if "sort_order" not in columns:
            connection.execute("ALTER TABLE worlds ADD COLUMN sort_order INTEGER")
        rows = connection.execute(
            "SELECT id, slug, display_no, sort_order FROM worlds ORDER BY created_at ASC, id ASC"
        ).fetchall()
        declared = {
            int(row["display_no"])
            for row in rows
            if row["display_no"] is not None and int(row["display_no"]) > 0
        }
        used: set[int] = set()
        next_number = max(declared, default=0) + 1
        mapping: list[dict[str, Any]] = []
        for row in rows:
            display_no = int(row["display_no"] or 0)
            if display_no <= 0 or display_no in used:
                while next_number in used:
                    next_number += 1
                display_no = next_number
                next_number += 1
            used.add(display_no)
            sort_order = int(row["sort_order"] or display_no)
            connection.execute(
                "UPDATE worlds SET display_no=?, sort_order=? WHERE id=?",
                (display_no, sort_order, row["id"]),
            )
            mapping.append(
                {"world_id": row["id"], "slug": row["slug"],
                 "display_no": display_no, "sort_order": sort_order}
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_worlds_display_no ON worlds(display_no)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_worlds_sort ON worlds(archived, sort_order, display_no)"
        )
        next_display_no = max(used, default=0) + 1
        current_counter = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='next_world_display_no'"
        ).fetchone()
        if current_counter:
            try:
                next_display_no = max(next_display_no, int(current_counter["value"]))
            except (TypeError, ValueError):
                pass
        connection.execute(
            """
            INSERT INTO tavern_meta(key, value) VALUES ('next_world_display_no', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(next_display_no),),
        )
        if mapping:
            connection.execute(
                """
                INSERT OR IGNORE INTO migration_receipts(
                    id, migration_type, source_version, target_version,
                    receipt_json, created_at
                ) VALUES ('schema_9_to_10', 'database_schema', '9', '10', ?, ?)
                """,
                (json_dump({"worlds": mapping}), utc_now()),
            )

    def _migrate_schema_11(self, connection: sqlite3.Connection) -> None:
        """v0.12.0-A16：托管/代操作与输入锁字段（幂等 ALTER）。"""
        def _columns(table: str) -> set[str]:
            return {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

        delegation = _columns("delegation_grants")
        for column, ddl in (
            ("expiry_kind", "expiry_kind TEXT NOT NULL DEFAULT 'none'"),
            ("expires_round", "expires_round INTEGER NOT NULL DEFAULT 0"),
            ("auto_restore", "auto_restore INTEGER NOT NULL DEFAULT 0"),
            ("source", "source TEXT NOT NULL DEFAULT 'player'"),
            ("granted_by", "granted_by TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in delegation:
                connection.execute(
                    f"ALTER TABLE delegation_grants ADD COLUMN {ddl}"
                )

        participants = _columns("participants")
        if "action_locked" not in participants:
            connection.execute(
                "ALTER TABLE participants ADD COLUMN action_locked "
                "INTEGER NOT NULL DEFAULT 0"
            )

        sessions = _columns("sessions")
        if "input_locked" not in sessions:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN input_locked "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_schema_12(self, connection: sqlite3.Connection) -> None:
        """B1：记录通知补偿队列迁移，不改写任何旧存档内容。"""

        connection.execute(
            """
            INSERT OR IGNORE INTO migration_receipts(
                id, migration_type, source_version, target_version,
                receipt_json, created_at
            ) VALUES ('schema_11_to_12', 'database_schema', '11', '12', ?, ?)
            """,
            (
                json_dump(
                    {
                        "added": ["notification_outbox"],
                        "preserved": [
                            "sessions",
                            "participants",
                            "worlds",
                            "snapshots",
                            "story_storage",
                        ],
                    }
                ),
                utc_now(),
            ),
        )

    @staticmethod
    def _stable_key(value: Any, fallback: str = "") -> str:
        text = " ".join(str(value or "").strip().casefold().split())
        if text:
            return text[:160]
        return str(fallback or uuid.uuid4().hex)[:160]

    def _seed_default_world(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT id FROM worlds WHERE slug = ?",
            (DEFAULT_WORLD["slug"],),
        ).fetchone()
        if existing:
            return
        now = utc_now()
        world_id = new_id("world")
        display_no = int(
            connection.execute(
                "SELECT COALESCE(MAX(display_no), 0) + 1 FROM worlds"
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO worlds(
                id, slug, display_no, sort_order, name, description, system_prompt, rules_json,
                extensions_json,
                opening_scene, initial_state_json, archived, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                world_id,
                DEFAULT_WORLD["slug"],
                display_no,
                display_no,
                DEFAULT_WORLD["name"],
                DEFAULT_WORLD["description"],
                DEFAULT_WORLD["system_prompt"],
                json_dump(DEFAULT_WORLD["rules"]),
                json_dump(
                    {
                        key: value
                        for key, value in DEFAULT_WORLD.items()
                        if key not in {
                            "slug", "name", "description", "system_prompt",
                            "rules", "opening_scene", "initial_state",
                            "world_schema_version", "capabilities",
                        }
                    }
                ),
                DEFAULT_WORLD["opening_scene"],
                json_dump(DEFAULT_WORLD["initial_state"]),
                now,
                now,
            ),
        )
        for character in DEFAULT_CHARACTERS:
            connection.execute(
                """
                INSERT INTO characters(
                    id, world_id, slug, name, role, profile_json, prompt,
                    enabled, sort_order, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
                """,
                (
                    new_id("character"),
                    world_id,
                    character["slug"],
                    character["name"],
                    character["role"],
                    json_dump(character["profile"]),
                    character["prompt"],
                    character["sort_order"],
                    now,
                    now,
                ),
            )
        row = connection.execute(
            "SELECT * FROM worlds WHERE id=?", (world_id,)
        ).fetchone()
        if row is not None:
            self._persist_world_revision(
                connection, row, self._world(row), now
            )

    def _backfill_world_revisions(self, connection: sqlite3.Connection) -> None:
        """Give pre-v0.11 worlds an immutable first rule revision and snapshot."""

        now = utc_now()
        for row in connection.execute(
            "SELECT * FROM worlds ORDER BY display_no ASC, id ASC"
        ).fetchall():
            exists = connection.execute(
                """
                SELECT 1 FROM world_snapshots
                WHERE world_id=? AND world_revision=?
                """,
                (row["id"], row["revision"]),
            ).fetchone()
            if exists:
                continue
            self._persist_world_revision(
                connection, row, self._world(row), now
            )

    @staticmethod
    def _candidate_session_values(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) == "session_id" and item:
                    result.add(str(item))
                result.update(TavernDatabase._candidate_session_values(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                result.update(TavernDatabase._candidate_session_values(item))
        elif isinstance(value, str) and value.startswith("session_"):
            result.add(value)
        return result

    def _all_session_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM sessions"
                ).fetchall()
            }

    def _affected_session_ids(
        self,
        method_name: str,
        args: Sequence[Any],
        result: Any = None,
    ) -> set[str]:
        if method_name in self._ALL_SESSION_MUTATIONS:
            return self._all_session_ids()
        candidates: set[str] = set()
        if method_name == "_set_group_token_quota" and len(args) >= 2:
            with self._connect() as connection:
                candidates.update(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT id FROM sessions
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (str(args[0]), str(args[1])),
                    ).fetchall()
                )
        for value in (*args, result):
            candidates.update(self._candidate_session_values(value))
        if args and isinstance(args[0], str):
            candidates.add(str(args[0]))
        with self._connect() as connection:
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                candidates = {
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT id FROM sessions
                        WHERE id IN ({placeholders})
                        """,
                        tuple(candidates),
                    ).fetchall()
                }
            lookup = self._ENTITY_SESSION_LOOKUPS.get(method_name)
            if not candidates and lookup and args:
                table, column = lookup
                row = connection.execute(
                    f"""
                    SELECT session_id FROM {table}
                    WHERE {column} = ?
                    """,
                    (str(args[0]),),
                ).fetchone()
                if row:
                    candidates.add(str(row[0]))
            if not candidates and method_name == "_cancel_card_draft" and args:
                row = connection.execute(
                    """
                    SELECT pt.session_id
                    FROM character_card_drafts draft
                    JOIN participants pt ON pt.id = draft.participant_id
                    WHERE pt.private_origin = ?
                    ORDER BY draft.created_at DESC LIMIT 1
                    """,
                    (str(args[0]),),
                ).fetchone()
                if row:
                    candidates.add(str(row[0]))
        return candidates

    def _sync_after_mutation(
        self,
        method_name: str,
        args: Sequence[Any],
        result: Any,
        before: set[str],
    ) -> None:
        session_ids = before | self._affected_session_ids(
            method_name,
            args,
            result,
        )
        # 0.11.1 暂缓：文件同步仍保持内联执行（整库拷贝 + 裁剪 + VACUUM
        # 的成本为已知限制，后台线程方案会与测试/停机清理产生句柄竞争）。
        for session_id in sorted(session_ids):
            try:
                self.storage.sync_session(session_id)
            except Exception:
                # The catalog transaction has already committed. The storage
                # row records the error and startup bootstrap retries it.
                continue

        if method_name == "_create_snapshot":
            for session_id in sorted(session_ids):
                try:
                    self.storage.create_archive(
                        session_id,
                        kind="save",
                        reason="手动命名存档",
                        refresh=False,
                    )
                except Exception:
                    continue
        elif method_name == "_finalize_session":
            for session_id in sorted(session_ids):
                try:
                    self.storage.create_archive(
                        session_id,
                        kind="save",
                        reason="副本最终存档",
                        refresh=False,
                    )
                except Exception:
                    continue
        elif method_name == "_commit_turn_sync" and result:
            interval = int(args[12] or 0) if len(args) > 12 else 0
            turn_no = int(
                result.get("turn_no", 0)
                if isinstance(result, Mapping)
                else 0
            )
            if interval > 0 and turn_no > 0 and turn_no % interval == 0:
                for session_id in sorted(session_ids):
                    try:
                        self.storage.create_archive(
                            session_id,
                            kind="backup",
                            reason=f"第 {turn_no} 回合自动安全备份",
                            refresh=False,
                        )
                    except Exception:
                        continue

    def _mark_storage_pending(self, session_ids: Sequence[str]) -> None:
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE story_storage SET
                    sync_status = 'pending', last_error = '',
                    updated_at = ?
                WHERE session_id IN ({placeholders})
                """,
                (utc_now(), *session_ids),
            )

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def invoke() -> T:
            method_name = fn.__name__
            before: set[str] = set()
            if (
                method_name in self._SESSION_MUTATIONS
                and hasattr(self, "storage")
            ):
                before = self._affected_session_ids(
                    method_name,
                    args,
                )
                self._mark_storage_pending(sorted(before))
            result = fn(*args)
            if (
                method_name in self._SESSION_MUTATIONS
                and hasattr(self, "storage")
            ):
                self._sync_after_mutation(
                    method_name,
                    args,
                    result,
                    before,
                )
            return result

        return await asyncio.to_thread(invoke)

    @staticmethod
    def _world(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "slug": row["slug"],
            "display_no": int(row["display_no"]),
            "sort_order": int(row["sort_order"]),
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "rules": json_load(row["rules_json"], {}),
            "opening_scene": row["opening_scene"],
            "initial_state": json_load(row["initial_state_json"], {}),
            "archived": bool(row["archived"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        extensions = json_load(row["extensions_json"], {})
        if isinstance(extensions, Mapping):
            for key, value in extensions.items():
                if key not in result:
                    result[str(key)] = value
        result["world_schema_version"] = int(result["rules"].get("world_schema_version", 0))
        result["capabilities"] = dict(result["rules"].get("capabilities") or {})
        result["player_limits"] = player_limits(result)
        result["card_template"] = card_template(result)
        result["time_rules"] = world_time_rules(result)
        rules = result["rules"]
        result["choice_mode"] = (
            "strict_abcd"
            if bool(rules.get("strict_choices", True))
            else "free_text"
        )
        result["check_density"] = str(
            rules.get("check_density", "standard")
        )
        return result

    @staticmethod
    def _character(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "world_id": row["world_id"],
            "slug": row["slug"],
            "name": row["name"],
            "role": row["role"],
            "profile": json_load(row["profile_json"], {}),
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "sort_order": row["sort_order"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        stored_world_state = json_load(row["world_state_json"], {})
        result = {
            "id": row["id"],
            "platform_id": row["platform_id"],
            "group_id": row["group_id"],
            "unified_origin": row["unified_origin"],
            "instance_slug": (
                row["instance_slug"]
                if "instance_slug" in keys
                else row["world_slug"]
            ),
            "instance_name": (
                row["instance_name"]
                if "instance_name" in keys
                else row["world_name"]
            ),
            "selected": bool(row["selected"]) if "selected" in keys else True,
            "world_id": row["world_id"],
            "state": row["state"],
            "turn_no": row["turn_no"],
            "revision": row["revision"],
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "history_floor_seq": row["history_floor_seq"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if "world_name" in keys:
            result["world_name"] = row["world_name"]
        if "world_slug" in keys:
            result["world_slug"] = row["world_slug"]
        if "world_description" in keys:
            result["world_description"] = str(
                row["world_description"] or ""
            )
        if "group_remark" in keys:
            result["group_remark"] = str(row["group_remark"] or "")
        if "group_revision" in keys:
            result["group_revision"] = int(row["group_revision"] or 1)
        if "storage_relative_path" in keys:
            result["storage_relative_path"] = str(
                row["storage_relative_path"] or ""
            )
        if "storage_sync_status" in keys:
            result["storage_sync_status"] = str(
                row["storage_sync_status"] or "pending"
            )
        if "storage_last_error" in keys:
            result["storage_last_error"] = str(
                row["storage_last_error"] or ""
            )
        if "playthrough_no" in keys:
            result["playthrough_no"] = int(row["playthrough_no"] or 1)
        if "player_count" in keys:
            result["player_count"] = row["player_count"]
        for key in (
            "ready_count",
            "memory_count",
            "snapshot_count",
            "npc_count",
            "active_timer_count",
        ):
            if key in keys:
                result[key] = int(row[key] or 0)
        if "progress_json" in keys:
            result["progress"] = normalize_progress(
                json_load(row["progress_json"], {})
            )
        if "recovery_json" in keys:
            result["recovery"] = json_load(row["recovery_json"], {})
        if "termination_type" in keys:
            result["archive"] = (
                {
                    "termination_type": row["termination_type"],
                    "reason": row["archive_reason"],
                    "final_snapshot_id": row["final_snapshot_id"],
                    "ended_by": row["ended_by"],
                    "ended_at": row["ended_at"],
                    "readonly": bool(row["readonly"]),
                }
                if row["termination_type"]
                else None
            )
            result["readonly"] = bool(row["readonly"])
        if "waiting_for" in keys:
            result["waiting_for"] = str(row["waiting_for"] or "")
        if "active_deadline_at" in keys:
            result["active_deadline_at"] = str(
                row["active_deadline_at"] or ""
            )
        progress = result.get("progress")
        if isinstance(progress, Mapping):
            total = int(progress.get("total_milestones") or 0)
            completed = int(progress.get("completed_milestones") or 0)
            result["progress_percent"] = (
                round(completed * 100 / total) if total > 0 else None
            )
        return result

    @staticmethod
    def _player(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "character_name": row["character_name"],
            "profile": json_load(row["profile_json"], {}),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "id": row["id"],
            "session_id": row["session_id"],
            "turn_no": row["turn_no"],
            "role": row["role"],
            "actor_id": row["actor_id"],
            "actor_name": row["actor_name"],
            "content": row["content"],
            "meta": json_load(row["meta_json"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _memory(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "scope": row["scope"],
            "scope_id": row["scope_id"],
            "kind": row["kind"],
            "content": row["content"],
            "importance": row["importance"],
            "salience": row["salience"],
            "tags": json_load(row["tags_json"], []),
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "visibility": (
                row["governance_visibility"]
                if "governance_visibility" in keys
                and row["governance_visibility"]
                else "public"
            ),
            "locked": bool(
                row["governance_locked"]
                if "governance_locked" in keys else 0
            ),
            "pinned": bool(
                row["governance_pinned"]
                if "governance_pinned" in keys else 0
            ),
            "invalidated": bool(
                row["governance_invalidated"]
                if "governance_invalidated" in keys else 0
            ),
            "supersedes_id": (
                row["governance_supersedes_id"]
                if "governance_supersedes_id" in keys else ""
            ),
            "conflict_status": (
                row["governance_conflict_status"]
                if "governance_conflict_status" in keys
                and row["governance_conflict_status"]
                else "clear"
            ),
            "governance_note": (
                row["governance_note"]
                if "governance_note" in keys else ""
            ),
        }

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        stored_world_state = json_load(row["world_state_json"], {})
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "kind": row["kind"],
            "turn_no": row["turn_no"],
            "session_revision": row["session_revision"],
            "world_id": row["world_id"],
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _session_character(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "name": row["name"],
            "aliases": json_load(row["aliases_json"], []),
            "role_type": row["role_type"],
            "public_profile": json_load(row["public_profile_json"], {}),
            "known_facts": json_load(row["known_facts_json"], []),
            "misconceptions": json_load(row["misconceptions_json"], []),
            "source": row["source"],
            "review_status": row["review_status"],
            "lifecycle_status": row["lifecycle_status"],
            "persistent": bool(row["persistent"]),
            "first_event_id": row["first_event_id"],
            "last_event_id": row["last_event_id"],
            "first_turn": row["first_turn"],
            "last_turn": row["last_turn"],
            "revision": row["revision"],
            "state": (
                json_load(row["state_json"], {})
                if "state_json" in keys
                else {}
            ),
            "state_revision": (
                int(row["state_revision"] or 0)
                if "state_revision" in keys
                else 0
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ledger_entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "kind": row["kind"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "visibility": row["visibility"],
            "source_event_id": row["source_event_id"],
            "completed_event_id": row["completed_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _scene_clock(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "title": row["title"],
            "segments": row["segments"],
            "current_value": row["current_value"],
            "visibility": row["visibility"],
            "trigger_text": row["trigger_text"],
            "status": row["status"],
            "triggered_event_id": row["triggered_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _assert_session_writable(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT s.state, sa.readonly
            FROM sessions s
            LEFT JOIN session_archives sa ON sa.session_id = s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            raise DatabaseNotFoundError("会话不存在")
        if row["state"] == SESSION_FINISHED or bool(row["readonly"]):
            raise InvalidTransitionError(
                "该副本已永久归档并处于只读状态"
            )
