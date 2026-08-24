"""Best-effort notifications for completed character-card reviews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .chat_experience import normalize_chat_experience
from .config import TavernConfig
from .platform_delivery import send_text as deliver_text
from .command_triggers import render_command_text


def _review_text(
    participant: Mapping[str, Any],
    *,
    approved: bool,
    note: str,
) -> str:
    name = str(
        participant.get("character_name")
        or participant.get("display_name")
        or participant.get("group_user_id")
        or participant.get("private_user_id")
        or "角色"
    ).strip()
    lines = [
        f"【角色卡审核】{name} · {'已通过' if approved else '已驳回'}"
    ]
    clean_note = str(note or "").strip()
    if clean_note:
        lines.append(f"备注：{clean_note}")
    if approved:
        lines.append("请回到所属群聊发送 /酒馆 准备。")
    else:
        lines.append("请重新建卡或联系主持人确认调整方式。")
    return "\n".join(lines)


async def _fallback_policy(database: Any, session_id: str) -> str:
    try:
        instance = await database.get_instance_config(session_id)
        return str(
            normalize_chat_experience(instance.get("world_snapshot") or {})
            ["delivery"]["proactive_fallback"]
        )
    except Exception:
        return "next_event"


async def _send_or_queue(
    *,
    context: Any,
    database: Any,
    broker: Any,
    session_id: str,
    participant: Mapping[str, Any],
    channel: str,
    origin: str,
    text: str,
    approved: bool,
) -> str:
    result = await deliver_text(context, origin, text, proactive=True)
    if result.ok:
        return "sent"
    policy = await _fallback_policy(database, session_id)
    if policy == "discard":
        return "discarded"
    kind = f"card.review.{channel}"
    stored_kind = f"webui_only:{kind}" if policy == "webui_only" else kind
    outcome = "approved" if approved else "rejected"
    dedupe_key = (
        f"card-review:{participant.get('id', '')}:"
        f"{participant.get('character_version_id', '')}:"
        f"{outcome}:{channel}"
    )
    await database.queue_delivery(
        session_id=session_id,
        origin=origin,
        kind=stored_kind,
        text=text,
        reason=result.reason,
        dedupe_key=dedupe_key,
    )
    await broker.publish(
        {
            "type": "delivery",
            "action": "queued",
            "session_id": session_id,
        }
    )
    return "queued"


async def notify_card_review(
    *,
    context: Any,
    database: Any,
    broker: Any,
    config: TavernConfig,
    participant: Mapping[str, Any],
    approved: bool,
    note: str,
    logger: Any,
) -> dict[str, str]:
    """Notify selected channels and return each channel's terminal status."""

    mode = config.card_review_notification_mode
    channels = {
        "both": ("private", "group"),
        "group": ("group",),
        "private": ("private",),
    }.get(mode, ("private", "group"))
    session_id = str(participant.get("session_id") or "").strip()
    body = render_command_text(
        _review_text(participant, approved=approved, note=note),
        config.primary_command_trigger,
    )
    display_name = str(
        participant.get("display_name")
        or participant.get("group_user_id")
        or participant.get("private_user_id")
        or "成员"
    ).strip()
    statuses: dict[str, str] = {}
    for channel in channels:
        try:
            if channel == "private":
                origin = str(participant.get("private_origin") or "").strip()
                text = body
            else:
                session = await database.get_session(session_id)
                origin = str(session.get("unified_origin") or "").strip()
                text = f"@{display_name}\n{body}"
            if not origin:
                statuses[channel] = "missing"
                logger.warning(
                    "角色卡审核通知缺少会话来源：session=%s channel=%s",
                    session_id,
                    channel,
                )
                continue
            statuses[channel] = await _send_or_queue(
                context=context,
                database=database,
                broker=broker,
                session_id=session_id,
                participant=participant,
                channel=channel,
                origin=origin,
                text=text,
                approved=approved,
            )
        except Exception as exc:
            statuses[channel] = "failed"
            logger.warning(
                "角色卡审核通知失败：session=%s channel=%s error=%s",
                session_id,
                channel,
                exc,
            )
    return statuses


__all__ = ["notify_card_review"]
