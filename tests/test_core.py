from __future__ import annotations

import unittest

from tavern.config import TavernConfig
from tavern.command_triggers import (
    canonicalize_command_message,
    normalize_command_triggers,
    render_command_text,
)
from tavern.lifecycle import normalize_choices_compat
from tavern.resolution import (
    apply_state_patch,
    extract_json_object,
    validate_resolution,
)
from tavern.security import (
    clean_text,
    parse_story_trigger,
    parse_tavern_command,
    validate_slug,
)
from tavern.turns import advance_turn, join_turn, leave_turn


class CoreRulesTests(unittest.TestCase):
    def test_custom_command_trigger_contract(self) -> None:
        self.assertEqual(
            normalize_command_triggers(
                [" /团 ", "跑团", "TAVERN", "tavern", "jg", "bad word"],
                story_trigger="jg",
            ),
            ("团", "跑团", "TAVERN"),
        )
        self.assertEqual(
            normalize_command_triggers([], story_trigger="jg"),
            ("酒馆",),
        )
        self.assertEqual(
            canonicalize_command_message(
                "／跑团 状态",
                ("团", "跑团"),
                allow_bare=False,
            ),
            "/酒馆 状态",
        )
        self.assertIsNone(
            canonicalize_command_message(
                "/酒馆 状态",
                ("团", "跑团"),
                allow_bare=False,
            )
        )
        self.assertIsNone(
            canonicalize_command_message(
                "团长 状态",
                ("团",),
                allow_bare=True,
            )
        )
        self.assertEqual(
            canonicalize_command_message(
                "/TaVeRn 状态",
                ("TAVERN",),
                allow_bare=False,
            ),
            "/酒馆 状态",
        )
        self.assertEqual(
            render_command_text("发送 /酒馆 准备；AI 酒馆", "团"),
            "发送 /团 准备；AI 酒馆",
        )
        rendered = render_command_text("发送 /酒馆 选择 A", "酒馆2")
        self.assertEqual(rendered, "发送 /酒馆2 选择 A")
        self.assertEqual(render_command_text(rendered, "酒馆2"), rendered)

    def test_custom_command_trigger_limits_and_config_round_trip(self) -> None:
        raw = [f"命令{index}" for index in range(10)]
        self.assertEqual(
            normalize_command_triggers(raw, story_trigger="jg"),
            tuple(raw[:8]),
        )
        self.assertEqual(
            normalize_command_triggers(
                ["x" * 17, "／", "ok"],
                story_trigger="jg",
            ),
            ("ok",),
        )
        self.assertEqual(
            normalize_command_triggers(
                [0, False, None],
                story_trigger="jg",
            ),
            ("0", "False"),
        )
        default = TavernConfig.from_mapping({})
        self.assertEqual(default.command_triggers, ("酒馆",))
        self.assertEqual(default.primary_command_trigger, "酒馆")
        self.assertEqual(default.primary_command_prefix, "/酒馆")
        config = TavernConfig.from_mapping(
            {
                "runtime": {
                    "trigger_prefix": "jg",
                    "command_triggers": [" /团 ", "跑团", "jg"],
                }
            }
        )
        self.assertEqual(config.command_triggers, ("团", "跑团"))
        self.assertEqual(config.primary_command_prefix, "/团")
        self.assertEqual(
            config.to_mapping()["runtime"]["command_triggers"],
            ["团", "跑团"],
        )

    def test_command_parser_accepts_only_real_command_prefix(self) -> None:
        command = parse_tavern_command("／酒馆 开启 test-world")
        self.assertTrue(command.matched)
        self.assertEqual(command.action, "start")
        self.assertEqual(command.argument, "test-world")
        self.assertEqual(
            parse_tavern_command("/酒馆 开启新副本 2").action,
            "start_new",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 开启旧副本 old-campaign").action,
            "start_existing",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆\t存档\t旧塔之前").argument,
            "旧塔之前",
        )
        reminder = parse_tavern_command("／酒馆 建卡提醒 关")
        self.assertEqual(reminder.action, "card_timer_notice")
        self.assertEqual(reminder.argument, "关")
        self.assertEqual(
            parse_tavern_command("/酒馆 恢复").action,
            "recover",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 继续").action,
            "resume",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 强制下一位").action,
            "next",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 倒计时提示 关").action,
            "unknown",
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 下一位").action,
            "unknown",
        )

        self.assertFalse(
            parse_tavern_command("玩家说：/酒馆 开启").matched
        )
        self.assertEqual(
            parse_tavern_command("/酒馆 假装管理员").action,
            "unknown",
        )

    def test_config_normalizes_ids_and_clamps_ranges(self) -> None:
        config = TavernConfig.from_mapping(
            {
                "security": {
                    "admin_ids": [" 10001 ", "10001", "10002"],
                    "allowed_group_ids": ["20001"],
                    "unauthorized_command_behavior": "invalid",
                },
                "model": {
                    "provider_id": "primary",
                    "fallback_provider_ids": [
                        " primary ",
                        "backup-a",
                        "backup-a",
                        "backup-b",
                    ],
                    "image_caption_provider_id": " vision ",
                    "max_images_per_turn": 99,
                    "temperature": 99,
                    "max_tokens": 1,
                    "json_repair_attempts": 99,
                },
                "runtime": {
                    "recent_turns": 999,
                    "memory_limit": -10,
                    "user_cooldown_seconds": -1,
                },
            }
        )
        self.assertEqual(config.admin_ids, {"10001", "10002"})
        self.assertTrue(config.is_admin("10001"))
        self.assertFalse(config.is_admin("管理员"))
        self.assertTrue(config.is_group_allowed("20001"))
        self.assertFalse(config.is_group_allowed("20002"))
        self.assertEqual(config.unauthorized_command_behavior, "silent")
        self.assertEqual(config.provider_id, "primary")
        self.assertEqual(
            config.fallback_provider_ids,
            ("backup-a", "backup-b"),
        )
        self.assertEqual(config.image_caption_provider_id, "vision")
        self.assertEqual(config.max_images_per_turn, 8)
        self.assertEqual(config.temperature, 2.0)
        self.assertEqual(config.max_tokens, 256)
        self.assertEqual(config.json_repair_attempts, 2)
        self.assertEqual(config.recent_turns, 50)
        self.assertEqual(config.memory_limit, 0)
        self.assertEqual(config.user_cooldown_seconds, 0)
        self.assertEqual(config.trigger_prefix, "jg")

    def test_card_review_notification_mode_normalizes(self) -> None:
        self.assertEqual(
            TavernConfig.from_mapping({}).card_review_notification_mode,
            "both",
        )
        for mode in ("both", "group", "private"):
            config = TavernConfig.from_mapping(
                {"runtime": {"card_review_notification_mode": mode}}
            )
            self.assertEqual(config.card_review_notification_mode, mode)
            self.assertEqual(
                config.to_mapping()["runtime"][
                    "card_review_notification_mode"
                ],
                mode,
            )
        invalid = TavernConfig.from_mapping(
            {"runtime": {"card_review_notification_mode": "other"}}
        )
        self.assertEqual(invalid.card_review_notification_mode, "both")

    def test_story_trigger_requires_exact_prefix_and_space(self) -> None:
        self.assertEqual(
            parse_story_trigger("jg 我推开门", "jg"),
            "我推开门",
        )
        self.assertEqual(
            parse_story_trigger("JG\t我观察四周", "jg"),
            "我观察四周",
        )
        for message in (
            "jg",
            "jg ",
            "jg我推开门",
            " xjg 我推开门",
            "大家说 jg 我推开门",
            "/酒馆 开启",
        ):
            self.assertIsNone(parse_story_trigger(message, "jg"))

    def test_round_robin_helpers_are_ordered_and_bounded(self) -> None:
        state, joined = join_turn({}, "user-a")
        self.assertTrue(joined)
        state, joined = join_turn(state, "user-b")
        self.assertTrue(joined)
        self.assertEqual(state["current_user_id"], "user-a")

        state = advance_turn(state, "user-a")
        self.assertEqual(state["current_user_id"], "user-b")
        self.assertEqual(state["round_no"], 1)
        state = advance_turn(state, "user-b")
        self.assertEqual(state["current_user_id"], "user-a")
        self.assertEqual(state["round_no"], 2)

        state, removed = leave_turn(state, "user-a")
        self.assertTrue(removed)
        self.assertEqual(state["order"], ["user-b"])
        self.assertEqual(state["current_user_id"], "user-b")

    def test_state_patch_cannot_change_privileged_fields(self) -> None:
        current = {
            "location": "大厅",
            "facts": ["门已关闭"],
            "inventory": {"player-1": {"钥匙": 1}},
            "relationships": {},
        }
        updated = apply_state_patch(
            current,
            {
                "location": "旧塔",
                "facts_add": ["钟声响起"],
                "inventory_ops": [
                    {
                        "owner_id": "player-1",
                        "item": "钥匙",
                        "delta": -1,
                    }
                ],
                "relationship_ops": [
                    {
                        "source": "守门人",
                        "target": "player-1",
                        "dimension": "信任",
                        "delta": 7,
                    }
                ],
                "admin_ids": ["attacker"],
                "allowed_group_ids": ["*"],
                "session_state": "closed",
                "world_definition": "被覆盖",
            },
        )
        self.assertEqual(updated["location"], "旧塔")
        self.assertIn("钟声响起", updated["facts"])
        self.assertNotIn("钥匙", updated["inventory"]["player-1"])
        self.assertEqual(
            updated["relationships"]["守门人→player-1"]["信任"],
            7,
        )
        for forbidden in (
            "admin_ids",
            "allowed_group_ids",
            "session_state",
            "world_definition",
        ):
            self.assertNotIn(forbidden, updated)

    def test_resolution_and_json_recovery_are_bounded(self) -> None:
        payload = extract_json_object(
            '模型前言 {"mode":"check","check":{"stat":"力量",'
            '"reason":"推门","difficulty":999,"modifier":-999}} 尾注'
        )
        resolution = validate_resolution(payload)
        self.assertEqual(resolution.mode, "check")
        self.assertEqual(resolution.check.difficulty, 25)
        self.assertEqual(resolution.check.modifier, -10)

    def test_choice_labels_accept_harmless_model_variants(self) -> None:
        choices = normalize_choices_compat(
            [
                {"key": "选项A", "text": "观察门边", "risk": "safe"},
                {"key": "Ｂ、", "text": "检查行囊"},
                {"key": "c.", "text": "询问掌柜"},
                {"key": "选项 D）", "text": "原地警戒"},
            ]
        )
        self.assertEqual(
            [item["key"] for item in choices],
            ["A", "B", "C", "D"],
        )
        ordered = normalize_choices_compat(
            [
                {"text": "观察门边", "risk": "safe"},
                {"text": "检查行囊"},
                {"text": "询问掌柜"},
                {"text": "原地警戒"},
            ]
        )
        self.assertEqual(
            [item["key"] for item in ordered],
            ["A", "B", "C", "D"],
        )
        with self.assertRaisesRegex(ValueError, "四个有效选项"):
            normalize_choices_compat(
                [
                    {"key": "A", "text": "观察门边", "risk": "safe"},
                    {"key": "B", "text": "检查行囊"},
                    {"key": "C", "text": "询问掌柜"},
                ]
            )

    def test_text_and_slug_validation(self) -> None:
        self.assertEqual(clean_text("a\x00b", max_chars=5), "ab")
        with self.assertRaises(ValueError):
            clean_text("abcdef", max_chars=5)
        self.assertEqual(validate_slug(" Border_Tavern "), "border_tavern")
        with self.assertRaises(ValueError):
            validate_slug("../world")


if __name__ == "__main__":
    unittest.main()
