from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .constants import DEFAULT_WORLD_SLUG


# AstrBot's generic configuration form serializes an empty numeric input as
# ``0``.  RC9 deliberately reserves zero as an invalid world/runtime duration,
# while the host configuration contract uses an empty input (or ``-1``) to mean
# "unlimited".  Repair that host-only representation before it reaches the
# strict lifecycle validator; authored world data and runtime mutations still
# reject zero through ``normalize_time_rules``.
_HOST_OPTIONAL_TIME_RULE_KEYS = frozenset(
    {
        "card_code_ttl_seconds",
        "card_draft_ttl_seconds",
        "card_completion_timeout_seconds",
        "preparation_timeout_seconds",
        "ready_timeout_seconds",
        "turn_timeout_seconds",
        "turn_reminder_seconds",
        "standby_timeout_seconds",
        "delegation_ttl_seconds",
        "vote_round_one_seconds",
        "vote_round_two_seconds",
        "vote_reminder_seconds",
        "all_idle_pause_seconds",
    }
)


def _normalize_time_rules(value: Any = None) -> Mapping[str, Any]:
    # Keep config loading independent from the protocol/compiler import cycle.
    from .lifecycle import normalize_time_rules

    return normalize_time_rules(value)


def _runtime_time_rules(value: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = dict(_mapping(value.get("time_rules")))
    for key in _HOST_OPTIONAL_TIME_RULE_KEYS:
        if rules.get(key) == 0:
            rules[key] = -1
    rules["story_generation_reminder"] = {
        "enabled": _reminder_enabled(
            value.get("story_generation_reminder_enabled", True)
        ),
        "interval_seconds": _reminder_interval(
            value.get("story_generation_reminder_interval_seconds", 60)
        ),
        "source": "global_default",
        "revision": 0,
        "source_revision": 0,
    }
    return _normalize_time_rules(rules)


CONFIG_SCHEMA = "tavern-rc8-config/1.0.0"
CONFIG_PROBLEM_SCHEMA = "tavern-config-problem/1.0.0"
CONFIG_RESET_COPY = {
    "bot": (
        "配置已按 RC8 规则重新读取。无法识别或已移除的设置未被使用；"
        "请让管理员在插件设置中检查提示后重新保存。"
    ),
    "webui": (
        "部分旧设置与当前版本不兼容，已忽略并使用安全默认值。"
        "请检查标记项后重新保存配置。"
    ),
}

_CONFIG_FIELDS = {
    "security": frozenset(
        {
            "admin_ids", "allowed_group_ids", "require_group_whitelist",
            "unauthorized_command_behavior", "public_status",
        }
    ),
    "model": frozenset(
        {
            "provider_id", "fallback_provider_1_id", "fallback_provider_2_id",
            "fallback_provider_3_id", "fallback_provider_4_id",
            "fallback_provider_ids", "image_caption_provider_id",
            "image_caption_prompt", "max_images_per_turn", "temperature",
            "max_tokens", "request_timeout_seconds", "json_repair_attempts",
            "generation_budget_total_seconds", "generation_budget_max_calls",
            "generation_budget_per_call_seconds", "generation_budget_max_fallbacks",
        }
    ),
    "runtime": frozenset(
        {
            "default_world_slug", "trigger_prefix", "two_phase_checks",
            "max_input_chars", "max_output_chars", "enforce_mobile_output",
            "qqbot_markdown_enabled", "recent_turns", "memory_limit",
            "user_cooldown_seconds", "auto_snapshot_interval", "ooc_prefixes",
            "time_rules", "story_generation_reminder_enabled",
            "story_generation_reminder_interval_seconds",
        }
    ),
    "advanced": frozenset({"audit_retention_days", "store_model_payloads", "debug"}),
    "token_quota": frozenset({"enabled", "window_seconds", "token_limit"}),
    "auto_backup": frozenset({"enabled", "interval_hours", "keep_count"}),
    "webhook": frozenset({"enabled", "urls", "secret", "events", "timeout_seconds"}),
    "remote_panel": frozenset(
        {
            "enabled", "host", "port",
            "allow_insecure_http", "external_scheme", "secure_cookie",
            "trusted_proxy_cidrs",
        }
    ),
}
REMOVED_CONFIG_FIELDS = frozenset({"rich", "card", "legacy", "database", "world_protocol"})

# Credentials are deliberately not represented by a reusable masked value.
# A masked value is too easy for a legacy form to submit back and overwrite the
# real secret.  Public projections remove the credential and expose only a
# boolean ``*_configured`` fact; submitted placeholders mean "keep current".
_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "api_key",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "private_key",
        "refresh_token",
        "secret",
        "token",
        "access_token",
    }
)
_CREDENTIAL_FIELD_SUFFIXES = (
    "_api_key",
    "_client_secret",
    "_password",
    "_passphrase",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
    "_access_token",
)
_REDACTED_CREDENTIAL_PLACEHOLDERS = frozenset(
    {
        "******",
        "********",
        "************",
        "••••••",
        "••••••••",
        "••••••••••••",
        "<redacted>",
        "[redacted]",
        "__redacted__",
        "redacted",
        "已设置",
    }
)


def _is_credential_field(name: Any) -> bool:
    normalized = str(name or "").strip().casefold().replace("-", "_")
    return normalized in _CREDENTIAL_FIELD_NAMES or normalized.endswith(
        _CREDENTIAL_FIELD_SUFFIXES
    )


def _credential_marker_field(name: Any) -> str | None:
    normalized = str(name or "").strip()
    suffix = "_configured"
    if not normalized.casefold().endswith(suffix):
        return None
    credential = normalized[: -len(suffix)]
    return credential if _is_credential_field(credential) else None


def safe_config_projection(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-safe settings projection with no credential values.

    The helper is intentionally recursive so newly introduced nested password,
    token, API-key or secret fields cannot accidentally start appearing in the
    legacy settings response.  Non-credential settings keep their current
    shape; each removed credential is represented only by ``*_configured``.
    """

    def project(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key)
                if _is_credential_field(key):
                    marker = f"{key}_configured"
                    result[marker] = bool(result.get(marker)) or bool(
                        str(raw_value or "").strip()
                    )
                    continue
                if _credential_marker_field(key) is not None:
                    result[key] = bool(result.get(key)) or bool(raw_value)
                    continue
                result[key] = project(raw_value)
            return result
        if isinstance(item, tuple):
            return [project(entry) for entry in item]
        if isinstance(item, list):
            return [project(entry) for entry in item]
        return item

    return project(dict(value or {}))


def _credential_replacement(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("凭据字段必须是字符串；设置未保存")
    normalized = value.strip()
    if not normalized or normalized.casefold() in _REDACTED_CREDENTIAL_PLACEHOLDERS:
        return None
    if len(normalized) > 4096:
        raise ValueError("凭据字段过长；设置未保存")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("凭据字段包含不允许的控制字符；设置未保存")
    return normalized


@dataclass(frozen=True, slots=True)
class ConfigProblem:
    code: str
    path: str
    message: str
    schema: str = CONFIG_PROBLEM_SCHEMA


@dataclass(frozen=True, slots=True)
class ConfigLoadResult:
    config: "TavernConfig"
    sanitized: Mapping[str, Any]
    problems: tuple[ConfigProblem, ...]
    schema: str = CONFIG_SCHEMA

    @property
    def user_copy(self) -> Mapping[str, str]:
        return CONFIG_RESET_COPY if self.problems else {}


def validate_config_mapping(raw: Mapping[str, Any] | None) -> tuple[dict[str, Any], tuple[ConfigProblem, ...]]:
    """Return only RC8 schema fields plus structured ignored-field problems."""
    if raw is not None and not isinstance(raw, Mapping):
        return {}, (
            ConfigProblem(
                "CONFIG_ROOT_INVALID",
                "$",
                "插件配置必须是对象，已忽略原始值并使用安全默认值。",
            ),
        )
    root = _mapping(raw)
    sanitized: dict[str, Any] = {}
    problems: list[ConfigProblem] = []
    for section, value in root.items():
        section_name = str(section)
        if section_name in REMOVED_CONFIG_FIELDS:
            problems.append(ConfigProblem("CONFIG_FIELD_REMOVED", section_name, "该旧配置项已移除，未被运行时使用。"))
            continue
        allowed = _CONFIG_FIELDS.get(section_name)
        if allowed is None:
            problems.append(ConfigProblem("CONFIG_SECTION_UNKNOWN", section_name, "无法识别的配置分组已忽略。"))
            continue
        if not isinstance(value, Mapping):
            problems.append(ConfigProblem("CONFIG_SECTION_INVALID", section_name, "配置分组必须是对象，已使用安全默认值。"))
            continue
        clean_section: dict[str, Any] = {}
        for key, item in value.items():
            key_name = str(key)
            if key_name not in allowed:
                problems.append(ConfigProblem("CONFIG_FIELD_UNKNOWN", f"{section_name}.{key_name}", "无法识别的配置项已忽略。"))
                continue
            clean_section[key_name] = item
        sanitized[section_name] = clean_section
    return sanitized, tuple(problems)


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


def _reminder_interval(value: Any) -> int:
    if type(value) is not int:
        return 60
    if value < 30 or value > 600 or value % 15:
        return 60
    return value


def _reminder_enabled(value: Any) -> bool:
    return value if type(value) is bool else True


def _trigger_prefix(value: Any) -> str:
    text = str(value or "t").strip()
    if (
        not text
        or len(text) > 16
        or any(char.isspace() or ord(char) < 33 for char in text)
    ):
        return "t"
    return text


def _migrate_trigger_prefix(value: Any) -> str:
    """把旧默认前缀 ``jg`` 重置为当前前缀 ``t``。

    新安装继续默认 ``t``；升级读取旧配置时，若值恰为旧默认 ``jg`` 则迁移。
    部署者若确实想保留 ``jg``，可升级后在设置中重新填回。
    """
    text = str(value or "t").strip()
    if text == "jg":
        return "t"
    return _trigger_prefix(value)


def _external_scheme(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("http", "https") else "http"


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return None


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return _strings(value)


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
    generation_budget_total_seconds: int = 180
    generation_budget_max_calls: int = 3
    generation_budget_per_call_seconds: int = 90
    generation_budget_max_fallbacks: int = 1

    default_world_slug: str = DEFAULT_WORLD_SLUG
    trigger_prefix: str = "t"
    two_phase_checks: bool = True
    max_input_chars: int = 2000
    max_output_chars: int = 5000
    enforce_mobile_output: bool = True
    qqbot_markdown_enabled: bool = True
    recent_turns: int = 6
    memory_limit: int = 6
    user_cooldown_seconds: float = 1.5
    auto_snapshot_interval: int = 5
    story_generation_reminder_enabled: bool = True
    story_generation_reminder_interval_seconds: int = 60
    ooc_prefixes: tuple[str, ...] = ("【OOC】", "[OOC]", "OOC:")
    time_rules: Mapping[str, Any] = field(
        default_factory=_normalize_time_rules
    )

    audit_retention_days: int = 90
    store_model_payloads: bool = False
    debug: bool = False

    # Token 配额默认值（v1.0-A2）：新建副本无策略时用于播种默认策略。
    token_quota_enabled: bool = False
    token_quota_window_seconds: int = 86400
    token_quota_token_limit: int = 400000

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

    # 独立 Web 面板（1.0.0-A6）：监听地址/端口与明文开关。
    # 账号密码、IP 白名单、会话 TTL 等凭据存放在 data_dir/remote_panel.json，
    # 由 AstrBot 控制台（重置密码）与 CLI 工具维护。
    remote_panel_enabled: bool = True
    remote_panel_host: str = "127.0.0.1"
    remote_panel_port: int = 8766
    remote_panel_allow_insecure_http: bool = False
    remote_panel_external_scheme: str = "http"
    remote_panel_secure_cookie: bool | None = None
    remote_panel_trusted_proxy_cidrs: tuple[str, ...] = (
        "127.0.0.1",
        "::1",
    )
    remote_panel_public_url: str = ""

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
        auto_backup = _mapping(root.get("auto_backup"))
        webhook = _mapping(root.get("webhook"))
        remote_panel = _mapping(root.get("remote_panel"))

        behavior = str(
            security.get("unauthorized_command_behavior", "silent")
        ).strip()
        if behavior not in {"silent", "deny"}:
            behavior = "silent"

        prefixes = _strings(runtime.get("ooc_prefixes"))
        if not prefixes:
            prefixes = ("【OOC】", "[OOC]", "OOC:")

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
            generation_budget_total_seconds=_bounded_int(
                model.get("generation_budget_total_seconds"), 180, 15, 1800
            ),
            generation_budget_max_calls=_bounded_int(
                model.get("generation_budget_max_calls"), 3, 1, 20
            ),
            generation_budget_per_call_seconds=_bounded_int(
                model.get("generation_budget_per_call_seconds"), 90, 5, 600
            ),
            generation_budget_max_fallbacks=_bounded_int(
                model.get("generation_budget_max_fallbacks"), 1, 0, 8
            ),
            default_world_slug=(
                str(
                    runtime.get("default_world_slug", DEFAULT_WORLD_SLUG)
                ).strip()
                or DEFAULT_WORLD_SLUG
            ),
            trigger_prefix=_migrate_trigger_prefix(
                runtime.get("trigger_prefix", "t")
            ),
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
            qqbot_markdown_enabled=bool(
                runtime.get("qqbot_markdown_enabled", True)
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
            story_generation_reminder_enabled=_reminder_enabled(
                runtime.get("story_generation_reminder_enabled", True)
            ),
            story_generation_reminder_interval_seconds=_reminder_interval(
                runtime.get("story_generation_reminder_interval_seconds", 60)
            ),
            ooc_prefixes=prefixes,
            time_rules=_runtime_time_rules(runtime),
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
            remote_panel_enabled=bool(remote_panel.get("enabled", True)),
            remote_panel_host=str(
                remote_panel.get("host") or "127.0.0.1"
            ).strip() or "127.0.0.1",
            remote_panel_port=_bounded_int(
                remote_panel.get("port"), 8766, 1, 65535
            ),
            remote_panel_allow_insecure_http=bool(
                remote_panel.get("allow_insecure_http", False)
            ),
            remote_panel_external_scheme=_external_scheme(
                remote_panel.get("external_scheme")
            ),
            remote_panel_secure_cookie=_optional_bool(
                remote_panel.get("secure_cookie")
            ),
            remote_panel_trusted_proxy_cidrs=(
                _string_list(remote_panel.get("trusted_proxy_cidrs"))
                or ("127.0.0.1", "::1")
            ),
            remote_panel_public_url=str(
                remote_panel.get("public_url", "") or ""
            ).strip().rstrip("/"),
        )

    @classmethod
    def load_validated(
        cls, raw: Mapping[str, Any] | None
    ) -> ConfigLoadResult:
        """Validate the RC8 boundary, ignore problems, then build runtime state."""
        sanitized, problems = validate_config_mapping(raw)
        return ConfigLoadResult(
            config=cls.from_mapping(sanitized),
            sanitized=sanitized,
            problems=problems,
        )

    def is_admin(self, sender_id: str) -> bool:
        return str(sender_id) in self.admin_ids

    def is_group_allowed(self, group_id: str) -> bool:
        normalized = str(group_id)
        if not self.require_group_whitelist:
            return True
        return normalized in self.allowed_group_ids

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
                "image_caption_provider_id": (
                    self.image_caption_provider_id
                ),
                "image_caption_prompt": self.image_caption_prompt,
                "max_images_per_turn": self.max_images_per_turn,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "request_timeout_seconds": self.request_timeout_seconds,
                "json_repair_attempts": self.json_repair_attempts,
                "generation_budget_total_seconds": (
                    self.generation_budget_total_seconds
                ),
                "generation_budget_max_calls": self.generation_budget_max_calls,
                "generation_budget_per_call_seconds": (
                    self.generation_budget_per_call_seconds
                ),
                "generation_budget_max_fallbacks": (
                    self.generation_budget_max_fallbacks
                ),
            },
            "runtime": {
                "default_world_slug": self.default_world_slug,
                "trigger_prefix": self.trigger_prefix,
                "two_phase_checks": self.two_phase_checks,
                "max_input_chars": self.max_input_chars,
                "max_output_chars": self.max_output_chars,
                "enforce_mobile_output": self.enforce_mobile_output,
                "qqbot_markdown_enabled": self.qqbot_markdown_enabled,
                "recent_turns": self.recent_turns,
                "memory_limit": self.memory_limit,
                "user_cooldown_seconds": self.user_cooldown_seconds,
                "auto_snapshot_interval": self.auto_snapshot_interval,
                "story_generation_reminder_enabled": (
                    self.story_generation_reminder_enabled
                ),
                "story_generation_reminder_interval_seconds": (
                    self.story_generation_reminder_interval_seconds
                ),
                "ooc_prefixes": list(self.ooc_prefixes),
                # story_generation_reminder 是运行时冻结快照，不是宿主配置
                # 字段；宿主只持久化上方两个已中文化的提醒设置。
                "time_rules": {
                    key: (
                        -1
                        if key in _HOST_OPTIONAL_TIME_RULE_KEYS and value is None
                        else value
                    )
                    for key, value in self.time_rules.items()
                    if key != "story_generation_reminder"
                },
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
            "remote_panel": {
                "enabled": self.remote_panel_enabled,
                "host": self.remote_panel_host,
                "port": self.remote_panel_port,
                "allow_insecure_http": self.remote_panel_allow_insecure_http,
                "external_scheme": self.remote_panel_external_scheme,
                "secure_cookie": self.remote_panel_secure_cookie,
                "trusted_proxy_cidrs": list(
                    self.remote_panel_trusted_proxy_cidrs
                ),
            },
        }

def merge_config_payload(
    current: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge a partial settings payload over the current plugin config.

    The WebUI settings form only submits the sections it renders
    (security / model / runtime / advanced).  Sections such as
    ``token_quota`` / ``auto_backup`` / ``webhook``
    are managed by the AstrBot host schema and must survive a plugin
    console save instead of silently resetting to defaults (1.0.0-A3).
    Ordinary leaf values of submitted sections replace the stored value.  A
    credential is different: omitted, ``None``, blank, or a recognised masked
    placeholder preserves the current value; a non-empty string explicitly
    replaces it.  Unknown sections/fields and malformed sections fail closed.
    """
    root = dict(current or {})
    if payload is None:
        return root
    if not isinstance(payload, Mapping):
        raise ValueError("设置请求必须是对象；设置未保存")
    submitted = dict(payload)
    for section, value in submitted.items():
        section_name = str(section)
        allowed_fields = _CONFIG_FIELDS.get(section_name)
        if allowed_fields is None:
            raise ValueError(f"不支持的设置分区：{section_name}")
        if not isinstance(value, Mapping):
            raise ValueError(f"设置分区 {section_name} 必须是对象")
        unexpected = sorted(
            str(field)
            for field in value
            if field not in allowed_fields
            and _credential_marker_field(field) is None
        )
        if unexpected:
            raise ValueError(
                f"设置分区 {section_name} 包含不支持的字段；设置未保存"
            )
        existing = root.get(section_name)
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        for raw_field, raw_value in value.items():
            field_name = str(raw_field)
            if _credential_marker_field(field_name) is not None:
                continue
            if _is_credential_field(field_name):
                replacement = _credential_replacement(raw_value)
                if replacement is None:
                    continue
                merged[field_name] = replacement
            else:
                merged[field_name] = raw_value
        root[section_name] = merged
    return root
