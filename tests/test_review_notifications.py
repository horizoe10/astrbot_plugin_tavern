from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tavern.config import TavernConfig
from tavern.platform_delivery import DeliveryResult
from tavern.review_notifications import notify_card_review


class ReviewNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = object()
        self.database = MagicMock()
        self.database.get_session = AsyncMock(
            return_value={"unified_origin": "qq:GroupMessage:group-1"}
        )
        self.database.get_instance_config = AsyncMock(
            return_value={"world_snapshot": {}}
        )
        self.database.queue_delivery = AsyncMock(
            return_value={"id": "delivery-1"}
        )
        self.broker = MagicMock()
        self.broker.publish = AsyncMock()
        self.logger = MagicMock()
        self.participant = {
            "id": "participant-1",
            "session_id": "session-1",
            "private_origin": "qq:FriendMessage:user-1",
            "display_name": "白鸦",
            "character_name": "梅林",
            "character_version_id": "version-1",
        }

    async def test_selected_modes_deliver_expected_channels(self) -> None:
        for mode, expected in {
            "both": {"private", "group"},
            "group": {"group"},
            "private": {"private"},
        }.items():
            with self.subTest(mode=mode), patch(
                "tavern.review_notifications.deliver_text",
                new=AsyncMock(return_value=DeliveryResult(True, "sent")),
            ) as deliver:
                result = await notify_card_review(
                    context=self.context,
                    database=self.database,
                    broker=self.broker,
                    config=TavernConfig.from_mapping(
                        {
                            "runtime": {
                                "card_review_notification_mode": mode
                            }
                        }
                    ),
                    participant=self.participant,
                    approved=True,
                    note="",
                    logger=self.logger,
                )
            self.assertEqual(set(result), expected)
            self.assertEqual(deliver.await_count, len(expected))
            sent = {
                call.args[1]: call.args[2]
                for call in deliver.await_args_list
            }
            if "group" in expected:
                group_text = sent["qq:GroupMessage:group-1"]
                self.assertTrue(group_text.startswith("@白鸦\n"))
                self.assertIn("/酒馆 准备", group_text)
            if "private" in expected:
                self.assertIn(
                    "/酒馆 准备",
                    sent["qq:FriendMessage:user-1"],
                )

    async def test_rejected_text_includes_note_without_ready_command(self) -> None:
        with patch(
            "tavern.review_notifications.deliver_text",
            new=AsyncMock(return_value=DeliveryResult(True, "sent")),
        ) as deliver:
            await notify_card_review(
                context=self.context,
                database=self.database,
                broker=self.broker,
                config=TavernConfig.from_mapping(
                    {
                        "runtime": {
                            "card_review_notification_mode": "private"
                        }
                    }
                ),
                participant=self.participant,
                approved=False,
                note="属性点超出上限",
                logger=self.logger,
            )
        text = deliver.await_args.args[2]
        self.assertIn("已驳回", text)
        self.assertIn("属性点超出上限", text)
        self.assertNotIn("/酒馆 准备", text)

    async def test_failed_delivery_queues_with_channel_dedupe_key(self) -> None:
        with patch(
            "tavern.review_notifications.deliver_text",
            new=AsyncMock(
                return_value=DeliveryResult(False, "rejected", "未确认发送")
            ),
        ):
            result = await notify_card_review(
                context=self.context,
                database=self.database,
                broker=self.broker,
                config=TavernConfig.from_mapping(
                    {
                        "runtime": {
                            "card_review_notification_mode": "private"
                        }
                    }
                ),
                participant=self.participant,
                approved=True,
                note="",
                logger=self.logger,
            )
        self.assertEqual(result, {"private": "queued"})
        self.database.queue_delivery.assert_awaited_once()
        kwargs = self.database.queue_delivery.await_args.kwargs
        self.assertEqual(kwargs["kind"], "card.review.private")
        self.assertEqual(
            kwargs["dedupe_key"],
            "card-review:participant-1:version-1:approved:private",
        )
        self.broker.publish.assert_awaited_once_with(
            {
                "type": "delivery",
                "action": "queued",
                "session_id": "session-1",
            }
        )

    async def test_missing_selected_origin_is_skipped(self) -> None:
        participant = {**self.participant, "private_origin": ""}
        with patch(
            "tavern.review_notifications.deliver_text",
            new=AsyncMock(return_value=DeliveryResult(True, "sent")),
        ) as deliver:
            result = await notify_card_review(
                context=self.context,
                database=self.database,
                broker=self.broker,
                config=TavernConfig.from_mapping(
                    {
                        "runtime": {
                            "card_review_notification_mode": "private"
                        }
                    }
                ),
                participant=participant,
                approved=True,
                note="",
                logger=self.logger,
            )
        self.assertEqual(result, {"private": "missing"})
        deliver.assert_not_awaited()
        self.logger.warning.assert_called_once()

    async def test_approved_text_uses_primary_command_trigger(self) -> None:
        with patch(
            "tavern.review_notifications.deliver_text",
            new=AsyncMock(return_value=DeliveryResult(True, "sent")),
        ) as deliver:
            await notify_card_review(
                context=self.context,
                database=self.database,
                broker=self.broker,
                config=TavernConfig.from_mapping(
                    {
                        "runtime": {
                            "command_triggers": ["团", "跑团"],
                            "card_review_notification_mode": "private",
                        }
                    }
                ),
                participant=self.participant,
                approved=True,
                note="",
                logger=self.logger,
            )
        text = deliver.await_args.args[2]
        self.assertIn("/团 准备", text)
        self.assertNotIn("/酒馆 准备", text)


if __name__ == "__main__":
    unittest.main()
