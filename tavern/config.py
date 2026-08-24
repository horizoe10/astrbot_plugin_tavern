from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .constants import DEFAULT_WORLD_SLUG
from .command_triggers import normalize_command_triggers
from .lifecycle import normalize_time_rules


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _trigger_prefix(value: Any) -> str:
    text = str(value or "jg").strip()
    if (
        not text
        or len(text) > 16
        or any(char.isspace() or ord(char) < 33 for char in text)
    ):
        return "jg"
    return text


@dataclass(frozen=True, slots=True)
class TavernConfig:
    admin_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_group_ids: frozenset[str] = field(default_factory=frozenset)
    require_group_whitelist: bool = True
    unauthorized_command_behavior: str = "silent"
    public_status: bool = True

    provider_id: str = ""
    fallback_provider_ids: tuple[str, ...] = ()
    image_caption_provider_id: str = ""
    image_caption_prompt: str = (
        "请用简洁、客观的中文描述图片中与角色行动、场景、物品、"
        "人物姿态和可见文字有关的信息。不要猜测看不见的事实。"
    )
    max_images_per_turn: int = 4
    temperature: float = 0.5
    max_tokens: int = 1400
    request_timeout_seconds: int = 120
    json_repair_attempts: int = 1

    default_world_slug: str = DEFAULT_WORLD_SLUG
    trigger_prefix: str = "jg"
    command_triggers: tuple[str, ...] = ("酒馆",)
    two_phase_checks: bool = True
    max_input_chars: int = 2000
    max_output_chars: int = 5000
    enforce_mobile_output: bool = False
    recent_turns: int = 6
    memory_limit: int = 6
    user_cooldown_seconds: float = 1.5
    auto_snapshot_interval: int = 5
    card_review_notification_mode: str = "both"
    ooc_prefixes: tuple[str, ...] = ("【OOC】", "[OOC]", "OOC:")
    time_rules: Mapping[str, Any] = field(
        default_factory=normalize_time_rules
    )

    audit_retention_days: int = 90
    store_model_payloads: bool = False
    debug: bool = False

    # Token 配额默认值（v0.12.0）：新建副本无策略时用于播种默认策略。
    token_quota_enabled: bool = False
    token_quota_window_seconds: int = 86400
    token_quota_token_limit: int = 400000

    # 世界包远程市场（0.12.0-A3，#4）。
    world_market_enabled: bool = False
    world_market_remote_manifest_url: str = ""
    world_market_allowed_hosts: tuple[str, ...] = (
        "raw.githubusercontent.com",
        "github.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
    )
    world_market_cache_ttl_seconds: int = 600
    world_market_max_package_bytes: int = 2_000_000
    world_market_verify_sha256: bool = True

    # 自动备份（v0.12.0-A15）：后台按间隔导出完整 ZIP 备份并保留最近 N 份。
    auto_backup_enabled: bool = False
    auto_backup_interval_hours: float = 24.0
    auto_backup_keep_count: int = 7

    # Webhook 事件通知（v0.12.0-A15）：将酒馆事件推送到外部地址。
    webhook_enabled: bool = False
    webhook_urls: tuple[str, ...] = ()
    webhook_secret: str = ""
    webhook_events: tuple[str, ...] = ()  # 空 = 推送全部事件
    webhook_timeout_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TavernConfig":
        root = _mapping(raw)
        security = _mapping(root.get("security"))
        model = _mapping(root.get("model"))
        runtime = _mapping(root.get("runtime"))
        advanced = _mapping(root.get("advanced"))
        # B1：旧版 rich 配置刻意只读忽略。保留旧键不会导致升级失败，
        # 但运行时与 WebUI 不再暴露 QQ 专属富卡片分支。
        token_quota = _mapping(root.get("token_quota"))
        world_market = _mapping(root.get("world_market"))
        auto_backup = _mapping(root.get("auto_backup"))
        webhook = _mapping(root.get("webhook"))

        behavior = str(
            security.get("unauthorized_command_behavior", "silent")
        ).strip()
        if behavior not in {"silent", "deny"}:
            behavior = "silent"

        prefixes = _strings(runtime.get("ooc_prefixes"))
        if not prefixes:
            prefixes = ("【OOC】", "[OOC]", "OOC:")

        card_review_notification_mode = str(
            runtime.get("card_review_notification_mode") or "both"
        ).strip()
        if card_review_notification_mode not in {
            "both",
            "group",
            "private",
        }:
            card_review_notification_mode = "both"

        trigger_prefix = _trigger_prefix(
            runtime.get("trigger_prefix", "jg")
        )
        command_triggers = normalize_command_triggers(
            runtime.get("command_triggers"),
            story_trigger=trigger_prefix,
        )

        provider_id = str(model.get("provider_id", "")).strip()
        fixed_fallbacks = [
            str(model.get(f"fallback_provider_{index}_id", "") or "").strip()
            for index in range(1, 5)
        ]
        fallback_provider_ids = tuple(
            item
            for item in _strings(
                [
                    *fixed_fallbacks,
                    *_strings(model.get("fallback_provider_ids")),
                ]
            )[:8]
            if item != provider_id
        )
        image_caption_prompt = str(
            model.get(
                "image_caption_prompt",
                (
                    "请用简洁、客观的中文描述图片中与角色行动、场景、"
                    "物品、人物姿态和可见文字有关的信息。"
                    "不要猜测看不见的事实。"
                ),
            )
            or ""
        ).strip()
        if not image_caption_prompt:
            image_caption_prompt = (
                "请用简洁、客观的中文描述图片中与角色行动、场景、"
                "物品、人物姿态和可见文字有关的信息。"
                "不要猜测看不见的事实。"
            )
        image_caption_prompt = image_caption_prompt[:2000]

        return cls(
            admin_ids=frozenset(_strings(security.get("admin_ids"))),
            allowed_group_ids=frozenset(
                _strings(security.get("allowed_group_ids"))
            ),
            require_group_whitelist=bool(
                security.get("require_group_whitelist", True)
            ),
            unauthorized_command_behavior=behavior,
            public_status=bool(security.get("public_status", True)),
            provider_id=provider_id,
            fallback_provider_ids=fallback_provider_ids,
            image_caption_provider_id=str(
                model.get("image_caption_provider_id", "")
            ).strip(),
            image_caption_prompt=image_caption_prompt,
            max_images_per_turn=_bounded_int(
                model.get("max_images_per_turn"), 4, 1, 8
            ),
            temperature=_bounded_float(
                model.get("temperature"), 0.5, 0.0, 2.0
            ),
            max_tokens=_bounded_int(
                model.get("max_tokens"), 1400, 256, 16000
            ),
            request_timeout_seconds=_bounded_int(
                model.get("request_timeout_seconds"), 120, 15, 600
            ),
            json_repair_attempts=_bounded_int(
                model.get("json_repair_attempts"), 1, 0, 2
            ),
            default_world_slug=(
                str(
                    runtime.get("default_world_slug", DEFAULT_WORLD_SLUG)
                ).strip()
                or DEFAULT_WORLD_SLUG
            ),
            trigger_prefix=trigger_prefix,
            command_triggers=command_triggers,
            two_phase_checks=bool(
                runtime.get("two_phase_checks", True)
            ),
            max_input_chars=_bounded_int(
                runtime.get("max_input_chars"), 2000, 100, 12000
            ),
            max_output_chars=_bounded_int(
                runtime.get("max_output_chars"), 5000, 500, 20000
            ),
            enforce_mobile_output=bool(
                runtime.get("enforce_mobile_output", True)
            ),
            recent_turns=_bounded_int(
                runtime.get("recent_turns"), 6, 2, 50
            ),
            memory_limit=_bounded_int(
                runtime.get("memory_limit"), 6, 0, 40
            ),
            user_cooldown_seconds=_bounded_float(
                runtime.get("user_cooldown_seconds"), 1.5, 0.0, 60.0
            ),
            auto_snapshot_interval=_bounded_int(
                runtime.get("auto_snapshot_interval"), 5, 0, 100
            ),
            card_review_notification_mode=card_review_notification_mode,
            ooc_prefixes=prefixes,
            time_rules=normalize_time_rules(runtime.get("time_rules")),
            audit_retention_days=_bounded_int(
                advanced.get("audit_retention_days"), 90, 1, 3650
            ),
            store_model_payloads=bool(
                advanced.get("store_model_payloads", False)
            ),
            debug=bool(advanced.get("debug", False)),
            token_quota_enabled=bool(
                token_quota.get("enabled", False)
            ),
            token_quota_window_seconds=_bounded_int(
                token_quota.get("window_seconds"), 86400, 60, 30 * 86400
            ),
            token_quota_token_limit=_bounded_int(
                token_quota.get("token_limit"), 400000, 1000, 10_000_000
            ),
            world_market_enabled=bool(
                world_market.get("enabled", False)
            ),
            world_market_remote_manifest_url=str(
                world_market.get("remote_manifest_url", "") or ""
            ).strip(),
            world_market_allowed_hosts=(
                tuple(
                    _strings(world_market.get("allowed_hosts"))
                )
                or (
                    "raw.githubusercontent.com",
                    "github.com",
                    "objects.githubusercontent.com",
                    "codeload.github.com",
                )
            ),
            world_market_cache_ttl_seconds=_bounded_int(
                world_market.get("cache_ttl_seconds"), 600, 30, 86_400
            ),
            world_market_max_package_bytes=_bounded_int(
                world_market.get("max_package_bytes"), 2_000_000,
                10_000, 50_000_000,
            ),
            world_market_verify_sha256=bool(
                world_market.get("verify_sha256", True)
            ),
            auto_backup_enabled=bool(auto_backup.get("enabled", False)),
            auto_backup_interval_hours=_bounded_float(
                auto_backup.get("interval_hours"), 24.0, 1.0, 24 * 30
            ),
            auto_backup_keep_count=_bounded_int(
                auto_backup.get("keep_count"), 7, 1, 365
            ),
            webhook_enabled=bool(webhook.get("enabled", False)),
            webhook_urls=_strings(webhook.get("urls")),
            webhook_secret=str(webhook.get("secret") or "").strip(),
            webhook_events=_strings(webhook.get("events")),
            webhook_timeout_seconds=_bounded_float(
                webhook.get("timeout_seconds"), 10.0, 1.0, 120.0
            ),
        )

    def is_admin(self, sender_id: str) -> bool:
        return str(sender_id) in self.admin_ids

    def is_group_allowed(self, group_id: str) -> bool:
        normalized = str(group_id)
        if not self.require_group_whitelist:
            return True
        return normalized in self.allowed_group_ids

    @property
    def primary_command_trigger(self) -> str:
        return self.command_triggers[0]

    @property
    def primary_command_prefix(self) -> str:
        return f"/{self.primary_command_trigger}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "security": {
                "admin_ids": sorted(self.admin_ids),
                "allowed_group_ids": sorted(self.allowed_group_ids),
                "require_group_whitelist": self.require_group_whitelist,
                "unauthorized_command_behavior": (
                    self.unauthorized_command_behavior
                ),
                "public_status": self.public_status,
            },
            "model": {
                "provider_id": self.provider_id,
                **{
                    f"fallback_provider_{index}_id": (
                        self.fallback_provider_ids[index - 1]
                        if len(self.fallback_provider_ids) >= index
                        else ""
                    )
                    for index in range(1, 5)
                },
                # Keep the aggregate field for the built-in console and for
                # upgrading configurations written by 0.4.x.
                "fallback_provider_ids": list(
                    self.fallback_provider_ids
                ),
                "image_caption_provider_id": (
                    self.image_caption_provider_id
                ),
                "image_caption_prompt": self.image_caption_prompt,
                "max_images_per_turn": self.max_images_per_turn,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "request_timeout_seconds": self.request_timeout_seconds,
                "json_repair_attempts": self.json_repair_attempts,
            },
            "runtime": {
                "default_world_slug": self.default_world_slug,
                "trigger_prefix": self.trigger_prefix,
                "command_triggers": list(self.command_triggers),
                "two_phase_checks": self.two_phase_checks,
                "max_input_chars": self.max_input_chars,
                "max_output_chars": self.max_output_chars,
                "enforce_mobile_output": self.enforce_mobile_output,
                "recent_turns": self.recent_turns,
                "memory_limit": self.memory_limit,
                "user_cooldown_seconds": self.user_cooldown_seconds,
                "auto_snapshot_interval": self.auto_snapshot_interval,
                "card_review_notification_mode": (
                    self.card_review_notification_mode
                ),
                "ooc_prefixes": list(self.ooc_prefixes),
                "time_rules": dict(self.time_rules),
            },
            "advanced": {
                "audit_retention_days": self.audit_retention_days,
                "store_model_payloads": self.store_model_payloads,
                "debug": self.debug,
            },
            "token_quota": {
                "enabled": self.token_quota_enabled,
                "window_seconds": self.token_quota_window_seconds,
                "token_limit": self.token_quota_token_limit,
            },
            "world_market": {
                "enabled": self.world_market_enabled,
                "remote_manifest_url": self.world_market_remote_manifest_url,
                "allowed_hosts": list(self.world_market_allowed_hosts),
                "cache_ttl_seconds": self.world_market_cache_ttl_seconds,
                "max_package_bytes": self.world_market_max_package_bytes,
                "verify_sha256": self.world_market_verify_sha256,
            },
            "auto_backup": {
                "enabled": self.auto_backup_enabled,
                "interval_hours": self.auto_backup_interval_hours,
                "keep_count": self.auto_backup_keep_count,
            },
            "webhook": {
                "enabled": self.webhook_enabled,
                "urls": list(self.webhook_urls),
                "secret": self.webhook_secret,
                "events": list(self.webhook_events),
                "timeout_seconds": self.webhook_timeout_seconds,
            },
        }
