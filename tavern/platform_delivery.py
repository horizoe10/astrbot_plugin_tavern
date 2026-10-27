"""Platform-neutral text delivery helpers for v0.12.0.

The story engine only emits text.  Adapter differences are represented as
capabilities and delivery results instead of platform-specific branches in
the game rules.  Unknown adapters receive conservative defaults: event
replies remain available, while proactive delivery is attempted only when a
caller explicitly asks for it and its outcome is reported honestly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    platform: str
    text_reply: bool = True
    group_conversation: bool = True
    private_conversation: bool = True
    # Unified origins may start with a user-defined platform instance id.
    # Unknown ids optimistically attempt AstrBot's standard text API; callers
    # still persist any unconfirmed or failed send instead of claiming success.
    proactive_send: bool = True
    mentions: bool = False
    threads: bool = False
    max_text_length: int = 3500
    identity_scope: str = "adapter_instance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    status: str
    reason: str = ""
    attempted_parts: int = 0
    sent_parts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The table describes transport behavior only.  Business features never
# branch on these names; they ask for a capability instead.
_CAPABILITIES: dict[str, PlatformCapabilities] = {
    "aiocqhttp": PlatformCapabilities("aiocqhttp", proactive_send=True, mentions=True),
    "qq_official": PlatformCapabilities("qq_official", mentions=True, max_text_length=1800),
    "qq_official_webhook": PlatformCapabilities("qq_official_webhook", mentions=True, max_text_length=1800),
    "telegram": PlatformCapabilities("telegram", proactive_send=True, mentions=True, threads=True, max_text_length=3900),
    "lark": PlatformCapabilities("lark", proactive_send=True, mentions=True, threads=True),
    "slack": PlatformCapabilities("slack", proactive_send=True, mentions=True, threads=True),
    "discord": PlatformCapabilities("discord", proactive_send=True, mentions=True, threads=True, max_text_length=1900),
    "misskey": PlatformCapabilities("misskey", proactive_send=True, max_text_length=2800),
    "satori": PlatformCapabilities("satori", proactive_send=True, mentions=True),
    "dingtalk": PlatformCapabilities("dingtalk", mentions=True),
    "kook": PlatformCapabilities("kook", mentions=True, threads=True),
    "wecom": PlatformCapabilities("wecom", mentions=True),
    "wecom_ai_bot": PlatformCapabilities("wecom_ai_bot", mentions=True),
    "weixin_official_account": PlatformCapabilities("weixin_official_account", group_conversation=False, max_text_length=1900),
    "weixin_oc": PlatformCapabilities("weixin_oc", group_conversation=False, max_text_length=1900),
    "line": PlatformCapabilities("line", max_text_length=4500),
    "matrix": PlatformCapabilities("matrix", mentions=True, threads=True),
    "mattermost": PlatformCapabilities("mattermost", mentions=True, threads=True),
    "vocechat": PlatformCapabilities("vocechat", mentions=True),
    "webchat": PlatformCapabilities("webchat", proactive_send=True, mentions=False),
}


def normalize_platform(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        # Older AstrBot OneBot unified origins commonly used the short
        # ``qq`` prefix.  Keep that identity mapping while removing the
        # retired qq_restapi transport itself.
        "qq": "aiocqhttp",
        "qqofficial": "qq_official",
        "qqofficial_webhook": "qq_official_webhook",
        "onebot": "aiocqhttp",
        "onebot_v11": "aiocqhttp",
    }
    return aliases.get(text, text or "unknown")


def platform_from_origin(origin: Any) -> str:
    return normalize_platform(str(origin or "").split(":", 1)[0])


def capabilities_for(platform_or_origin: Any) -> PlatformCapabilities:
    raw = str(platform_or_origin or "")
    platform = platform_from_origin(raw) if ":" in raw else normalize_platform(raw)
    return _CAPABILITIES.get(platform, PlatformCapabilities(platform))


def capability_matrix() -> list[dict[str, Any]]:
    return [item.to_dict() for item in sorted(_CAPABILITIES.values(), key=lambda row: row.platform)]


def split_text(text: Any, maximum: int) -> list[str]:
    """Split text without breaking paragraphs or losing content."""

    value = str(text or "").strip()
    limit = max(256, int(maximum or 3500))
    if not value:
        return []
    if len(value) <= limit:
        return [value]
    parts: list[str] = []
    remaining = value
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        window = remaining[: limit + 1]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"), window.rfind("；"))
        if cut < limit // 3:
            cut = limit
        else:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [part for part in parts if part]


async def send_text(
    context: Any,
    origin: Any,
    text: Any,
    *,
    proactive: bool,
) -> DeliveryResult:
    target = str(origin or "").strip()
    content = str(text or "").strip()
    if not target:
        return DeliveryResult(False, "invalid_target", "没有可用的会话来源")
    if not content:
        return DeliveryResult(False, "empty", "消息内容为空")
    sender = getattr(context, "send_message", None)
    if not callable(sender):
        return DeliveryResult(False, "unavailable", "AstrBot 文本发送接口不可用")
    capabilities = capabilities_for(target)
    if proactive and not capabilities.proactive_send:
        return DeliveryResult(
            False,
            "queued_required",
            "当前平台未声明可靠的主动推送能力，将等待下一次会话消息补发",
        )
    parts = split_text(content, capabilities.max_text_length)
    sent_parts = 0
    try:
        from astrbot.api.event import MessageChain

        for part in parts:
            result = await sender(target, MessageChain().message(part))
            # None is deliberately not treated as success.  Several adapters
            # use it when a proactive request is skipped.
            if result is False or result is None:
                return DeliveryResult(
                    False,
                    "rejected",
                    "平台未确认消息已发送",
                    len(parts),
                    sent_parts,
                )
            sent_parts += 1
    except Exception as exc:
        return DeliveryResult(
            False,
            "exception",
            f"发送异常：{type(exc).__name__}: {str(exc)[:160]}",
            len(parts),
            sent_parts,
        )
    return DeliveryResult(True, "sent", "", len(parts), sent_parts)


__all__ = [
    "DeliveryResult",
    "PlatformCapabilities",
    "capabilities_for",
    "capability_matrix",
    "normalize_platform",
    "platform_from_origin",
    "send_text",
    "split_text",
]
