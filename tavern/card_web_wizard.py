"""网页建卡（/cw）：令牌、配额、状态渲染与独立面板接口。

职责边界：

- 聊天侧（``/团 网页建卡``）经 :class:`CardWebLinkGateway` 签发一次性链接
  令牌；令牌登记在独立面板进程内的 :class:`CardWebRegistry`（内存态，
  面板重启即失效，玩家重新发送指令即可）；
- 浏览器侧：静态建卡页 ``GET /cw`` 用链接令牌一次性兑换会话令牌（Bearer
  头调用），所有数据操作经 :class:`CardWebMixin` 转发给插件既有的
  ``fill_card_draft`` / ``card_ai``，字段校验与依赖清理不绕行；
- AI 配额按 QQ × 副本 × 字段 × 自然日持久化在草稿 ``fields_json`` 的
  ``_ai_quota``（经 :meth:`CharacterWebStateRepositoryMixin.set_card_web_state`）；
- 「网页激活中」标记 ``_web_active_until`` 使聊天侧候选投递静默。

安全边界：链接/会话令牌只存 SHA-256 哈希；会话仅授权访问本人草稿；
AI 接口受每草稿配额与全局并发（BoundedSemaphore）双重限制；/cw 命名
空间不要求面板管理员白名单，但 POST 仍复用面板同源校验与按 IP 限速。
"""
from __future__ import annotations

import hashlib
import hmac
import random
import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any

from .card_ai import CardAIError
from .database_support import DatabaseNotFoundError
from .card_wizard import (
    preset_options,
    resolve_current_wizard_step,
)
from .lifecycle.character_creation import staged_creation
from .lifecycle.world_time import CARD_STAGE_A
from .repositories.character_web_state import (
    AI_QUOTA_KEY,
    WEB_ACTIVE_KEY,
)

LINK_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 30 * 60
WEB_ACTIVE_WINDOW_SECONDS = 10 * 60
# 全局同时进行的 AI 生成数量上限（I/O 等待为主，4 核服务器足够）。
AI_GLOBAL_CONCURRENCY = 3
# 网页会话并发上限：min(硬顶, max(下限, 活跃建卡草稿数 + 余量))。
CAPACITY_HARD_LIMIT = 25
CAPACITY_FLOOR = 10
CAPACITY_MARGIN = 5
AI_DAILY_LIMIT = 1
_EXCHANGE_IP_LIMIT = 10
_EXCHANGE_IP_WINDOW = 60.0

_MODES = ("random", "expand")

# 字段类型分组：文本类可随机/补全（走 LLM），选项类只可随机（本地随机函数），
# 数值类不支持自动生成。
_TEXT_FIELD_TYPES = frozenset({"text", "textarea"})
_OPTION_FIELD_TYPES = frozenset({"select", "preset_select", "multi_select"})


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _day_key() -> str:
    return time.strftime("%Y-%m-%d")


def web_active_until(fields: Mapping[str, Any] | None) -> float:
    """读取草稿的网页激活截止时间（0 表示未激活）。"""

    if not isinstance(fields, Mapping):
        return 0.0
    try:
        return max(0.0, float(fields.get(WEB_ACTIVE_KEY) or 0.0))
    except (TypeError, ValueError):
        return 0.0


class CardWebRegistry:
    """链接/会话令牌登记表（内存态，线程安全）。

    链接令牌一次性、按签发顺序覆盖旧值；会话令牌滑动续期。面板重启后
    登记表清空，玩家重新发送 ``/团 网页建卡`` 即可，无需持久化。
    """

    def __init__(self, logger: Any = None) -> None:
        self._logger = logger
        self._lock = threading.RLock()
        self._links: dict[str, dict[str, float]] = {}  # origin -> entry
        self._sessions: dict[str, dict[str, Any]] = {}  # token_hash -> entry
        self._review_links: dict[str, dict[str, Any]] = {}  # admin_id -> entry
        self._review_sessions: dict[str, dict[str, Any]] = {}  # token_hash -> entry
        self._exchange_hits: dict[str, list[float]] = {}
        self.ai_semaphore = threading.BoundedSemaphore(AI_GLOBAL_CONCURRENCY)

    # ── 链接令牌 ──
    def issue_link(self, origin: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._links[str(origin or "")] = {
                "token_hash": _hash_token(token),
                "expires_at": time.time() + LINK_TTL_SECONDS,
            }
        return token

    def consume_link(self, token: str) -> str:
        """一次性兑换：成功返回 origin 并作废链接，失败抛 ValueError。"""

        digest = _hash_token(token)
        now = time.time()
        with self._lock:
            for origin, entry in list(self._links.items()):
                if entry.get("expires_at", 0) < now:
                    self._links.pop(origin, None)
                    continue
                if hmac_equal(entry.get("token_hash"), digest):
                    self._links.pop(origin, None)
                    return origin
        raise ValueError("链接无效或已过期，请在 QQ 私聊重新发送 /团 网页建卡")

    # ── 会话令牌 ──
    def issue_session(self, origin: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_hash_token(token)] = {
                "origin": str(origin or ""),
                "expires_at": time.time() + SESSION_TTL_SECONDS,
            }
        return token

    def resolve_session(self, token: str) -> str:
        digest = _hash_token(token)
        now = time.time()
        with self._lock:
            entry = self._sessions.get(digest)
            if not entry or entry.get("expires_at", 0) < now:
                self._sessions.pop(digest, None)
                return ""
            entry["expires_at"] = now + SESSION_TTL_SECONDS
            return str(entry.get("origin") or "")

    def session_count(self) -> int:
        now = time.time()
        with self._lock:
            for digest in [
                key
                for key, entry in self._sessions.items()
                if entry.get("expires_at", 0) < now
            ]:
                self._sessions.pop(digest, None)
            return len(self._sessions)

    def revoke_origin(self, origin: str) -> None:
        with self._lock:
            self._links.pop(str(origin or ""), None)
            for digest in [
                key
                for key, entry in self._sessions.items()
                if entry.get("origin") == str(origin or "")
            ]:
                self._sessions.pop(digest, None)

    # ── 网页审核（/cw/review）令牌 ──
    def issue_review_link(
        self, admin_id: str, *, bypass_host: bool = False
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._review_links[str(admin_id or "")] = {
                "token_hash": _hash_token(token),
                "expires_at": time.time() + LINK_TTL_SECONDS,
                "bypass_host": bool(bypass_host),
            }
        return token

    def consume_review_link(self, token: str) -> dict[str, Any]:
        """一次性兑换：成功返回 ``{"admin_id", "bypass_host"}`` 并作废链接。"""

        digest = _hash_token(token)
        now = time.time()
        with self._lock:
            for admin_id, entry in list(self._review_links.items()):
                if entry.get("expires_at", 0) < now:
                    self._review_links.pop(admin_id, None)
                    continue
                if hmac_equal(entry.get("token_hash"), digest):
                    self._review_links.pop(admin_id, None)
                    return {
                        "admin_id": str(admin_id or ""),
                        "bypass_host": bool(entry.get("bypass_host")),
                    }
        raise ValueError("链接无效或已过期，请在 QQ 私聊重新发送 /团 审核")

    def issue_review_session(
        self, admin_id: str, *, bypass_host: bool = False
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._review_sessions[_hash_token(token)] = {
                "admin_id": str(admin_id or ""),
                "bypass_host": bool(bypass_host),
                "expires_at": time.time() + SESSION_TTL_SECONDS,
            }
        return token

    def resolve_review_session(self, token: str) -> dict[str, Any]:
        digest = _hash_token(token)
        now = time.time()
        with self._lock:
            entry = self._review_sessions.get(digest)
            if not entry or entry.get("expires_at", 0) < now:
                self._review_sessions.pop(digest, None)
                return {}
            entry["expires_at"] = now + SESSION_TTL_SECONDS
            return {
                "admin_id": str(entry.get("admin_id") or ""),
                "bypass_host": bool(entry.get("bypass_host")),
            }

    # ── 兑换接口按 IP 限速 ──
    def exchange_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [
                stamp
                for stamp in self._exchange_hits.get(ip, [])
                if now - stamp < _EXCHANGE_IP_WINDOW
            ]
            if len(hits) >= _EXCHANGE_IP_LIMIT:
                self._exchange_hits[ip] = hits
                return False
            hits.append(now)
            self._exchange_hits[ip] = hits
            return True


def hmac_equal(left: Any, right: Any) -> bool:
    return hmac.compare_digest(
        str(left or "").encode("utf-8"), str(right or "").encode("utf-8")
    )


def ai_quota_left(fields: Mapping[str, Any] | None, mode: str) -> int:
    """当前自然日内该草稿（对应字段在外层判断）剩余次数。"""

    if mode not in _MODES:
        return 0
    quota = _quota_mapping(fields)
    day = quota.get(_day_key())
    return AI_DAILY_LIMIT if not isinstance(day, Mapping) else 0


def _quota_mapping(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        return {}
    quota = fields.get(AI_QUOTA_KEY)
    return dict(quota) if isinstance(quota, Mapping) else {}


def quota_left_for_field(
    fields: Mapping[str, Any] | None, field_key: str, mode: str
) -> int:
    if mode not in _MODES or not str(field_key or ""):
        return 0
    quota = _quota_mapping(fields)
    day = quota.get(_day_key())
    if not isinstance(day, Mapping):
        return AI_DAILY_LIMIT
    field_quota = day.get(str(field_key))
    used = (
        int(field_quota.get(mode, 0) or 0)
        if isinstance(field_quota, Mapping)
        else 0
    )
    return max(0, AI_DAILY_LIMIT - used)


def consume_quota_for_field(
    fields: Mapping[str, Any] | None, field_key: str, mode: str
) -> dict[str, Any]:
    """在草稿配额上记一次消耗，返回写回 ``_ai_quota`` 的新映射。"""

    quota = _quota_mapping(fields)
    today = _day_key()
    day = dict(quota.get(today)) if isinstance(quota.get(today), Mapping) else {}
    field_quota = dict(day.get(str(field_key))) if isinstance(
        day.get(str(field_key)), Mapping
    ) else {}
    field_quota[mode] = int(field_quota.get(mode, 0) or 0) + 1
    day[str(field_key)] = field_quota
    # 只保留当天与昨天两份，避免草稿 JSON 无限增长。
    kept = {key: value for key, value in quota.items() if key != today}
    recent = sorted(kept.keys())[-1:]
    result = {key: kept[key] for key in recent}
    result[today] = day
    return result


class CardWebLinkGateway:
    """聊天侧链接签发网关（由插件入口层注入 CardCommandService）。

    ``issue_link`` 返回 ``(链接, 失败原因)``；失败原因为空表示成功。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    async def issue_link(
        self, origin: str, draft: Mapping[str, Any]
    ) -> tuple[str, str]:
        del draft
        server = getattr(self._plugin, "_panel_server", None)
        registry = getattr(server, "card_registry", None)
        if server is None or registry is None:
            return (
                "",
                "网页建卡未启用：请在插件配置中开启独立 Web 面板并重新加载插件后，"
                "再发送 /团 网页建卡。",
            )
        base, note = self._panel_base_url()
        token = registry.issue_link(origin)
        return f"{base}/cw?token={token}", note

    async def issue_review_link(
        self, admin_id: str, *, bypass_host: bool = False
    ) -> tuple[str, str]:
        """为主持人签发网页审核链接（/cw/review，一次性令牌）。"""

        server = getattr(self._plugin, "_panel_server", None)
        registry = getattr(server, "card_registry", None)
        if server is None or registry is None:
            return (
                "",
                "网页审核未启用：请在插件配置中开启独立 Web 面板并重新加载插件后，"
                "再发送 /团 审核。",
            )
        base, note = self._panel_base_url()
        token = registry.issue_review_link(admin_id, bypass_host=bypass_host)
        return f"{base}/cw/review?token={token}", note

    def _panel_base_url(self) -> tuple[str, str]:
        config = self._plugin.runtime_config()
        base = str(
            getattr(config, "remote_panel_public_url", "") or ""
        ).strip().rstrip("/")
        if base:
            return base, ""
        host = str(getattr(config, "remote_panel_host", "") or "127.0.0.1")
        port = int(getattr(config, "remote_panel_port", 8766) or 8766)
        url = f"http://{host}:{port}"
        note = ""
        if host in {"127.0.0.1", "::1", "localhost", ""}:
            note = "（当前仅本机可访问；公网使用请在插件配置 remote_panel.public_url 填写外网地址）"
        return url, note


class CardWebMixin:
    """独立面板 handler 的网页建卡命名空间（/cw）。"""

    # ---- 入口（由 assets._handle 在管理员白名单之前调用） ----
    def _handle_card_wizard(self, path: str) -> None:
        try:
            if path == "/cw":
                if self.command == "GET":
                    self._send_html(_CARD_WIZARD_HTML)
                    return
                self._send_json({"error": "不支持的方法"}, status=405)
                return
            if path == "/cw/review":
                if self.command == "GET":
                    self._send_html(_CARD_REVIEW_HTML)
                    return
                self._send_json({"error": "不支持的方法"}, status=405)
                return
            if not path.startswith("/cw/api/"):
                self._send_json({"error": "未找到"}, status=404)
                return
            action = path[len("/cw/api/"):].strip("/")
            if self.command == "POST":
                if not self._same_origin_ok():
                    self._send_json(
                        {"error": "跨站请求被拒绝"},
                        status=403,
                    )
                    return
                if action == "exchange":
                    self._cw_exchange()
                    return
                if action == "review-exchange":
                    self._cw_review_exchange()
                    return
            # 网页审核 API 使用独立的审核会话令牌，不走建卡会话认证。
            if action == "review-state" and self.command == "GET":
                self._cw_review_state()
                return
            if action == "review-decide" and self.command == "POST":
                self._cw_review_decide()
                return
            if self.command != "GET" and self.command != "POST":
                self._send_json({"error": "不支持的方法"}, status=405)
                return
            origin = self._cw_origin()
            if not origin:
                self._send_json(
                    {"error": "会话已失效，请在 QQ 私聊重新发送 /团 网页建卡"},
                    status=401,
                )
                return
            handlers = {
                ("GET", "state"): self._cw_state,
                ("GET", "preview"): self._cw_preview,
                ("POST", "fill"): self._cw_fill,
                ("POST", "ai"): self._cw_ai,
                ("POST", "previous"): self._cw_previous,
                ("POST", "modify"): self._cw_modify,
                ("POST", "confirm"): self._cw_confirm,
                ("POST", "cancel"): self._cw_cancel,
                ("POST", "restart"): self._cw_restart,
            }
            handler = handlers.get((self.command, action))
            if handler is None:
                self._send_json({"error": "未找到"}, status=404)
                return
            handler(origin)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except CardAIError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            self.panel.logger.exception("321开团网页建卡处理异常")
            try:
                self._send_json({"error": "服务器内部错误"}, status=500)
            except Exception:
                pass

    # ---- 认证与工具 ----
    def _cw_origin(self) -> str:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.strip().lower() != "bearer":
            return ""
        registry = self._cw_registry()
        if registry is None:
            return ""
        return registry.resolve_session(token.strip())

    def _cw_registry(self) -> CardWebRegistry | None:
        registry = getattr(self.panel, "card_registry", None)
        return registry if isinstance(registry, CardWebRegistry) else None

    def _cw_exchange(self) -> None:
        registry = self._cw_registry()
        if registry is None:
            self._send_json(
                {"error": "网页建卡未启用：面板未就绪，请稍后重试或联系管理员"},
                status=503,
            )
            return
        ip = self._client_ip()
        if not registry.exchange_allowed(ip):
            self._send_json({"error": "尝试过于频繁，请一分钟后再试"}, status=429)
            return
        payload = self._read_json(max_bytes=4096)
        token = str(payload.get("token") or "").strip()
        if not token:
            raise ValueError("链接缺少令牌")
        # 容量检查放在兑换之前：满员时拒绝但不吞掉玩家的一次性链接。
        capacity = self._cw_capacity()
        if registry.session_count() >= capacity:
            self._send_json(
                {"error": "当前网页建卡人数已满，请稍后再试"}, status=429
            )
            return
        origin = registry.consume_link(token)
        draft = self._run_async(
            self.panel.database.card_draft_for_private(origin)
        )
        if not draft:
            raise ValueError("角色卡草稿不存在或已过期，请重新发送 /团 网页建卡")
        session_token = registry.issue_session(origin)
        self._run_async(
            self.panel.database.set_card_web_state(
                origin,
                {
                    WEB_ACTIVE_KEY: time.time() + WEB_ACTIVE_WINDOW_SECONDS,
                },
            )
        )
        self._send_json(
            {
                "session_token": session_token,
                "expires_in": SESSION_TTL_SECONDS,
            }
        )

    def _cw_capacity(self) -> int:
        try:
            drafts = int(
                self._run_async(self.panel.database.count_active_card_drafts())
                or 0
            )
        except Exception:
            self.panel.logger.exception("321开团网页建卡活跃草稿统计失败")
            drafts = CAPACITY_FLOOR
        return min(
            CAPACITY_HARD_LIMIT,
            max(CAPACITY_FLOOR, drafts + CAPACITY_MARGIN),
        )

    def _cw_touch(self, origin: str) -> None:
        self._run_async(
            self.panel.database.set_card_web_state(
                origin,
                {WEB_ACTIVE_KEY: time.time() + WEB_ACTIVE_WINDOW_SECONDS},
            )
        )

    # ---- 数据端点 ----
    def _cw_state(self, origin: str) -> None:
        draft = self._run_async(
            self.panel.database.card_draft_for_private(origin)
        )
        if not draft:
            self._send_json(
                {"error": "角色卡草稿不存在或已过期", "gone": True}, status=404
            )
            return
        self._send_json(_state_payload(origin, draft))

    def _cw_preview(self, origin: str) -> None:
        draft = self._run_async(
            self.panel.database.card_draft_for_private(origin)
        )
        if not draft:
            self._send_json(
                {"error": "角色卡草稿不存在或已过期", "gone": True}, status=404
            )
            return
        self._send_json({"rows": _preview_rows(draft)})

    def _cw_fill(self, origin: str) -> None:
        payload = self._read_json(max_bytes=65536)
        value = str(payload.get("value") or "")
        if not value.strip():
            raise ValueError("请先填写内容")
        result = self._run_async(
            self.panel.database.fill_card_draft(
                origin,
                value,
                source_event_id=f"cw:{secrets.token_hex(8)}",
            )
        )
        if result.get("duplicate"):
            draft = self._run_async(
                self.panel.database.card_draft_for_private(origin)
            )
            self._send_json(_state_payload(origin, draft or result))
            return
        self._cw_touch(origin)
        self._send_json(_state_payload(origin, result))

    def _cw_ai(self, origin: str) -> None:
        payload = self._read_json(max_bytes=65536)
        mode = str(payload.get("mode") or "").strip()
        if mode not in _MODES:
            raise ValueError("mode 必须是 random 或 expand")
        draft = self._run_async(
            self.panel.database.card_draft_for_private(origin)
        )
        if not draft:
            self._send_json(
                {"error": "角色卡草稿不存在或已过期", "gone": True}, status=404
            )
            return
        field = _current_field(draft)
        if field is None:
            raise CardAIError("角色卡必填资料已填写完成，无需 AI 生成。")
        if field.kind == "synthetic":
            raise CardAIError(
                f"当前步骤是「{field.label}」，需要玩家亲自选择建卡方式，"
                "不适用随机/补全。"
            )
        if not field.user_fillable or field.auto_filled:
            raise CardAIError(
                f"当前字段「{field.label}」由系统代填，无法使用 AI 生成。"
            )
        field_type = field.field_type
        template = draft.get("template")
        template = template if isinstance(template, Mapping) else {}
        fields = draft.get("fields")
        fields = fields if isinstance(fields, Mapping) else {}

        # 选项类字段：随机走本地随机函数，不调用大模型、不消耗每日配额；
        # 补全对选择题无意义，直接拒绝。
        if field_type in _OPTION_FIELD_TYPES:
            if mode == "expand":
                raise CardAIError(
                    f"「{field.label}」是选择题，不需要补全；可点「随机」或直接选择。"
                )
            value = _random_pick_options(
                template,
                field.definition,
                fields,
                label=field.label,
                multi=(field_type == "multi_select"),
            )
            self._cw_touch(origin)
            self._send_json(
                {"value": value, "field_label": field.label, "filled": False}
            )
            return

        # 文本类字段：随机/补全走 LLM，受每日配额与全局并发双重限制。
        if field_type not in _TEXT_FIELD_TYPES:
            raise CardAIError(
                f"当前字段「{field.label}」不支持自动生成，请手动填写。"
            )
        composer = getattr(self.panel, "card_ai", None)
        if composer is None:
            raise CardAIError("AI 设定助手未启用：面板未接入语言模型。")
        field_key = str(field.step_key or "")
        if quota_left_for_field(fields, field_key, mode) <= 0:
            raise CardAIError(
                f"「{field.label}」今日的{'随机' if mode == 'random' else '补全'}次数已用完，"
                "明天再来，或直接手动修改内容。"
            )
        user_draft = str(payload.get("draft") or "").strip()
        if mode == "expand" and not user_draft:
            raise ValueError("补全需要先填写你的初始设定")
        # 配额先扣后生成：生成失败会在下方退回，避免同秒并发绕过每日限制。
        quota = consume_quota_for_field(fields, field_key, mode)
        self._run_async(
            self.panel.database.set_card_web_state(origin, {AI_QUOTA_KEY: quota})
        )
        semaphore = getattr(self._cw_registry(), "ai_semaphore", None)
        if semaphore is None or not semaphore.acquire(blocking=False):
            # 退回本次预扣的配额。
            self._run_async(
                self.panel.database.set_card_web_state(
                    origin, {AI_QUOTA_KEY: _unconsume(fields, field_key, mode)}
                )
            )
            self._send_json(
                {"error": "AI 生成排队已满（全局 3 路），请几秒后重试"}, status=429
            )
            return
        try:
            value, field_label, generated = self._run_async(
                composer.compose_field_value(
                    origin,
                    draft,
                    mode=mode,
                    user_draft=user_draft,
                ),
                timeout=150.0,
            )
        except Exception:
            # 生成失败退回本次预扣的配额（模型侧失败不应消耗每日次数）。
            self._run_async(
                self.panel.database.set_card_web_state(
                    origin,
                    {AI_QUOTA_KEY: _unconsume(fields, field_key, mode)},
                )
            )
            raise
        finally:
            if semaphore is not None:
                semaphore.release()
        self._cw_touch(origin)
        # 生成值仅回填给玩家审核，不落库、不推进游标；玩家确认后点
        # 「提交并下一项」才走 fill_card_draft 正式填入。
        self._send_json(
            {
                "value": value,
                "field_label": field_label,
                "generated": generated,
                "filled": False,
            }
        )

    def _cw_previous(self, origin: str) -> None:
        previous = self._run_async(
            self.panel.database.previous_card_step(origin)
        )
        self._cw_touch(origin)
        self._send_json(_state_payload(origin, previous))

    def _cw_modify(self, origin: str) -> None:
        payload = self._read_json(max_bytes=4096)
        reference = str(payload.get("field") or "").strip()
        if not reference:
            raise ValueError("请指定要修改的字段名称")
        modified = self._run_async(
            self.panel.database.modify_card_field(origin, reference)
        )
        self._cw_touch(origin)
        self._send_json(_state_payload(origin, modified))

    def _cw_confirm(self, origin: str) -> None:
        result = self._run_async(self.panel.database.confirm_card_draft(origin))
        self._revoke(origin)
        self._send_json(
            {
                "confirmed": True,
                "needs_revision": bool(result.get("needs_revision")),
                "character_name": str(result.get("character_name") or ""),
                "message": _confirm_message(result),
            }
        )

    def _cw_cancel(self, origin: str) -> None:
        self._run_async(self.panel.database.cancel_card_draft(origin))
        self._revoke(origin)
        self._send_json(
            {
                "cancelled": True,
                "message": "草稿已取消，席位保留。重新开始请发送 /团 重新建卡。",
            }
        )

    def _cw_restart(self, origin: str) -> None:
        restarted = self._run_async(
            self.panel.database.restart_card_draft(origin)
        )
        self._cw_touch(origin)
        self._send_json(_state_payload(origin, restarted))

    # ---- 网页审核（/cw/review） ----
    def _cw_review_principal(self) -> dict[str, Any]:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.strip().lower() != "bearer":
            return {}
        registry = self._cw_registry()
        if registry is None:
            return {}
        return registry.resolve_review_session(token.strip())

    def _cw_review_exchange(self) -> None:
        registry = self._cw_registry()
        if registry is None:
            self._send_json(
                {"error": "网页审核未启用：面板未就绪，请稍后重试或联系管理员"},
                status=503,
            )
            return
        ip = self._client_ip()
        if not registry.exchange_allowed(ip):
            self._send_json({"error": "尝试过于频繁，请一分钟后再试"}, status=429)
            return
        payload = self._read_json(max_bytes=4096)
        token = str(payload.get("token") or "").strip()
        if not token:
            raise ValueError("链接缺少令牌")
        principal = registry.consume_review_link(token)
        session_token = registry.issue_review_session(
            principal["admin_id"],
            bypass_host=principal["bypass_host"],
        )
        self._send_json({"token": session_token})

    def _cw_review_state(self) -> None:
        principal = self._cw_review_principal()
        if not principal:
            self._send_json(
                {"error": "会话已失效，请在 QQ 私聊重新发送 /团 审核"},
                status=401,
            )
            return
        state = self._run_async(
            _build_review_state(self.panel.database, principal)
        )
        self._send_json(state)

    def _cw_review_decide(self) -> None:
        principal = self._cw_review_principal()
        if not principal:
            self._send_json(
                {"error": "会话已失效，请在 QQ 私聊重新发送 /团 审核"},
                status=401,
            )
            return
        payload = self._read_json(max_bytes=8192)
        result = self._run_async(
            _decide_review(
                self.panel.database,
                principal,
                str(payload.get("session_id") or ""),
                str(payload.get("participant_id") or ""),
                bool(payload.get("approved")),
                str(payload.get("note") or "").strip()[:500],
            )
        )
        self._send_json(result)

    def _revoke(self, origin: str) -> None:
        registry = self._cw_registry()
        if registry is not None:
            registry.revoke_origin(origin)


def _unconsume(
    fields: Mapping[str, Any] | None, field_key: str, mode: str
) -> dict[str, Any]:
    """回退一次预扣的配额（生成排队失败时使用）。"""

    quota = _quota_mapping(fields)
    today = _day_key()
    day = dict(quota.get(today)) if isinstance(quota.get(today), Mapping) else {}
    field_quota = dict(day.get(str(field_key))) if isinstance(
        day.get(str(field_key)), Mapping
    ) else {}
    field_quota[mode] = max(0, int(field_quota.get(mode, 0) or 0) - 1)
    day[str(field_key)] = field_quota
    quota[today] = day
    return quota


def _random_pick_options(
    template: Mapping[str, Any],
    definition: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    label: str,
    multi: bool,
) -> str:
    """选择题的本地随机：从候选中随机挑一项（多选则按 min/max 随机挑若干项）。

    不调用大模型、不消耗每日配额，返回以顿号连接的候选外显名称，供前端回填。
    """

    try:
        options = preset_options(template, definition, fields)
    except ValueError as exc:
        raise CardAIError(str(exc)) from exc
    if not options:
        raise CardAIError(
            f"字段「{label}」当前没有可用候选，无法随机选择；请直接手动选择。"
        )
    if multi:
        minimum = max(0, int(definition.get("min_choices", 0) or 0))
        maximum = max(minimum, int(definition.get("max_choices", 100) or 100))
        maximum = min(maximum, len(options))
        minimum = min(minimum, maximum)
        if minimum < 1:
            minimum = 1
        count = random.randint(minimum, maximum)
        picked = random.sample(options, count)
    else:
        picked = [random.choice(options)]
    return "、".join(
        str(item.get("label") or item.get("value") or "") for item in picked
    )


def _review_reference_of(participant: Mapping[str, Any]) -> str:
    """审核号（与群内 /团 审核 展示一致）：R-XXXXXXXX。"""

    raw = str(participant.get("id") or "").split("_", 1)[-1]
    token = re.sub(r"[^a-zA-Z0-9]", "", raw).upper()
    return f"R-{(token or 'UNKNOWN')[:8]}"


def _pending_review_cards_of(roster: Any) -> list[Mapping[str, Any]]:
    from .presentation.reviews import _pending_review_cards

    return list(_pending_review_cards(roster))


async def _review_hosted_pending(
    database: Any, principal: Mapping[str, Any]
) -> list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]]:
    """返回审核人可主持且有待审核卡的副本及其待审列表。

    host 角色是必要条件；签发链接时验证过的面板管理员可凭
    ``bypass_host`` 放行（与群内 is_host 判定保持一致）。
    """

    admin_id = str(principal.get("admin_id") or "")
    bypass = bool(principal.get("bypass_host"))
    hosted: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for session in await database.list_sessions():
        if str(session.get("state") or "") == "finished":
            continue
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        if not bypass:
            roles = await database.permission_roles(session_id, admin_id)
            if "host" not in roles:
                continue
        roster = await database.list_roster(session_id)
        pending = _pending_review_cards_of(roster)
        if pending:
            hosted.append((session, pending))
    return hosted


async def _build_review_state(
    database: Any, principal: Mapping[str, Any]
) -> dict[str, Any]:
    from .presentation.story import format_review_card

    hosted = await _review_hosted_pending(database, principal)
    items: list[dict[str, Any]] = []
    for session, pending in hosted:
        try:
            instance = await database.get_instance_config(
                str(session.get("id") or "")
            )
            instance = instance if isinstance(instance, Mapping) else {}
            template = instance.get("character_card_template")
            template = template if isinstance(template, Mapping) else {}
            world = instance.get("world_snapshot")
            world = world if isinstance(world, Mapping) else {}
        except Exception:
            template, world = {}, {}
        session_name = str(
            session.get("instance_name") or session.get("instance_slug") or ""
        )
        for ordinal, target in enumerate(pending, 1):
            try:
                detail = str(
                    format_review_card(target, template, world)
                ).strip()
            except Exception:
                detail = (
                    "角色卡详情渲染失败；请使用群内 /团 审核 <序号> 查看。"
                )
            items.append(
                {
                    "session_id": str(session.get("id") or ""),
                    "session_name": session_name,
                    "ordinal": ordinal,
                    "review_ref": _review_reference_of(target),
                    "participant_id": str(target.get("id") or ""),
                    "character_name": str(
                        target.get("character_name")
                        or target.get("display_name")
                        or ""
                    ),
                    "character_code": str(
                        target.get("character_code") or ""
                    ),
                    "player_name": str(
                        target.get("display_name") or ""
                    ),
                    "card_version_no": int(
                        target.get("card_version_no") or 1
                    ),
                    "detail_text": detail,
                }
            )
    return {"items": items, "count": len(items)}


async def _decide_review(
    database: Any,
    principal: Mapping[str, Any],
    session_id: str,
    participant_id: str,
    approved: bool,
    note: str,
) -> dict[str, Any]:
    from .operations import operation_key

    admin_id = str(principal.get("admin_id") or "")
    bypass = bool(principal.get("bypass_host"))
    session_id = str(session_id or "")
    try:
        if not bypass:
            roles = await database.permission_roles(session_id, admin_id)
            if "host" not in roles:
                return {
                    "ok": False,
                    "error": "你不是该副本的主持人，无法审批。",
                }
        session = await database.get_session(session_id)
        roster = await database.list_roster(session_id)
        pending = _pending_review_cards_of(roster)
        target = next(
            (
                item
                for item in pending
                if str(item.get("id") or "") == participant_id
            ),
            None,
        )
        if target is None:
            return {
                "ok": False,
                "error": "该角色卡不在待审核列表中（可能已被审批或已失效）。",
            }
        expected_version = int(target.get("card_version_no") or 0)
        review_key = operation_key(
            session_id,
            "card.review",
            turn_no=int(session.get("turn_no") or 0),
            actor_id=admin_id,
            source_id="web-review",
            payload={
                "participant": participant_id,
                "version": expected_version,
                "action": "approve" if approved else "reject",
                "note": note,
            },
        )
        participant = await database.review_character_card(
            session_id,
            participant_id,
            approved,
            admin_id,
            note,
            expected_version,
            review_key,
        )
        return {
            "ok": True,
            "character_name": str(
                participant.get("character_name") or ""
            ),
            "decision": "已通过" if approved else "已驳回",
        }
    except DatabaseNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except (ValueError, PermissionError) as exc:
        return {"ok": False, "error": str(exc)}


def _confirm_message(result: Mapping[str, Any]) -> str:
    if result.get("needs_revision"):
        return "部分依赖字段需要重新选择，请回 QQ 私聊发送 /团 当前步骤 查看详情。"
    name = str(result.get("character_name") or "").strip()
    if not name:
        return "角色卡缺少角色名，请回 QQ 私聊修改后再确认。"
    approved = bool(result.get("auto_approved"))
    return (
        f"「{name}」已保存。"
        + ("角色卡已自动通过，可回群发送 /团 准备。" if approved else
           "角色卡已提交主持人审核：主持人在团局群发送 /团 审核 即可查看并审批，通过后回群发送 /团 准备。")
    )


def _current_field(draft: Mapping[str, Any]) -> Any:
    template = draft.get("template")
    template = template if isinstance(template, Mapping) else {}
    fields = draft.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    step = int(draft.get("current_step", draft.get("draft_step", 0)) or 0)
    allow_stages = (CARD_STAGE_A,) if staged_creation(template) else None
    return resolve_current_wizard_step(
        template,
        fields,
        step,
        allow_stages=allow_stages,
    )


def _field_payload(draft: Mapping[str, Any]) -> dict[str, Any] | None:
    """当前字段的玩家可见 JSON；完成/合成步骤返回 None 或受限说明。"""

    wizard = _current_field(draft)
    if wizard is None:
        return None
    if wizard.kind == "synthetic":
        return {
            "key": wizard.step_key,
            "label": wizard.label,
            "type": "select",
            "synthetic": True,
            "required": True,
            "description": "请选择建卡方式（AI 不代作决定）。",
            "max_chars": 0,
            "value": None,
            "options": _options_payload(draft, wizard.definition),
            "quota": {"random": 0, "expand": 0},
        }
    template = draft.get("template")
    template = template if isinstance(template, Mapping) else {}
    fields = draft.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    definition = wizard.definition
    field_type = wizard.field_type
    options: list[dict[str, Any]] = []
    if field_type in _OPTION_FIELD_TYPES:
        options = _options_payload(draft, definition, fields, template)
    if field_type in _OPTION_FIELD_TYPES:
        # 选项类字段：随机走本地随机函数、不限次数（-1 表示不限），无补全。
        quota = {"random": -1, "expand": 0}
    elif field_type in _TEXT_FIELD_TYPES:
        quota = {
            "random": quota_left_for_field(fields, wizard.step_key, "random"),
            "expand": quota_left_for_field(fields, wizard.step_key, "expand"),
        }
    else:
        quota = {"random": 0, "expand": 0}
    payload = {
        "key": wizard.step_key,
        "label": wizard.label,
        "type": field_type,
        "synthetic": False,
        "required": bool(wizard.required),
        "description": str(definition.get("description") or "").strip(),
        "max_chars": int(definition.get("max_chars", 0) or 0),
        "min_choices": int(definition.get("min_choices", 0) or 0) or None,
        "max_choices": int(definition.get("max_choices", 0) or 0) or None,
        "value": None,
        "options": options,
        "quota": quota,
    }
    if field_type == "integer":
        payload["minimum"] = int(definition.get("minimum", -100) or 0)
        payload["maximum"] = int(definition.get("maximum", 100) or 0)
    return payload


def _options_payload(
    draft: Mapping[str, Any],
    definition: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
    template: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    template = template if isinstance(template, Mapping) else {}
    fields = fields if isinstance(fields, Mapping) else {}
    try:
        options = preset_options(template, definition, fields)
    except ValueError:
        options = []
    return [
        {
            "ordinal": index,
            "label": str(option.get("label") or option.get("value") or ""),
            "description": str(option.get("description") or "").strip(),
        }
        for index, option in enumerate(options, 1)
    ]


def _preview_rows(draft: Mapping[str, Any]) -> list[dict[str, str]]:
    template = draft.get("template")
    template = template if isinstance(template, Mapping) else {}
    fields = draft.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    from .card_ai import _character_context

    rows = []
    for item in template.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "")
        if not key or key.startswith("_"):
            continue
        value = fields.get(key)
        if value in (None, ""):
            continue
        label = str(item.get("label") or key)
        if isinstance(value, list):
            text = "、".join(str(part) for part in value)
        else:
            text = str(value)
        rows.append({"label": label, "value": text[:400]})
    return rows


def _state_payload(
    origin: str, draft: Mapping[str, Any]
) -> dict[str, Any]:
    del origin
    template = draft.get("template")
    template = template if isinstance(template, Mapping) else {}
    fields = draft.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    world = draft.get("world")
    world = world if isinstance(world, Mapping) else {}
    # D1：进度口径与向导实际提问一致——分阶段世界开演前只问 A 组字段，
    # B/C 组留给剧情补充；不分阶段的世界统计全部字段。
    staged = staged_creation(template)
    fillable_keys: set[str] = set()
    deferred = 0
    for item in template.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "")
        if not key or key.startswith("_"):
            continue
        if str(item.get("type") or "") == "derived":
            continue
        if staged and str(item.get("stage") or CARD_STAGE_A) != CARD_STAGE_A:
            deferred += 1
            continue
        fillable_keys.add(key)
    total = len(fillable_keys)
    filled = sum(
        1
        for key, value in fields.items()
        if key in fillable_keys and value not in (None, "")
    )
    return {
        "complete": _current_field(draft) is None,
        "suspended": bool(draft.get("suspended")),
        "needs_revision": bool(draft.get("needs_revision")),
        "content_update_notice": str(draft.get("content_update_notice") or ""),
        "world_name": str(
            world.get("name") or world.get("display_name") or template.get("world_name") or ""
        ),
        "session_name": str(draft.get("instance_name") or draft.get("name") or ""),
        "character_name": str(draft.get("name") or ""),
        "progress": {"filled": filled, "total": total, "deferred": deferred},
        "field": _field_payload(draft),
        "preview": _preview_rows(draft),
    }


_CARD_WIZARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>321开团 · 网页建卡</title>
<style>
:root{color-scheme:light;--bg:#f3f6f8;--card:#fff;--ink:#14202c;--muted:#6c7d8c;--line:#d6e0e7;--accent:#b66b16;--accent-soft:#f7ecdf;--danger:#bd3e34;--ok:#2e7d5b}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:16px 14px 40px}
.top{margin:6px 0 14px}
.brand{display:flex;gap:10px;align-items:center}
.mark{width:42px;height:42px;display:grid;place-items:center;border:1px solid #e7cda8;border-radius:14px;background:var(--accent-soft);color:var(--accent);font-size:22px}
.eyebrow{color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.14em}
h1{font-size:19px;margin:2px 0 0}
.meta{color:var(--muted);font-size:13px;margin-top:8px;line-height:1.6}
.bar{height:8px;border-radius:6px;background:#e2e9ee;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:var(--accent);transition:width .3s}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:14px;box-shadow:0 12px 40px rgba(31,53,71,.08)}
.card h2{font-size:16px;margin:0 0 6px}
.desc{color:var(--muted);font-size:13px;line-height:1.7;margin:0 0 12px;white-space:pre-wrap}
input[type=text],input[type=number],textarea{width:100%;padding:12px;border:1px solid var(--line);border-radius:12px;background:#f8fafb;color:var(--ink);font-size:15px;font-family:inherit}
textarea{min-height:120px;resize:vertical}
.counter{color:var(--muted);font-size:12px;text-align:right;margin-top:4px}
.opt{display:flex;gap:10px;align-items:flex-start;padding:11px 12px;border:1px solid var(--line);border-radius:12px;margin-bottom:8px;cursor:pointer;background:#fbfdfe}
.opt:hover{border-color:#c98a40}
.opt input{width:auto;margin-top:3px}
.opt .name{font-weight:700;font-size:14px}
.opt .brief{color:var(--muted);font-size:12px;line-height:1.6;margin-top:2px;white-space:pre-wrap}
.ai{display:flex;gap:8px;margin:4px 0 12px}
.ai button{flex:1;padding:10px;border:1px dashed #d9b98c;border-radius:12px;background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:700;cursor:pointer}
.ai button:disabled{opacity:.5;cursor:not-allowed}
.ai .left{display:block;font-size:11px;font-weight:400;color:var(--muted)}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.actions button{flex:1;min-width:96px;padding:12px;border:0;border-radius:12px;font-size:14px;font-weight:800;cursor:pointer}
.primary{background:var(--accent);color:#fff}
.ghost{background:#eef2f5;color:var(--ink)}
.danger{background:#fbeae7;color:var(--danger)}
.msg{min-height:20px;font-size:13px;line-height:1.6;margin-top:10px}
.msg.err{color:var(--danger)}
.msg.ok{color:var(--ok)}
.center{text-align:center}
.hidden{display:none}
.linkish{color:var(--accent);word-break:break-all}
ul.rows{list-style:none;margin:0;padding:0}
ul.rows li{display:flex;gap:8px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13px;line-height:1.6}
ul.rows li b{flex:0 0 7.5em;color:var(--muted);font-weight:600}
ul.rows li span{flex:1;white-space:pre-wrap;word-break:break-word}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="mark">♜</div>
      <div><div class="eyebrow">AI TAVERN</div><h1>网页建卡</h1></div></div>
    <div class="meta" id="meta">正在载入…</div>
    <div class="bar"><i id="bar" style="width:0%"></i></div>
  </div>
  <div id="app"><div class="card center">正在载入…</div></div>
  <div class="msg" id="msg"></div>
</div>
<script>
const $=(s)=>document.querySelector(s);
const msg=$("#msg");
function note(t,cls){msg.className="msg"+(cls?" "+cls:"");msg.textContent=t||"";}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}
function token(){return sessionStorage.getItem("cw_token")||"";}
async function api(path,opts={}){
  const resp=await fetch(path,{...opts,headers:{"Content-Type":"application/json","Authorization":"Bearer "+token(),...(opts.headers||{})}});
  const data=await resp.json().catch(()=>({}));
  if(resp.status===401){expired();throw new Error(data.error||"会话已失效");}
  if(!resp.ok)throw new Error(data.error||("请求失败 HTTP "+resp.status));
  return data;
}
function expired(){
  sessionStorage.removeItem("cw_token");
  $("#app").innerHTML='<div class="card center"><h2>会话已失效</h2><p class="desc">网页建卡链接是一次性的，或已超过 30 分钟未操作。<br>请在 QQ 私聊机器人重新发送 <b>/团 网页建卡</b> 获取新链接。</p></div>';
}
async function exchange(linkToken){
  const resp=await fetch("/cw/api/exchange",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:linkToken})});
  const data=await resp.json().catch(()=>({}));
  if(!resp.ok)throw new Error(data.error||"链接兑换失败");
  sessionStorage.setItem("cw_token",data.session_token);
  history.replaceState(null,"","/cw");
}
function remainingText(s){
  if(s==null)return"";
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h>0?`剩余约 ${h} 小时 ${m} 分钟`:`剩余约 ${m} 分钟`;
}
function fieldHtml(f){
  let body="";
  if(f.type==="text"||f.type==="textarea"){
    const tag=f.type==="textarea"?"textarea":"input";
    body=`<${tag} id="v" ${f.type==="text"?'type="text"':""} maxlength="${f.max_chars||4000}" placeholder="请输入${esc(f.label)}">${f.type==="textarea"?"</textarea>":""}`+
      `<div class="counter"><span id="cnt"></span> / ${f.max_chars||"—"} 字</div>`;
  }else if(f.type==="integer"){
    body=`<input id="v" type="number" min="${f.minimum??-100}" max="${f.maximum??100}" placeholder="填写数值">`;
  }else{
    const kind=f.type==="multi_select"?"checkbox":"radio";
    body=(f.options||[]).map(o=>`<label class="opt"><input type="${kind}" name="v" value="${esc(o.label)}"><span><span class="name">${o.ordinal}. ${esc(o.label)}</span>${o.description?`<span class="brief">${esc(o.description)}</span>`:""}</span></label>`).join("")||'<p class="desc">当前没有可选项。</p>';
  }
  const quota=f.quota||{};
  const isOption=["select","preset_select","multi_select"].includes(f.type);
  const isText=f.type==="text"||f.type==="textarea";
  let ai="";
  if(!f.synthetic){
    if(isOption){
      ai=`<div class="ai"><button id="ai-r">🎲 随机选一项</button></div>`;
    }else if(isText){
      ai=`<div class="ai">
    <button id="ai-r" ${quota.random<=0?"disabled":""}>🎲 随机<span class="left">今日剩余 ${quota.random??0} 次</span></button>
    <button id="ai-e" ${quota.expand<=0?"disabled":""}>✍️ 补全<span class="left">今日剩余 ${quota.expand??0} 次</span></button>
  </div>`;
    }
  }
  return `<div class="card"><h2>${esc(f.label)}${f.required?" *":""}</h2>
    ${f.description?`<p class="desc">${esc(f.description)}</p>`:""}${ai}${body}
    ${f.type==="multi_select"&&f.min_choices?`<p class="desc">需选择 ${f.min_choices}${f.max_choices&&f.max_choices!==f.min_choices?"—"+f.max_choices:""} 项。</p>`:""}
    <div class="actions" style="margin-top:12px"><button class="ghost" id="prev">上一项</button><button class="primary" id="submit">提交并下一项</button></div></div>`;
}
function render(d){
  window.__state=d;
  $("#meta").innerHTML=`${esc(d.world_name||"")} · ${esc(d.session_name||"")} · 已填 ${d.progress.filled}/${d.progress.total}${d.progress.deferred?`（另有 ${d.progress.deferred} 项剧情中补充）`:""}${d.remaining_seconds!=null?" · "+remainingText(d.remaining_seconds):""}`;
  $("#bar").style.width=(d.progress.total?Math.round(100*d.progress.filled/Math.max(1,d.progress.total)):0)+"%";
  if(d.suspended){$("#app").innerHTML='<div class="card center"><h2>建卡已暂停</h2><p class="desc">副本已关闭，系统保留你的建卡资料。</p></div>';return;}
  if(d.needs_revision){note(d.content_update_notice||"世界内容已更新，部分选择需要重新确认。","err");}
  if(d.complete){
    $("#app").innerHTML=`<div class="card center"><h2>角色卡已填写完成 🎉</h2>
      <p class="desc">请检查预览，确认无误后提交审核；也可以回到某一项继续修改。</p>
      <div class="actions"><button class="primary" onclick="doConfirm()">确认建卡</button><button class="ghost" onclick="load()">刷新</button></div></div>`;
    return;
  }
  const f=d.field;
  $("#app").innerHTML=fieldHtml(f);
  if(f.type==="text"||f.type==="textarea"){
    const v=$("#v"),c=$("#cnt");
    const upd=()=>{c&&(c.textContent=[...v.value].length);};
    v.addEventListener("input",upd);upd();
  }
  const sub=$("#submit");
  if(sub)sub.addEventListener("click",doSubmit);
  const prev=$("#prev");
  if(prev)prev.addEventListener("click",doPrev);
  const r=$("#ai-r"),e=$("#ai-e");
  if(r)r.addEventListener("click",()=>doAi("random"));
  if(e)e.addEventListener("click",()=>doAi("expand"));
}
async function load(){
  try{render(await api("/cw/api/state"));}
  catch(e){if(e.message)note(e.message,"err");}
}
async function doSubmit(){
  const f=(window.__state||{}).field||{};
  let value="";
  if(f.type==="text"||f.type==="textarea"||f.type==="integer"){value=$("#v").value.trim();}
  else{
    value=[...document.querySelectorAll('input[name="v"]:checked')].map(i=>i.value).join("，");
  }
  if(!value){note("请先填写内容","err");return;}
  try{
    const d=await api("/cw/api/fill",{method:"POST",body:JSON.stringify({value})});
    window.__state=d;note("已保存。","ok");render(d);window.scrollTo(0,0);
  }catch(e){note(e.message,"err");}
}
async function doAi(mode,draftOverride){
  if(mode==="expand"&&draftOverride==null){
    showExpand();
    return;
  }
  const btn=$(mode==="random"?"#ai-r":"#ai-e");
  const draft=mode==="expand"?(draftOverride||""):"";
  if(btn)btn.disabled=true;
  note(mode==="random"?"正在随机选择…":"AI 正在生成，通常需要十几秒…");
  try{
    const d=await api("/cw/api/ai",{method:"POST",body:JSON.stringify({mode,draft})});
    applyValue(d.value);
    note(`已${mode==="random"?"随机选出":"生成"}「${d.field_label||""}」，请确认后点「提交并下一项」。`,"ok");
  }catch(e){note(e.message,"err");}
  finally{if(btn)btn.disabled=false;}
}
function showExpand(){
  const ai=$(".ai");
  if(!ai||$("#expand-box"))return;
  const box=document.createElement("div");
  box.id="expand-box";
  box.style.marginBottom="12px";
  box.innerHTML='<textarea id="expand-draft" style="min-height:80px" placeholder="输入你的初始设定，AI 将保留你的创意进行扩写…" maxlength="2000"></textarea>'+
    '<div class="actions" style="margin-top:8px"><button class="primary" id="expand-ok">开始补全</button><button class="ghost" id="expand-cancel">取消</button></div>';
  ai.after(box);
  const ta=$("#expand-draft");
  if(ta)ta.focus();
  $("#expand-ok").addEventListener("click",()=>{
    const d=ta.value.trim();
    if(!d){note("请先输入你的初始设定","err");return;}
    box.remove();
    doAi("expand",d);
  });
  $("#expand-cancel").addEventListener("click",()=>box.remove());
}
function applyValue(value){
  const f=(window.__state||{}).field||{};
  if(f.type==="text"||f.type==="textarea"||f.type==="integer"){
    const v=$("#v");if(v){v.value=value||"";const c=$("#cnt");if(c)c.textContent=[...(value||"")].length;}
  }else{
    const parts=String(value||"").split(/[、,，]/).map(s=>s.trim()).filter(Boolean);
    document.querySelectorAll('input[name="v"]').forEach(i=>{i.checked=parts.includes(i.value);});
  }
}
window.doConfirm=async function(){
  if(!confirm("确认提交角色卡？提交后网页会话将失效。"))return;
  try{const d=await api("/cw/api/confirm",{method:"POST",body:"{}"});
    $("#app").innerHTML=`<div class="card center"><h2>${d.needs_revision?"需要修正":"已提交"} ✅</h2><p class="desc">${esc(d.message)}</p></div>`;
  }catch(e){note(e.message,"err");}
};
window.doCancel=async function(){
  if(!confirm("取消当前草稿？席位保留，可重新建卡。"))return;
  try{const d=await api("/cw/api/cancel",{method:"POST",body:"{}"});
    $("#app").innerHTML=`<div class="card center"><h2>已取消</h2><p class="desc">${esc(d.message)}</p></div>`;
  }catch(e){note(e.message,"err");}
};
window.doPrev=async function(){
  try{const d=await api("/cw/api/previous",{method:"POST",body:"{}"});window.__state=d;render(d);window.scrollTo(0,0);}
  catch(e){note(e.message,"err");}
};
window.doRestart=async function(){
  if(!confirm("重新开始建卡？旧草稿保留为历史，不会进入正式角色卡。"))return;
  try{const d=await api("/cw/api/restart",{method:"POST",body:"{}"});window.__state=d;render(d);}
  catch(e){note(e.message,"err");}
};
window.doModify=async function(name){
  try{const d=await api("/cw/api/modify",{method:"POST",body:JSON.stringify({field:name})});window.__state=d;render(d);window.scrollTo(0,0);}
  catch(e){note(e.message,"err");}
};
window.doPreview=function(){
  const rows=(window.__state&&window.__state.preview)||[];
  const html=rows.length?`<ul class="rows">${rows.map(r=>`<li><b>${esc(r.label)}</b><span>${esc(r.value)}</span></li>`).join("")}</ul>`:'<p class="desc">还没有已填写的内容。</p>';
  $("#app").innerHTML=`<div class="card"><h2>已填写资料</h2>${html}
    <div class="actions" style="margin-top:12px"><button class="ghost" onclick="load()">返回当前项</button></div></div>`;
};
(async function init(){
  const link=new URLSearchParams(location.search).get("token");
  try{
    if(link){await exchange(link);}
    if(!token()){expired();return;}
    await load();
  }catch(e){
    $("#app").innerHTML=`<div class="card center"><h2>无法打开</h2><p class="desc">${esc(e.message||"链接无效")}</p>
      <p class="desc">请在 QQ 私聊机器人重新发送 <b>/团 网页建卡</b>。</p></div>`;
  }
})();
</script>
</body>
</html>
"""

_CARD_REVIEW_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>321开团 · 网页审核</title>
<style>
:root{color-scheme:light;--bg:#f3f6f8;--card:#fff;--ink:#14202c;--muted:#6c7d8c;--line:#d6e0e7;--accent:#b66b16;--accent-soft:#f7ecdf;--danger:#bd3e34;--ok:#2e7d5b}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:680px;margin:0 auto;padding:16px 14px 48px}
.top{margin:6px 0 14px}
h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0;box-shadow:0 1px 3px rgba(20,32,44,.05)}
.item-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;cursor:pointer}
.item-head b{font-size:16px}
.tag{font-size:12px;color:var(--muted)}
button{font:inherit;border:none;border-radius:9px;padding:9px 16px;cursor:pointer}
button.primary{background:var(--accent);color:#fff}
button.ok{background:var(--ok);color:#fff}
button.danger{background:var(--danger);color:#fff}
button.ghost{background:var(--accent-soft);color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
.detail{display:none;margin-top:10px}
.detail.open{display:block}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.65;font-family:inherit;margin:0 0 10px}
textarea{width:100%;font:inherit;border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-height:56px;resize:vertical;background:#fff;color:var(--ink)}
.actions{display:flex;gap:10px;margin-top:10px}
.actions button{flex:1}
.done{color:var(--ok);font-weight:600}
.note-msg{font-size:13px;margin-top:8px;color:var(--muted)}
.center{text-align:center}
.empty{color:var(--muted);text-align:center;padding:24px 0}
#msg{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:9px 18px;border-radius:20px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;max-width:88vw}
#msg.show{opacity:.95}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><h1>321开团 · 网页审核</h1><div class="meta" id="meta">正在加载…</div></div>
  <div id="app"><div class="card empty">正在加载待审核列表…</div></div>
</div>
<div id="msg"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let __token="";
function note(t,kind){const m=$("#msg");m.textContent=t;m.className=kind==="err"?"show err":"show";m.style.background=kind==="err"?"#bd3e34":"#14202c";clearTimeout(m.__t);m.__t=setTimeout(()=>m.classList.remove("show"),3200);}
async function api(p,opt={}){const r=await fetch(p,{...opt,headers:{"Content-Type":"application/json","Authorization":"Bearer "+__token}});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||("HTTP "+r.status));return d;}
async function exchange(linkToken){const r=await fetch("/cw/api/review-exchange",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:linkToken})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||"兑换失败");__token=d.token;sessionStorage.setItem("tavern_review_token",__token);}
function decidedKey(it){return "decided:"+it.session_id+":"+it.participant_id+":"+it.card_version_no;}
function render(d){
  $("#meta").textContent="待审核 "+d.count+" 张 · 会话 30 分钟有效，可刷新页面继续";
  if(!d.count){$("#app").innerHTML='<div class="card empty">当前没有待审核的角色卡。</div>';return;}
  $("#app").innerHTML=d.items.map((it,i)=>`
  <div class="card" data-pid="${esc(it.participant_id)}" data-sid="${esc(it.session_id)}" data-ver="${it.card_version_no}">
    <div class="item-head" onclick="this.parentNode.querySelector('.detail').classList.toggle('open')">
      <b>${i+1}. ${esc(it.character_name||"未命名")}${it.character_code?"（"+esc(it.character_code)+"）":""}</b>
      <span class="tag">${esc(it.session_name)} · ${esc(it.review_ref)}</span>
    </div>
    <div class="tag" style="margin-top:2px">玩家：${esc(it.player_name||"未知")} · 版本 v${it.card_version_no} · 点击标题展开详情</div>
    <div class="detail"><pre>${esc(it.detail_text)}</pre>
      <textarea placeholder="备注（可选，驳回时建议填写原因）"></textarea>
      <div class="actions">
        <button class="ok" onclick="decide(this,true)">通过</button>
        <button class="danger" onclick="decide(this,false)">驳回</button>
      </div>
      <div class="note-msg result"></div>
    </div>
  </div>`).join("");
  d.items.forEach(it=>{if(sessionStorage.getItem(decidedKey(it))){const card=document.querySelector(`[data-pid="${it.participant_id}"]`);if(card){card.querySelectorAll("button").forEach(b=>b.disabled=true);card.querySelector(".result").innerHTML='<span class="done">✔ 已处理（'+esc(sessionStorage.getItem(decidedKey(it)))+'）</span>';}}});
}
async function load(){try{const d=await api("/cw/api/review-state");render(d);}catch(e){$("#app").innerHTML=`<div class="card center"><h2>无法加载</h2><p class="desc">${esc(e.message)}</p><p class="desc">请在 QQ 私聊机器人重新发送 <b>/团 审核</b>。</p></div>`;}}
window.decide=async function(btn,approved){
  const card=btn.closest(".card");
  const noteText=card.querySelector("textarea").value.trim();
  if(!approved&&!noteText){note("驳回建议填写原因备注","err");return;}
  if(!confirm(approved?"确认通过该角色卡？":"确认驳回该角色卡？"))return;
  btn.disabled=true;
  try{
    const d=await api("/cw/api/review-decide",{method:"POST",body:JSON.stringify({session_id:card.dataset.sid,participant_id:card.dataset.pid,approved,note:noteText})});
    if(!d.ok)throw new Error(d.error||"操作失败");
    sessionStorage.setItem(decidedKey({session_id:card.dataset.sid,participant_id:card.dataset.pid,card_version_no:card.dataset.ver}),d.decision);
    card.querySelectorAll("button").forEach(b=>b.disabled=true);
    card.querySelector(".result").innerHTML=`<span class="done">✔ 「${esc(d.character_name)}」${esc(d.decision)}</span>`;
    note("已"+d.decision+"「"+d.character_name+"」");
  }catch(e){note(e.message,"err");btn.disabled=false;}
};
(async function init(){
  const link=new URLSearchParams(location.search).get("token");
  __token=sessionStorage.getItem("tavern_review_token")||"";
  try{
    if(link){await exchange(link);}
    if(!__token){throw new Error("链接无效或已过期");}
    await load();
  }catch(e){
    $("#app").innerHTML=`<div class="card center"><h2>无法打开</h2><p class="desc">${esc(e.message||"链接无效")}</p>
      <p class="desc">请在 QQ 私聊机器人重新发送 <b>/团 审核</b> 获取新链接。</p></div>`;
  }
})();
</script>
</body>
</html>
"""
