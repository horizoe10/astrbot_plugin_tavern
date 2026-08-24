from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_CLOSED,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import (
    DatabaseConflictError,
    InvalidTransitionError,
    TavernDatabase,
)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.session = await self.database.ensure_session(
            "qq",
            "group-100",
            "qq:group-100",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _start(self) -> dict:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        return self.session

    async def _commit(
        self,
        session: dict,
        *,
        fact: str,
        user_id: str = "user-1",
        display_name: str = "旅客",
    ) -> dict:
        player = await self.database.ensure_player(
            session["id"],
            user_id,
            display_name,
        )
        state = dict(session["world_state"])
        state["scene_summary"] = fact
        facts = list(state.get("facts", []))
        facts.append(fact)
        state["facts"] = facts
        return await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=player["id"],
            player_user_id=player["user_id"],
            player_name=player["display_name"],
            player_input=f"尝试：{fact}",
            narrative=f"结果：{fact}",
            world_state=state,
            memories=[
                {
                    "scope": "world",
                    "scope_id": "",
                    "kind": "fact",
                    "content": fact,
                    "importance": 4,
                    "tags": ["测试"],
                }
            ],
            check_payload=None,
            model_payload={"mode": "resolve"},
            director_note="测试裁定",
            auto_snapshot_interval=5,
            store_model_payload=False,
        )

    async def test_group_can_keep_multiple_isolated_instances(self) -> None:
        first = await self._start()
        first = await self._commit(
            first,
            fact="一号副本已经取得铜钥匙",
        )
        second = await self.database.ensure_session(
            "qq",
            "group-100",
            "qq:group-100",
            DEFAULT_WORLD_SLUG,
            "admin-1",
            "border-tavern-second",
            "边境酒馆二号副本",
        )
        self.assertFalse(second["selected"])
        self.assertEqual(first["world_id"], second["world_id"])
        self.assertEqual(second["turn_no"], 0)
        self.assertNotIn(
            "一号副本已经取得铜钥匙",
            second["world_state"].get("facts", []),
        )
        self.assertEqual(
            len(
                await self.database.list_group_sessions(
                    "qq",
                    "group-100",
                )
            ),
            2,
        )

        second = await self.database.transition_session(
            second["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        first = await self.database.get_session(first["id"])
        self.assertEqual(first["state"], SESSION_PAUSED)
        self.assertIn(
            "一号副本已经取得铜钥匙",
            first["world_state"].get("facts", []),
        )
        self.assertFalse(first["selected"])
        self.assertTrue(second["selected"])
        selected = await self.database.get_session_by_group(
            "qq",
            "group-100",
        )
        self.assertEqual(selected["id"], second["id"])
        self.assertEqual(
            selected["instance_slug"],
            "border-tavern-second",
        )

    @unittest.skip(
        "B1 是刻意干净基线（database.py 注释：永不读写旧版 "
        "tavern.sqlite3/catalog.sqlite3；ARCHITECTURE.md：Schema 1—7 不受支持），"
        "该用例测试的是已被移除的 v1 迁移路径，属于过时用例。"
    )
    async def test_v1_session_schema_migrates_to_named_instance(self) -> None:
        legacy_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(legacy_dir.name) / "tavern.sqlite3"
            now = "2026-01-01T00:00:00+00:00"
            # closing：sqlite3 连接上下文管理器不负责关闭，Windows 下
            # 不关闭会让临时目录清理因文件占用而失败。
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE worlds (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        rules_json TEXT NOT NULL DEFAULT '{}',
                        opening_scene TEXT NOT NULL DEFAULT '',
                        initial_state_json TEXT NOT NULL DEFAULT '{}',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        state TEXT NOT NULL DEFAULT 'closed',
                        turn_no INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        world_state_json TEXT NOT NULL DEFAULT '{}',
                        history_floor_seq INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id)
                    );
                    CREATE TABLE players (
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
                    """
                )
                connection.execute(
                    """
                    INSERT INTO worlds(
                        id, slug, name, system_prompt, initial_state_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "world_legacy",
                        "legacy-world",
                        "旧版世界",
                        "保持连续。",
                        json.dumps({"location": "旧址"}),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin, world_id,
                        state, world_state_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        "session_legacy",
                        "qq",
                        "legacy-group",
                        "qq:legacy-group",
                        "world_legacy",
                        json.dumps({"location": "旧址"}),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "player_legacy",
                        "session_legacy",
                        "legacy-user",
                        "旧版玩家",
                        now,
                        now,
                    ),
                )

            migrated = TavernDatabase(Path(legacy_dir.name))
            session = await migrated.get_session_by_group(
                "qq",
                "legacy-group",
            )
            self.assertEqual(session["instance_slug"], "legacy-world")
            self.assertEqual(session["instance_name"], "旧版世界")
            self.assertTrue(session["selected"])
            self.assertEqual(session["state"], SESSION_PAUSED)
            players = await migrated.list_players(session["id"])
            self.assertEqual(
                [(item["user_id"], item["display_name"]) for item in players],
                [("legacy-user", "旧版玩家")],
            )
            roster = await migrated.list_roster(session["id"])
            self.assertEqual(len(roster), 1)
            self.assertEqual(roster[0]["card_status"], "approved")
            self.assertEqual(roster[0]["group_user_id"], "legacy-user")
            self.assertIsNotNone(migrated.migration_backup_path)
            self.assertTrue(
                (Path(legacy_dir.name) / "catalog.sqlite3").exists()
            )
            self.assertFalse(path.exists())
            retained = list(
                (
                    Path(legacy_dir.name) / "migration_backups"
                ).glob("backup_legacy_tavern_*.sqlite3")
            )
            self.assertEqual(len(retained), 1)
            self.assertRegex(
                retained[0].name,
                r"^backup_legacy_tavern_\d{14}(?:_\d{2})?\.sqlite3$",
            )
            storage = await migrated.get_storage_info(session["id"])
            story_dir = Path(legacy_dir.name) / storage["relative_path"]
            self.assertTrue((story_dir / "instance.sqlite3").exists())
            self.assertTrue(
                list((story_dir / "backups").glob("backup_*.zip"))
            )
        finally:
            legacy_dir.cleanup()

    async def test_multiplayer_turn_order_advances_atomically(self) -> None:
        session = await self._start()
        first = await self.database.join_turn_order(
            session["id"],
            "user-a",
            "甲",
            "user-a",
        )
        second = await self.database.join_turn_order(
            session["id"],
            "user-b",
            "乙",
            "user-b",
        )
        self.assertTrue(first["joined"])
        self.assertTrue(second["joined"])
        self.assertEqual(second["turn"]["current_user_id"], "user-a")

        current = await self.database.get_session(session["id"])
        current = await self._commit(
            current,
            fact="甲先检查柜台",
            user_id="user-a",
            display_name="甲",
        )
        turn = await self.database.get_turn_status(session["id"])
        self.assertEqual(turn["current_user_id"], "user-b")
        self.assertEqual(turn["round_no"], 1)

        with self.assertRaisesRegex(InvalidTransitionError, "当前轮到"):
            await self._commit(
                current,
                fact="甲试图连续行动",
                user_id="user-a",
                display_name="甲",
            )
        self.assertEqual(
            (await self.database.get_session(session["id"]))["turn_no"],
            1,
        )

        current = await self._commit(
            current,
            fact="乙查看门外",
            user_id="user-b",
            display_name="乙",
        )
        turn = await self.database.get_turn_status(session["id"])
        self.assertEqual(turn["current_user_id"], "user-a")
        self.assertEqual(turn["round_no"], 2)
        self.assertEqual(current["turn_no"], 2)

    async def test_leaving_or_skipping_current_player_keeps_queue_live(
        self,
    ) -> None:
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            session["id"], "user-b", "乙", "user-b"
        )
        skipped = await self.database.skip_turn(
            session["id"],
            "user-a",
            "user-a",
        )
        self.assertEqual(skipped["current_user_id"], "user-b")
        left = await self.database.leave_turn_order(
            session["id"],
            "user-b",
            "user-b",
        )
        self.assertTrue(left["removed"])
        self.assertEqual(left["turn"]["current_user_id"], "user-a")

    async def test_snapshot_restores_multiplayer_turn_token(self) -> None:
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            session["id"], "user-b", "乙", "user-b"
        )
        await self.database.create_snapshot(
            session["id"],
            "轮次起点",
            "admin-1",
        )
        await self.database.skip_turn(
            session["id"],
            "user-a",
            "user-a",
        )
        self.assertEqual(
            (await self.database.get_turn_status(session["id"]))[
                "current_user_id"
            ],
            "user-b",
        )
        await self.database.restore_snapshot(
            session["id"],
            "轮次起点",
            "admin-1",
        )
        restored = await self.database.get_turn_status(session["id"])
        self.assertEqual(restored["current_user_id"], "user-a")
        self.assertEqual(
            [item["user_id"] for item in restored["order"]],
            ["user-a", "user-b"],
        )

    async def test_state_machine_and_closed_world_switch(self) -> None:
        session = await self._start()
        world = await self.database.save_world(
            {
                "slug": "another-world",
                "name": "另一个世界",
                "description": "测试",
                # 0.11.0 起校验要求显式声明世界协议版本，裸世界 v0 会被拒绝。
                "world_schema_version": 2,
                "system_prompt": "严格遵守因果。",
                "opening_scene": "新场景。",
                "rules": {"resolution": "d20"},
                "initial_state": {
                    "location": "新地点",
                    "facts": [],
                    "inventory": {},
                    "relationships": {},
                },
            },
            "admin-1",
        )
        with self.assertRaises(InvalidTransitionError):
            await self.database.transition_session(
                session["id"],
                SESSION_RUNNING,
                "admin-1",
                world["slug"],
            )
        session = await self.database.transition_session(
            session["id"],
            SESSION_CLOSED,
            "admin-1",
        )
        session = await self.database.transition_session(
            session["id"],
            SESSION_RUNNING,
            "admin-1",
            world["slug"],
        )
        self.assertEqual(session["world_id"], world["id"])
        self.assertEqual(session["turn_no"], 0)
        self.assertEqual(session["world_state"]["location"], "新地点")

    async def test_archived_world_stays_archived_until_explicit_restore(self) -> None:
        world = await self.database.get_world(DEFAULT_WORLD_SLUG)
        world = await self.database.archive_world(world["id"], "admin-1")
        self.assertTrue(world["archived"])
        # 用可移植载荷重存（保留 world_schema_version / minimum_plugin_version /
        # protocol / required_features），避免 v5 契约校验误伤。
        from tavern.world_import import world_import_payload

        payload = world_import_payload(world)
        payload.update(
            {
                "id": world["id"],
                "revision": world["revision"],
                "name": "已归档但可编辑",
            }
        )
        world = await self.database.save_world(payload, "admin-1")
        self.assertTrue(world["archived"])
        world = await self.database.restore_world(world["id"], "admin-1")
        self.assertFalse(world["archived"])

    async def test_single_turn_rollback_creates_new_history_branch(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="发现铜钥匙")
        session = await self._commit(session, fact="打开北侧门")
        self.assertEqual(session["turn_no"], 2)

        snapshots = await self.database.list_snapshots(session["id"])
        self.assertTrue(any(item["kind"] == "undo" for item in snapshots))

        restored = await self.database.restore_latest_auto(
            session["id"],
            "admin-1",
        )
        self.assertEqual(restored["state"], SESSION_PAUSED)
        self.assertEqual(restored["turn_no"], 1)
        self.assertNotIn(
            "打开北侧门",
            restored["world_state"].get("facts", []),
        )

        visible = await self.database.recent_events(session["id"], 100)
        self.assertEqual(
            [item["role"] for item in visible],
            ["player", "narrator", "system"],
        )
        self.assertTrue(
            any("发现铜钥匙" in item["content"] for item in visible)
        )
        self.assertFalse(
            any("打开北侧门" in item["content"] for item in visible)
        )
        restored = await self.database.transition_session(
            restored["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        restored = await self._commit(restored, fact="改走南侧门")
        visible = await self.database.recent_events(session["id"], 100)
        contents = [item["content"] for item in visible]
        self.assertTrue(any("改走南侧门" in item for item in contents))
        self.assertFalse(any("打开北侧门" in item for item in contents))

    async def test_optimistic_revision_rejects_double_commit(self) -> None:
        original = await self._start()
        await self._commit(original, fact="第一条结果")
        with self.assertRaises(DatabaseConflictError):
            await self._commit(original, fact="过期请求")
        current = await self.database.get_session(original["id"])
        self.assertEqual(current["turn_no"], 1)

    async def test_cancelled_card_can_rejoin_but_retired_still_cannot(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        first = await self.database.reserve_participant(
            self.session["id"], "rejoin-user", "重入玩家"
        )
        origin = "qq:FriendMessage:rejoin-user"
        await self.database.bind_card_code(
            first["binding_code"], "private-rejoin-user", origin
        )
        await self.database.fill_card_draft(origin, "旧草稿内容")
        await self.database.cancel_card_draft(origin)
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name, archived,
                    deleted, current_version, created_at, updated_at
                ) VALUES ('card-residual', 'rejoin-user', ?, '旧角色卡',
                          0, 0, 1, 'old', 'old')
                """,
                (self.session["world_id"],),
            )
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES ('version-residual', 'card-residual', 1, 1,
                          '{}', '{}', 'approved', '', '', 'old')
                """
            )
            connection.execute(
                """
                UPDATE participants SET
                    character_card_id = 'card-residual',
                    character_version_id = 'version-residual',
                    character_name = '旧角色', character_code = 'OLD-CODE',
                    aliases_json = '["旧别名"]', action_locked = 1,
                    joined_round = 7
                WHERE id = ?
                """,
                (first["id"],),
            )
            old_timer_ids = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM timer_instances WHERE participant_id = ?",
                    (first["id"],),
                ).fetchall()
            }

        second = await self.database.reserve_participant(
            self.session["id"], "rejoin-user", "重入玩家"
        )
        self.assertTrue(second["joined"])
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["binding_code"], first["binding_code"])
        self.assertEqual(second["participation_status"], "reserved")
        self.assertEqual(second["private_origin"], "")
        with self.database._connect() as connection:
            participant = connection.execute(
                "SELECT * FROM participants WHERE id = ?", (first["id"],)
            ).fetchone()
            draft = connection.execute(
                "SELECT * FROM character_card_drafts WHERE participant_id = ?",
                (first["id"],),
            ).fetchone()
            codes = connection.execute(
                """
                SELECT code, status, private_user_id, private_origin, used_at
                FROM card_binding_codes WHERE participant_id = ?
                ORDER BY created_at, id
                """,
                (first["id"],),
            ).fetchall()
            timers = connection.execute(
                """
                SELECT id, timer_type, status FROM timer_instances
                WHERE participant_id = ? ORDER BY created_at, id
                """,
                (first["id"],),
            ).fetchall()
        self.assertEqual(
            (
                participant["private_user_id"],
                participant["private_origin"],
                participant["character_card_id"],
                participant["character_version_id"],
                participant["character_name"],
                participant["character_code"],
                participant["aliases_json"],
                participant["card_status"],
                participant["ready"],
                participant["action_locked"],
                participant["joined_round"],
                participant["exit_reason"],
            ),
            ("", "", None, None, "", "", "[]", "uncreated", 0, 0, 1, ""),
        )
        self.assertEqual(
            (draft["status"], draft["fields_json"], draft["current_step"]),
            ("active", "{}", 0),
        )
        self.assertEqual(len(codes), 2)
        code_statuses = {row["code"]: row["status"] for row in codes}
        self.assertEqual(code_statuses[first["binding_code"]], "expired")
        self.assertEqual(code_statuses[second["binding_code"]], "active")
        new_code_row = next(
            row for row in codes if row["code"] == second["binding_code"]
        )
        self.assertEqual(
            (
                new_code_row["private_user_id"],
                new_code_row["private_origin"],
                new_code_row["used_at"],
            ),
            ("", "", ""),
        )
        for timer in timers:
            if timer["id"] in old_timer_ids:
                self.assertIn(timer["status"], {"cancelled", "completed"})
        live_timers = [
            row for row in timers if row["status"] in {"active", "paused"}
        ]
        self.assertEqual(
            sorted(row["timer_type"] for row in live_timers),
            ["card_code", "card_completion"],
        )
        with self.assertRaisesRegex(ValueError, "建卡码不存在或已使用"):
            await self.database.bind_card_code(
                first["binding_code"], "old-private", "qq:FriendMessage:old"
            )

        with self.database._connect() as connection:
            connection.execute(
                "UPDATE participants SET participation_status = 'retired' WHERE id = ?",
                (first["id"],),
            )
            retired_before = dict(
                connection.execute(
                    "SELECT * FROM participants WHERE id = ?", (first["id"],)
                ).fetchone()
            )
        with self.assertRaisesRegex(ValueError, "正式退场.*申请返场"):
            await self.database.reserve_participant(
                self.session["id"], "rejoin-user", "重入玩家"
            )
        with self.database._connect() as connection:
            retired_after = dict(
                connection.execute(
                    "SELECT * FROM participants WHERE id = ?", (first["id"],)
                ).fetchone()
            )
        self.assertEqual(retired_after, retired_before)

    async def test_other_archived_participants_are_not_reactivated(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        cases = (
            ("other-archive", "removed_by_admin", "uncreated"),
            ("cancelled-draft", "cancelled_card", "draft"),
        )
        for user_id, exit_reason, card_status in cases:
            participant = await self.database.reserve_participant(
                self.session["id"], user_id, user_id
            )
            with self.database._connect() as connection:
                connection.execute(
                    """
                    UPDATE participants SET participation_status = 'archived',
                        exit_reason = ?, card_status = ? WHERE id = ?
                    """,
                    (exit_reason, card_status, participant["id"]),
                )
                before = dict(
                    connection.execute(
                        "SELECT * FROM participants WHERE id = ?",
                        (participant["id"],),
                    ).fetchone()
                )
            with self.assertRaisesRegex(ValueError, "已归档.*主持人") as caught:
                await self.database.reserve_participant(
                    self.session["id"], user_id, f"{user_id}-new"
                )
            self.assertNotIn("申请返场", str(caught.exception))
            with self.database._connect() as connection:
                after = dict(
                    connection.execute(
                        "SELECT * FROM participants WHERE id = ?",
                        (participant["id"],),
                    ).fetchone()
                )
            self.assertEqual(after, before)

    async def test_cancelled_card_rejoin_still_obeys_capacity(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        first = await self.database.reserve_participant(
            self.session["id"], "capacity-user", "候补玩家"
        )
        origin = "qq:FriendMessage:capacity-user"
        await self.database.bind_card_code(first["binding_code"], "private", origin)
        await self.database.cancel_card_draft(origin)
        for index in range(4):
            await self.database.reserve_participant(
                self.session["id"], f"occupant-{index}", f"占位玩家{index}"
            )

        with self.database._connect() as connection:
            participant_before = dict(
                connection.execute(
                    "SELECT * FROM participants WHERE id = ?", (first["id"],)
                ).fetchone()
            )
            player_before = dict(
                connection.execute(
                    "SELECT * FROM players WHERE id = ?", (first["player_id"],)
                ).fetchone()
            )
            draft_before = dict(
                connection.execute(
                    "SELECT * FROM character_card_drafts WHERE participant_id = ?",
                    (first["id"],),
                ).fetchone()
            )
            codes_before = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM card_binding_codes WHERE participant_id = ?
                    ORDER BY id
                    """,
                    (first["id"],),
                ).fetchall()
            ]
            timers_before = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM timer_instances WHERE participant_id = ?
                    ORDER BY id
                    """,
                    (first["id"],),
                ).fetchall()
            ]

        with self.assertRaisesRegex(ValueError, "已满"):
            await self.database.reserve_participant(
                self.session["id"], "capacity-user", "不应写入的新名字"
            )
        with self.database._connect() as connection:
            participant_after = dict(
                connection.execute(
                    "SELECT * FROM participants WHERE id = ?", (first["id"],)
                ).fetchone()
            )
            player_after = dict(
                connection.execute(
                    "SELECT * FROM players WHERE id = ?", (first["player_id"],)
                ).fetchone()
            )
            draft_after = dict(
                connection.execute(
                    "SELECT * FROM character_card_drafts WHERE participant_id = ?",
                    (first["id"],),
                ).fetchone()
            )
            codes_after = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM card_binding_codes WHERE participant_id = ?
                    ORDER BY id
                    """,
                    (first["id"],),
                ).fetchall()
            ]
            timers_after = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM timer_instances WHERE participant_id = ?
                    ORDER BY id
                    """,
                    (first["id"],),
                ).fetchall()
            ]
        self.assertEqual(participant_after, participant_before)
        self.assertEqual(player_after, player_before)
        self.assertEqual(draft_after, draft_before)
        self.assertEqual(codes_after, codes_before)
        self.assertEqual(timers_after, timers_before)

    async def test_merge_backup_is_insert_only_and_preserves_live_data(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="保留这条时间线")
        bundle = await self.database.export_bundle()
        bundle["data"]["worlds"][0]["name"] = "合并后的世界名"

        counts = await self.database.import_bundle(
            bundle,
            "merge",
            "web:tester",
        )
        self.assertEqual(counts["audit_logs"], 0)
        current = await self.database.get_session(session["id"])
        self.assertNotEqual(current["world_name"], "合并后的世界名")
        self.assertEqual(len(await self.database.list_players(session["id"])), 1)
        self.assertEqual(len(await self.database.recent_events(session["id"], 20)), 2)

    async def test_merge_rejects_same_identity_with_different_id(self) -> None:
        bundle = await self.database.export_bundle()
        conflicting = copy.deepcopy(bundle)
        conflicting["data"]["worlds"][0]["id"] = "world_foreign"
        with self.assertRaisesRegex(ValueError, "取消合并"):
            await self.database.import_bundle(
                conflicting,
                "merge",
                "web:tester",
            )

    async def test_newer_backup_schema_is_rejected(self) -> None:
        bundle = await self.database.export_bundle()
        bundle["schema_version"] = 999
        with self.assertRaisesRegex(ValueError, "升级插件"):
            await self.database.import_bundle(
                bundle,
                "replace",
                "web:tester",
            )

    async def test_replace_backup_round_trip(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="可恢复事实")
        bundle = await self.database.export_bundle()

        other_dir = tempfile.TemporaryDirectory()
        try:
            other = TavernDatabase(Path(other_dir.name))
            counts = await other.import_bundle(
                bundle,
                "replace",
                "web:tester",
            )
            self.assertGreaterEqual(counts["worlds"], 1)
            restored = await other.get_session(session["id"])
            self.assertEqual(restored["turn_no"], 1)
            self.assertIn(
                "可恢复事实",
                restored["world_state"].get("facts", []),
            )
        finally:
            other_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
