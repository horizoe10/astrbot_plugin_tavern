from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
from collections import OrderedDict
from contextlib import closing
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)

from .config import TavernConfig
from .constants import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from .database import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from .database_support import validate_slug
from .events import EventBroker
from .lifecycle import normalize_time_rules
from .diagnostics import build_diagnostic_report
from .emergency import EmergencyService
from .errors import PolicyRejection, report_failure
from .dashboard import (
    dashboard_sessions as build_dashboard_sessions,
    session_dashboard as build_session_dashboard,
    session_timeline as build_session_timeline,
    session_timers as build_session_timers,
)
from .operations import recovery_summary
from .world_migration import compare_world_contracts
from .world_preflight import inspect_world_package
from .world_market import (
    clear_remote_cache,
    fetch_entry,
    fetch_remote_manifest,
    fetch_remote_package,
    scan_entries,
    search_entries,
)
from .world_import import world_edit_payload, world_import_payload
from .elemental import (
    parse as parse_elemental,
    resolve as resolve_elemental,
    table as elemental_table,
)
from .world_contract import RESOLUTION_MODES, WORLD_SCHEMA_VERSION
from .operation_engine import OPERATION_TYPES, PERSISTENCE_SCOPES
from .api.hooks import SUPPORTED_EVENTS as HOOK_EVENTS
from .api.registry import ExtensionRegistry
from .rule_runtime import RuleRuntime
from .entity_registry import EntityRegistry
from .backup_service import build_backup_archive
from .storage import (
    file_sha256,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)
from .platform_delivery import capability_matrix, send_text as deliver_text
from .chat_experience import normalize_chat_experience
from .review_notifications import notify_card_review
from .command_triggers import render_command_text


_BACKUP_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_BACKUP_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
# 世界包导入冲突的前缀标记：前端据此弹出「覆盖为新修订 / 另存副本」决策弹窗。
_WORLD_CONFLICT_PREFIX = "导入冲突"
_WORLD_SIMULATE_MAX_BYTES = 1_000_000
_WORLD_SIMULATE_MAX_DEPTH = 40


def _json_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _json_depth(obj: Any, limit: int = _WORLD_SIMULATE_MAX_DEPTH) -> bool:
    """粗略判断 JSON 嵌套深度是否超过 limit（防计算型 DoS）。"""

    def walk(value: Any, depth: int) -> bool:
        if depth > limit:
            return True
        if isinstance(value, dict):
            return any(walk(item, depth + 1) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(walk(item, depth + 1) for item in value)
        return False

    return walk(obj, 0)


def _content_version_key(value: Any) -> tuple[int, ...] | None:
    text = str(value or "").strip().lstrip("vV")
    match = re.fullmatch(r"\d+(?:\.\d+){0,3}", text)
    if not match:
        return None
    return tuple(int(part) for part in text.split("."))


def _safe_backup_member(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ValueError("ZIP 备份含非法文件路径")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("ZIP 备份含非法文件路径")
    return path


def _backup_checksums(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        checksum_info = archive.getinfo("checksum.sha256")
    except KeyError as exc:
        raise ValueError("ZIP 备份缺少 checksum.sha256") from exc
    if checksum_info.file_size > 16 * 1024 * 1024:
        raise ValueError("ZIP 备份校验清单过大")
    try:
        text = archive.read(checksum_info).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("ZIP 备份校验清单编码错误") from exc
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError("ZIP 备份校验清单格式错误")
        digest, name = parts[0].strip().lower(), parts[1].strip()
        _safe_backup_member(name)
        if not _BACKUP_HASH_PATTERN.fullmatch(digest) or name in checksums:
            raise ValueError("ZIP 备份校验清单格式错误")
        checksums[name] = digest
    return checksums


def _verify_backup_archive(archive: zipfile.ZipFile) -> dict[str, str]:
    infos = archive.infolist()
    names: set[str] = set()
    total_size = 0
    for info in infos:
        path = _safe_backup_member(info.filename.rstrip("/"))
        name = path.as_posix()
        if name in names:
            raise ValueError("ZIP 备份含重复文件名")
        names.add(name)
        if info.flag_bits & 0x1:
            raise ValueError("不支持加密 ZIP 备份")
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise ValueError("ZIP 备份不能包含符号链接")
        if not info.is_dir():
            total_size += int(info.file_size)
    if total_size > _MAX_BACKUP_UNCOMPRESSED:
        raise ValueError("ZIP 解压后的总大小不能超过 4 GiB")
    checksums = _backup_checksums(archive)
    payload_names = {
        info.filename
        for info in infos
        if not info.is_dir() and info.filename != "checksum.sha256"
    }
    if payload_names != set(checksums):
        raise ValueError("ZIP 备份文件与校验清单不一致")
    for info in infos:
        if info.is_dir() or info.filename == "checksum.sha256":
            continue
        digest = hashlib.sha256()
        with archive.open(info, "r") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != checksums[info.filename]:
            raise ValueError(f"ZIP 备份文件校验失败：{info.filename}")
    return checksums


def _stage_group_files(
    archive: zipfile.ZipFile,
    stage_dir: Path,
) -> list[tuple[Path, PurePosixPath]]:
    staged: list[tuple[Path, PurePosixPath]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = _safe_backup_member(info.filename)
        if not relative.parts or relative.parts[0] != "groups":
            continue
        target = stage_dir.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        staged.append((target, relative))
    return staged


def _collision_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(
            f"{path.stem}_{index:02d}{path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名独立存档过多")


def _deep_diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    """递归字段级差异：返回 [{path, kind: added|removed|changed, old, new}]。"""
    changes: list[dict[str, Any]] = []
    if type(left) is not type(right):
        changes.append({"path": path, "kind": "type_changed", "old": left, "new": right})
        return changes
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            sub = f"{path}.{key}" if path else key
            if key not in left:
                changes.append({"path": sub, "kind": "added", "old": None, "new": right[key]})
            elif key not in right:
                changes.append({"path": sub, "kind": "removed", "old": left[key], "new": None})
            else:
                changes.extend(_deep_diff(left[key], right[key], sub))
    elif isinstance(left, list) and isinstance(right, list):
        if left != right:
            changes.append({"path": path or "$", "kind": "changed", "old": left, "new": right})
    elif left != right:
        changes.append({"path": path or "$", "kind": "changed", "old": left, "new": right})
    return changes


def _with_token_context(
    usage: dict[str, Any],
    instance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A16：在 Token 用量摘要上附加上下文预算与最近裁剪时间（只读展示）。"""
    result = dict(usage) if isinstance(usage, dict) else {}
    result["context_budget"] = {}
    if instance and isinstance(instance.get("world_snapshot"), Mapping):
        rules = instance["world_snapshot"].get("rules") or {}
        if isinstance(rules, Mapping):
            result["context_budget"] = dict(rules.get("context_budget") or {})
    result["last_trim_at"] = ""
    return result


def _safe(fn: Any, default: Any) -> Any:
    """A23: 模块级逐字段容错助手（此前误定义在类内且缺 self，导致
    extensions/hook_events/meta_capabilities 抛 NameError）。"""
    try:
        return fn()
    except Exception:
        return default


def _extension_catalog(registry: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Return a JSON-safe extension catalog while isolating malformed items."""
    if registry is None:
        return {}, []
    errors: list[str] = []
    try:
        raw_catalog = registry.list()
    except Exception as exc:
        return {}, [f"扩展注册表读取失败：{type(exc).__name__}: {str(exc)[:160]}"]
    if not isinstance(raw_catalog, Mapping):
        return {}, ["扩展注册表返回了无效的数据结构"]

    catalog: dict[str, list[str]] = {}
    for raw_kind, raw_names in raw_catalog.items():
        try:
            kind = str(raw_kind).strip()
            if not kind:
                raise ValueError("扩展类型为空")
            if isinstance(raw_names, str):
                candidates = (raw_names,)
            elif isinstance(raw_names, Mapping):
                candidates = raw_names.keys()
            else:
                candidates = raw_names or ()
            names: list[str] = []
            iterator = iter(candidates)
            while True:
                try:
                    candidate = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    errors.append(
                        f"{kind} 扩展列表读取中断：{type(exc).__name__}: {str(exc)[:120]}"
                    )
                    break
                try:
                    name = str(candidate).strip()
                    if name:
                        names.append(name)
                except Exception as exc:
                    errors.append(
                        f"{kind} 中有一个扩展项无法序列化：{type(exc).__name__}"
                    )
            catalog[kind] = sorted(set(names))
        except Exception as exc:
            errors.append(
                f"一个扩展类型无法读取：{type(exc).__name__}: {str(exc)[:120]}"
            )
    return dict(sorted(catalog.items())), errors


def _hook_catalog(hooks: Any) -> tuple[dict[str, int], list[str]]:
    """Return JSON-safe subscription counts without trusting extension data."""
    if hooks is None:
        return {}, []
    try:
        raw_catalog = hooks.list_subscriptions()
    except Exception as exc:
        return {}, [f"事件订阅表读取失败：{type(exc).__name__}: {str(exc)[:160]}"]
    if not isinstance(raw_catalog, Mapping):
        return {}, ["事件订阅表返回了无效的数据结构"]

    catalog: dict[str, int] = {}
    errors: list[str] = []
    for raw_event, raw_count in raw_catalog.items():
        try:
            event = str(raw_event).strip()
            count = max(0, int(raw_count))
            if not event:
                raise ValueError("事件名为空")
            catalog[event] = count
        except Exception as exc:
            errors.append(
                f"一个事件订阅项无法读取：{type(exc).__name__}: {str(exc)[:120]}"
            )
    return dict(sorted(catalog.items())), errors


class TavernWebConsole:
    def __init__(
        self,
        *,
        context: Any,
        plugin_config: Any,
        database: TavernDatabase,
        broker: EventBroker,
        data_dir: Path,
        logger: Any,
        allow_group: Any,
        config_lock: Any,
        extensions: Any = None,
        hooks: Any = None,
        engine: Any = None,
    ) -> None:
        self.context = context
        self.plugin_config = plugin_config
        self.database = database
        self._tavern_engine = engine
        self.broker = broker
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.allow_group = allow_group
        self.config_lock = config_lock
        # Do not name this attribute ``extensions``: that is also the public
        # async route handler.  The old collision registered the registry
        # object as an HTTP handler and caused GET /extensions to return 500.
        self._extension_registry = extensions
        self.hooks = hooks
        # 0.11.1：按 (slug, revision) 缓存 RuleRuntime，避免每次
        # world_simulate 全量重建 EntityRegistry/CapabilityService/EventPipeline。
        # RuleRuntime 在 __init__ 后只读（resolve 不修改内部状态），可安全复用。
        self._rule_runtime_cache: "OrderedDict[tuple[str, int], RuleRuntime]" = (
            OrderedDict()
        )
        # v0.12.0：缓存时间戳表（TTL 失效用），与 _rule_runtime_cache 同键。
        self._rule_runtime_timestamps: dict[tuple[str, int], float] = {}
        self._register_routes()

    # v0.12.0（性能优化）：RuleRuntime 缓存补强——真 LRU（命中即置尾）、
    # 闲置 TTL（超时重建）、并在世界被改动/归档/恢复时按 slug 显式失效。
    _RUNTIME_CACHE_TTL_SECONDS = 600.0

    def _cached_rule_runtime(self, world: Mapping[str, Any]) -> RuleRuntime:
        slug = str(world.get("slug") or "").strip()
        revision = int(world.get("revision") or 0)
        if not slug:
            return RuleRuntime(world)
        key = (slug, revision)
        now = time.monotonic()
        cached = self._rule_runtime_cache.get(key)
        if cached is not None:
            created = self._rule_runtime_timestamps.get(key, 0.0)
            if now - created <= self._RUNTIME_CACHE_TTL_SECONDS:
                self._rule_runtime_cache.move_to_end(key)
                return cached
            self._rule_runtime_cache.pop(key, None)
            self._rule_runtime_timestamps.pop(key, None)
        cached = RuleRuntime(world)
        self._rule_runtime_cache[key] = cached
        self._rule_runtime_timestamps[key] = now
        if len(self._rule_runtime_cache) > 8:
            _, stale = self._rule_runtime_cache.popitem(last=False)
            self._rule_runtime_timestamps.pop(stale, None)
        return cached

    def _purge_rule_runtime(self, slug: str) -> None:
        """世界被编辑/归档/恢复后按 slug 失效对应缓存条目。"""
        slug = str(slug or "").strip()
        if not slug:
            return
        stale_keys = [
            key for key in list(self._rule_runtime_cache)
            if key[0] == slug
        ]
        for key in stale_keys:
            self._rule_runtime_cache.pop(key, None)
            self._rule_runtime_timestamps.pop(key, None)

    def _register(self, path: str, handler: Any, methods: list[str], desc: str) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/{path}",
            handler,
            methods,
            desc,
        )

    def _register_routes(self) -> None:
        routes = [
            ("overview", self.overview, ["GET"], "Tavern overview"),
            ("worlds", self.worlds, ["GET"], "List worlds"),
            ("worlds/preflight", self.world_preflight, ["POST"], "Inspect world package"),
            ("worlds/migration", self.world_migration, ["POST"], "Compare frozen world contract"),
            ("worlds/save", self.world_save, ["POST"], "Save world"),
            ("worlds/order", self.world_order, ["POST"], "Reorder world"),
            ("worlds/simulate", self.world_simulate, ["POST"], "Dry-run v5 rules"),
            ("worlds/archive", self.world_archive, ["POST"], "Archive world"),
            ("worlds/restore", self.world_restore, ["POST"], "Restore world"),
            ("characters", self.characters, ["GET"], "List characters"),
            (
                "characters/save",
                self.character_save,
                ["POST"],
                "Save character",
            ),
            (
                "characters/delete",
                self.character_delete,
                ["POST"],
                "Delete character",
            ),
            (
                "worlds/import",
                self.world_import,
                ["POST"],
                "Import world package JSON",
            ),
            (
                "characters/import",
                self.character_import,
                ["POST"],
                "Import resident NPCs/characters JSON",
            ),
            ("sessions", self.sessions, ["GET"], "List sessions"),
            (
                "sessions/detail",
                self.session_detail,
                ["GET"],
                "Session detail",
            ),
            ("sessions/recovery", self.session_recovery, ["GET"], "Inspect workflow recovery state"),
            ("sessions/diagnostics", self.session_diagnostics, ["GET"], "Export redacted diagnostics"),
            ("sessions/rescue", self.session_rescue, ["POST"], "Run precise workflow recovery"),
            ("sessions/card-revisions", self.session_card_revisions, ["GET", "POST"], "Manage character card revisions"),
            (
                "sessions/action",
                self.session_action,
                ["POST"],
                "Session action",
            ),
            (
                "sessions/state",
                self.session_state,
                ["POST"],
                "Edit session state",
            ),
            (
                "sessions/turn-order",
                self.session_turn_order,
                ["POST"],
                "Edit multiplayer turn order",
            ),
            (
                "sessions/time-rules",
                self.session_time_rules,
                ["POST"],
                "Edit instance timing rules",
            ),
            (
                "sessions/rules",
                self.session_rules,
                ["POST"],
                "Edit session rules and progress",
            ),
            (
                "sessions/npc",
                self.session_npc,
                ["POST"],
                "Create or edit a session NPC",
            ),
            (
                "sessions/timer",
                self.session_timer,
                ["POST"],
                "Control persistent timer",
            ),
            (
                "sessions/timer-policy",
                self.session_timer_policy,
                ["POST"],
                "Control countdown categories",
            ),
            (
                "sessions/token-quota",
                self.session_token_quota,
                ["POST"],
                "Control token quotas",
            ),
            (
                "sessions/card-review",
                self.session_card_review,
                ["POST"],
                "Review a character card",
            ),
            (
                "sessions/permission",
                self.session_permission,
                ["POST"],
                "Grant instance role",
            ),
            (
                "sessions/participant",
                self.session_participant,
                ["POST"],
                "Manage participant status",
            ),
            (
                "groups/remark",
                self.group_remark,
                ["POST"],
                "Save a group remark",
            ),
            (
                "groups/token-usage",
                self.group_token_usage,
                ["GET"],
                "Read group token usage and quota",
            ),
            (
                "groups/token-quota",
                self.group_token_quota,
                ["POST"],
                "Control a group token quota",
            ),
            ("players", self.players, ["GET"], "List players"),
            ("players/save", self.player_save, ["POST"], "Save player"),
            (
                "players/delete",
                self.player_delete,
                ["POST"],
                "Delete player",
            ),
            ("memories", self.memories, ["GET"], "List memories"),
            ("memories/save", self.memory_save, ["POST"], "Save memory"),
            (
                "memories/delete",
                self.memory_delete,
                ["POST"],
                "Delete memory",
            ),
            ("snapshots", self.snapshots, ["GET"], "List snapshots"),
            (
                "snapshots/create",
                self.snapshot_create,
                ["POST"],
                "Create snapshot",
            ),
            (
                "snapshots/restore",
                self.snapshot_restore,
                ["POST"],
                "Restore snapshot",
            ),
            (
                "snapshots/delete",
                self.snapshot_delete,
                ["POST"],
                "Delete snapshot",
            ),
            (
                "archives/delete",
                self.archive_delete,
                ["POST"],
                "Delete independent save archive",
            ),
            ("audit", self.audit, ["GET"], "Audit log"),
            ("providers", self.providers, ["GET"], "List chat providers"),
            ("settings", self.settings, ["GET"], "Tavern settings"),
            (
                "settings/save",
                self.settings_save,
                ["POST"],
                "Save Tavern settings",
            ),
            (
                "backup/export",
                self.backup_export,
                ["GET"],
                "Export Tavern backup",
            ),
            (
                "backup/import/<mode>",
                self.backup_import,
                ["POST"],
                "Import Tavern backup",
            ),
            ("events", self.events, ["GET"], "Tavern activity stream"),
            ("deliveries", self.deliveries, ["GET", "POST"], "Pending text deliveries"),
            # ── v0.12.0：副本实时仪表盘 ──────────────────────────────
            (
                "dashboard/sessions",
                self.dashboard_sessions,
                ["GET"],
                "Session realtime overview",
            ),
            (
                "dashboard/session",
                self.dashboard_session,
                ["GET"],
                "Session realtime detail",
            ),
            (
                "dashboard/timeline",
                self.dashboard_timeline,
                ["GET"],
                "Session event timeline",
            ),
            (
                "dashboard/timers",
                self.dashboard_timers_fast,
                ["GET"],
                "Session timers widget",
            ),
            (
                "dashboard/seed-quota",
                self.dashboard_seed_quota,
                ["POST"],
                "Seed default token quota",
            ),
            # ── v0.12.0：世界包社区注册表 / 市场 ──────────────────────
            ("market/list", self.market_list, ["GET"], "World package market"),
            (
                "market/fetch",
                self.market_fetch,
                ["POST"],
                "Fetch world package content",
            ),
            # ── A14：元素反应与通用接口 ─────────────────────────────
            (
                "worlds/element-reaction",
                self.world_element_reaction,
                ["POST"],
                "Resolve elemental reaction (dry-run)",
            ),
            (
                "worlds/element-table",
                self.world_element_table,
                ["GET"],
                "World elemental table",
            ),
            ("worlds/schema", self.world_schema, ["GET"], "World package schema"),
            ("worlds/export", self.world_export, ["GET"], "Export world package"),
            ("worlds/diff", self.world_diff, ["POST"], "Diff two world packages"),
            (
                "worlds/import-batch",
                self.world_import_batch,
                ["POST"],
                "Batch import worlds",
            ),
            (
                "worlds/simulate-batch",
                self.world_simulate_batch,
                ["POST"],
                "Batch rule simulation",
            ),
            (
                "worlds/resolution-table",
                self.world_resolution_table,
                ["GET"],
                "World resolution tables",
            ),
            (
                "sessions/turn-preflight",
                self.session_turn_preflight,
                ["GET"],
                "Turn preflight (read-only)",
            ),
            (
                "sessions/context-compile",
                self.session_context_compile,
                ["GET"],
                "Compiled context debug snapshot",
            ),
            (
                "sessions/inject-fact",
                self.session_inject_fact,
                ["POST"],
                "Inject a world fact",
            ),
            (
                "sessions/apply-effect",
                self.session_apply_effect,
                ["POST"],
                "Validate/dry-run declared effects",
            ),
            (
                "sessions/advance-clock",
                self.session_advance_clock,
                ["POST"],
                "Advance a scene clock",
            ),
            ("extensions", self.extensions, ["GET"], "Registered extensions"),
            ("hooks/events", self.hook_events, ["GET"], "Hook event catalog"),
            (
                "meta/capabilities",
                self.meta_capabilities,
                ["GET"],
                "Runtime capabilities",
            ),
            ("sessions/token-reset", self.session_token_reset, ["POST"], "Reset session token stats"),
            ("sessions/turn-command", self.session_turn_command, ["POST"], "Adjust turn order (DM)"),
            ("economy/summary", self.economy_summary, ["GET"], "Economy summary"),
            ("economy/set-enabled", self.economy_set_enabled, ["POST"], "Toggle economy"),
            ("economy/adjust", self.economy_adjust, ["POST"], "Adjust wallet balance"),
            ("economy/transactions", self.economy_transactions, ["GET"], "Economy transactions"),
            ("delegations/list", self.delegations_list, ["GET"], "List delegations"),
            ("delegations/grant", self.delegations_grant, ["POST"], "Grant delegation"),
            ("delegations/revoke", self.delegations_revoke, ["POST"], "Revoke delegation"),
            ("delegations/restore", self.delegations_restore, ["POST"], "Restore owner control"),
            ("delegations/forced-choose", self.delegations_forced_choose, ["POST"], "Forced choose"),
            ("dm/state", self.dm_console_state, ["GET"], "DM console state"),
            ("dm/command", self.dm_command, ["POST"], "DM command"),
        ]
        for route in routes:
            self._register(*route)

    @staticmethod
    def _username() -> str:
        username = str(request.username or "").strip()
        if not username:
            raise PermissionError("需要登录 AstrBot 管理后台")
        return username

    @classmethod
    def _actor(cls) -> str:
        return f"web:{cls._username()}"

    # ── v0.12.0：副本实时仪表盘端点 ──────────────────────────────────
    async def dashboard_sessions(self):
        """副本概览列表：状态 / 世界 / 当前行动者 / 活跃计时器数。"""
        try:
            self._username()
            sessions = await build_dashboard_sessions(self.database)
            return json_response({"sessions": sessions})
        except Exception as exc:
            return self._handle_error(exc)

    async def dashboard_session(self):
        """单个副本的实时聚合（状态机 / 行动者 / 计时器 / 选项 / 投票）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            payload = await build_session_dashboard(self.database, session_id)
            return json_response(payload)
        except Exception as exc:
            return self._handle_error(exc)

    async def dashboard_timeline(self):
        """事件时间线（回放视图）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            limit = int(request.query.get("limit", "30") or "30")
            payload = await build_session_timeline(
                self.database, session_id, limit=limit
            )
            return json_response(payload)
        except Exception as exc:
            return self._handle_error(exc)

    async def dashboard_timers_fast(self):
        """轻量倒计时列表（供嵌入式小窗口局部刷新；支持 ?order=asc|desc）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            order = str(request.query.get("order", "desc") or "desc")
            timers = await build_session_timers(
                self.database, session_id, order=order
            )
            return json_response({"timers": timers})
        except Exception as exc:
            return self._handle_error(exc)

    async def dashboard_seed_quota(self):
        """用配置默认值给尚未配置配额策略的副本播种（F3 的控制台入口）。"""
        try:
            self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id")
            config = TavernConfig.from_mapping(self.plugin_config)
            summary = await self.database.ensure_default_token_quota(
                session_id,
                window_seconds=config.token_quota_window_seconds,
                token_limit=config.token_quota_token_limit,
                enabled=config.token_quota_enabled,
                actor_id=self._actor(),
            )
            return json_response(
                {
                    "seeded": config.token_quota_enabled,
                    "quota": summary.get("quotas", []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    # ── v0.12.0：世界包社区注册表 / 市场端点 ─────────────────────────
    def _remote_market_config(self) -> TavernConfig | None:
        """返回启用远程市场时的配置；未启用或缺少 HTTP 客户端时返回 None。"""
        config = TavernConfig.from_mapping(self.plugin_config)
        if not config.world_market_enabled:
            return None
        if not config.world_market_remote_manifest_url:
            return None
        if getattr(self.context, "http_client", None) is None:
            self.logger.warning(
                "AI 酒馆远程市场已启用，但 AstrBot 未提供 HTTP 客户端"
            )
            return None
        return config

    async def market_list(self):
        """市场条目列表（本地 + 可选远程）。

        - ``?manifest_url=``：控制台「GitHub 直链」入口传入时，无论配置是否启用远程市场，
          都从该地址拉取远程清单并合并到本地条目（仍受主机白名单 / 体积上限约束）。
        - 未传 manifest_url 时保持既有行为（仅配置启用且配置了地址才拉取）。
        """
        try:
            self._username()
            query = str(request.query.get("q", "") or "")
            explicit_url = str(
                request.query.get("manifest_url", "") or ""
            ).strip()
            # 0.12.0-A5：市场默认为空（不再内联本地示例/模板），仅通过
            # GitHub 直链或配置的远程清单拉取条目。
            entries: list[dict[str, Any]] = []
            remote_enabled = False
            config = self._remote_market_config()
            fetch_target = explicit_url or (
                config.world_market_remote_manifest_url
                if config is not None
                else ""
            )
            if fetch_target:
                client = self.context.http_client
                allowed = (
                    config.world_market_allowed_hosts
                    if config is not None
                    else None
                )
                try:
                    remote = await fetch_remote_manifest(
                        client,
                        fetch_target,
                        allowed,
                        max_bytes=(
                            config.world_market_max_package_bytes
                            if config is not None
                            else 2_000_000
                        ),
                        ttl_seconds=(
                            config.world_market_cache_ttl_seconds
                            if config is not None
                            else 600
                        ),
                    )
                    entries = [*entries, *remote]
                    remote_enabled = True
                except LookupError as exc:
                    self.logger.warning("AI 酒馆远程市场拉取失败：%s", exc)
                except Exception as exc:
                    report_failure(
                        self.logger,
                        stage="market",
                        operation="manifest",
                        exc=exc,
                        transient=True,
                    )
            if query:
                entries = search_entries(entries, query)
            return json_response(
                {
                    "items": entries,
                    "remote_enabled": remote_enabled,
                    "remote_error": None,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def market_fetch(self):
        """按 package_key 拉取完整世界包内容（本地或远程；供预览与导入）。"""
        try:
            self._username()
            payload = await self._payload()
            package_key = str(payload.get("package_key") or "").strip()
            if not package_key:
                raise ValueError("缺少 package_key")
            root = Path(__file__).resolve().parent.parent
            if package_key.startswith("remote:"):
                config = self._remote_market_config()
                if config is None:
                    raise LookupError("远程市场未启用或缺少 HTTP 客户端")
                entries = await fetch_remote_manifest(
                    self.context.http_client,
                    config.world_market_remote_manifest_url,
                    config.world_market_allowed_hosts,
                    max_bytes=config.world_market_max_package_bytes,
                    ttl_seconds=config.world_market_cache_ttl_seconds,
                )
                entry = next(
                    (
                        item
                        for item in entries
                        if item.get("package_key") == package_key
                    ),
                    None,
                )
                if entry is None:
                    raise LookupError("远程条目不存在")
                content = await fetch_remote_package(
                    self.context.http_client,
                    entry,
                    config.world_market_allowed_hosts,
                    max_bytes=config.world_market_max_package_bytes,
                    verify_sha256=config.world_market_verify_sha256,
                )
                report = inspect_world_package(content)
                return json_response(
                    {"entry": entry, "world": content, "preflight": report}
                )
            entry, content = fetch_entry(root, package_key)
            report = inspect_world_package(content)
            return json_response(
                {"entry": entry, "world": content, "preflight": report}
            )
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        except Exception as exc:
            return self._handle_error(exc)

    def _handle_error(self, exc: Exception):
        if isinstance(exc, PermissionError):
            return error_response(str(exc), status_code=401)
        if isinstance(exc, PolicyRejection):
            return error_response(str(exc), status_code=403)
        if isinstance(exc, DatabaseNotFoundError):
            return error_response(str(exc), status_code=404)
        if isinstance(exc, DatabaseConflictError):
            return error_response(str(exc), status_code=409)
        if isinstance(exc, (InvalidTransitionError, ValueError, TypeError)):
            return error_response(str(exc), status_code=400)
        if isinstance(exc, sqlite3.IntegrityError):
            return error_response(
                "数据冲突：标识、群会话或名称可能已存在",
                status_code=409,
            )
        # v0.12.0：未知异常统一经 report_failure 上报（含请求上下文），
        # 避免被静默吞掉；同时保留原 500 响应语义。
        report_failure(
            self.logger,
            stage="webui",
            operation=str(getattr(request, "path", "unknown")),
            exc=exc,
        )
        return error_response("服务器内部错误", status_code=500)

    async def _payload(self) -> dict[str, Any]:
        value = await request.json(default={})
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    async def overview(self):
        try:
            self._username()
            result = await self.database.overview()
            result["plugin_version"] = PLUGIN_VERSION
            result["sessions"] = (await self.database.list_sessions())[:8]
            config = TavernConfig.from_mapping(self.plugin_config)
            result["security"] = {
                "admin_count": len(config.admin_ids),
                "allowed_group_count": len(config.allowed_group_ids),
                "whitelist_required": config.require_group_whitelist,
                "ready": bool(config.admin_ids),
            }
            # 0.12.0-A3：Token 用量（WebUI 总览顶部指标卡 / 关键数据）。
            window = max(60, config.token_quota_window_seconds)
            result["token_usage"] = {
                "enabled": config.token_quota_enabled,
                "used": await self.database.global_token_usage(window),
                "window_seconds": window,
                "limit": config.token_quota_token_limit,
            }
            # 0.12.0-A3：开馆前检查（WebUI 总览「开馆前检查」卡片）。
            health = await self.database.list_provider_health()
            result["provider_health"] = health
            result["readiness"] = {
                "admin_ready": bool(config.admin_ids),
                "whitelisted_groups": len(config.allowed_group_ids),
                "whitelist_required": config.require_group_whitelist,
                "worlds_ready": int(result["counts"]["worlds"]),
                "card_code_ttl_seconds": int(
                    config.time_rules.get("card_code_ttl_seconds") or 1800
                ),
                "providers_ready": bool(health) and all(
                    bool(item.get("success")) for item in health
                ),
            }
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def worlds(self):
        try:
            self._username()
            archived_value = request.query.get("include_archived", "")
            include_archived = str(archived_value).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            return json_response(
                {"items": await self.database.list_worlds(include_archived)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_save(self):
        try:
            submitted = await self._payload()
            current = None
            world_id = str(submitted.get("id") or "").strip()
            if world_id:
                current = await self.database.get_world(world_id)
            payload = world_edit_payload(submitted, current)
            report = inspect_world_package(payload)
            if not report["compatible"]:
                messages = [
                    item["message"]
                    for item in report["issues"]
                    if item["level"] == "error"
                ]
                raise ValueError("世界包体检未通过：" + "；".join(messages[:5]))
            item = await self.database.save_world(payload, self._actor())
            self._purge_rule_runtime(str(item.get("slug") or ""))
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response({"item": item, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_preflight(self):
        try:
            self._username()
            payload = await self._payload()
            world_ref = str(payload.get("world_ref") or "")
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world", payload)
            )
            if not isinstance(world, dict):
                raise ValueError("world 必须是 JSON 对象")
            return json_response({"report": inspect_world_package(world)})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_order(self):
        try:
            payload = await self._payload()
            item = await self.database.set_world_sort_order(
                str(payload.get("id") or ""),
                int(payload.get("sort_order") or 1),
                self._actor(),
            )
            await self.broker.publish({"type": "world", "action": "reorder"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_simulate(self):
        try:
            # 0.11.1：补齐鉴权（此前是唯一漏掉 _username() 的路由），
            # 并对可投递的 world/intent/context 做体积与深度上限，防计算型 DoS。
            self._username()
            payload = await self._payload()
            world_ref = str(payload.get("world_ref") or "")
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world")
            )
            if not isinstance(world, dict):
                raise ValueError("请提供 world_ref 或 world")
            if _json_size(world) > _WORLD_SIMULATE_MAX_BYTES:
                raise ValueError("世界包过大，拒绝执行规则模拟")
            if _json_depth(world):
                raise ValueError("世界包嵌套过深，拒绝执行规则模拟")
            report = inspect_world_package(world)
            if not report["compatible"]:
                raise ValueError("世界包体检未通过，不能执行规则模拟")
            intent = payload.get("intent")
            context = payload.get("context", {})
            if not isinstance(intent, dict) or not isinstance(context, dict):
                raise ValueError("intent 与 context 必须是 JSON 对象")
            if _json_depth({"intent": intent, "context": context}):
                raise ValueError("intent 或 context 嵌套过深")
            result = self._cached_rule_runtime(world).resolve_action_intent(
                intent, context, dry_run=True,
                world_snapshot_id=f"preview:{world.get('slug', '')}",
            )
            return json_response({"result": result, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_migration(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            instance = await self.database.get_instance_config(session_id)
            candidate_ref = str(payload.get("candidate_world_ref") or "")
            candidate = (
                await self.database.get_world(candidate_ref)
                if candidate_ref
                else payload.get("candidate_world")
            )
            if not isinstance(candidate, dict):
                raise ValueError("请提供候选世界包或世界标识")
            return json_response(
                {
                    "report": compare_world_contracts(
                        instance.get("world_snapshot") or {}, candidate
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_archive(self):
        try:
            payload = await self._payload()
            world_id = str(payload.get("id", ""))
            world = await self.database.get_world(world_id)
            config = TavernConfig.from_mapping(self.plugin_config)
            if world["slug"] == config.default_world_slug:
                raise ValueError(
                    "该世界是当前默认世界，请先在设置中更换默认世界"
                )
            item = await self.database.archive_world(
                world_id,
                self._actor(),
            )
            self._purge_rule_runtime(world["slug"])
            await self.broker.publish({"type": "world", "action": "archive"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_restore(self):
        try:
            payload = await self._payload()
            world_id = str(payload.get("id", ""))
            current = await self.database.get_world(world_id)
            item = await self.database.restore_world(
                world_id,
                self._actor(),
            )
            self._purge_rule_runtime(current["slug"])
            await self.broker.publish({"type": "world", "action": "restore"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def characters(self):
        try:
            self._username()
            world_id = str(request.query.get("world_id", "") or "")
            return json_response(
                {"items": await self.database.list_characters(world_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def character_save(self):
        try:
            item = await self.database.save_character(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def character_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_character(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_import(self):
        """导入世界包 JSON：校验结构后按 slug 新建或更新世界。"""
        try:
            self._username()
            request_payload = await self._payload()
            import_mode = str(request_payload.get("import_mode") or "auto")
            payload = request_payload.get("world", request_payload)
            if not isinstance(payload, dict):
                raise ValueError("世界包 JSON 格式无效：顶层必须是一个对象")
            if not payload.get("slug"):
                raise ValueError("世界包缺少必填字段 slug")
            if not payload.get("name"):
                raise ValueError("世界包缺少必填字段 name（世界名称）")
            if not payload.get("system_prompt"):
                raise ValueError("世界包缺少必填字段 system_prompt（世界设定）")
            rules = payload.get("rules")
            if rules is not None and not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            initial_state = payload.get("initial_state")
            if initial_state is not None and not isinstance(initial_state, dict):
                raise ValueError("initial_state 必须是 JSON 对象")
            report = inspect_world_package(payload)
            if not report["compatible"]:
                messages = [
                    item["message"]
                    for item in report["issues"]
                    if item["level"] == "error"
                ]
                raise ValueError("世界包体检未通过：" + "；".join(messages[:5]))
            import_payload = world_import_payload(payload)
            mode = "created"
            try:
                existing = await self.database.get_world(str(payload["slug"]))
                existing_package = world_import_payload(existing)
                incoming_hash = hashlib.sha256(
                    json.dumps(import_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                existing_hash = hashlib.sha256(
                    json.dumps(existing_package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if incoming_hash == existing_hash:
                    return json_response({"item": existing, "mode": "identical", "preflight": report})
                if import_mode == "copy":
                    copy_slug = str(request_payload.get("copy_slug") or f"{payload['slug']}-copy")
                    suffix = 2
                    while True:
                        try:
                            await self.database.get_world(copy_slug)
                            copy_slug = f"{payload['slug']}-copy-{suffix}"
                            suffix += 1
                        except DatabaseNotFoundError:
                            break
                    import_payload["slug"] = copy_slug
                    mode = "copied"
                else:
                    old_version = str(existing.get("world_content_version") or existing.get("version") or "")
                    new_version = str(payload.get("world_content_version") or payload.get("version") or "")
                    if old_version and new_version and old_version == new_version and import_mode != "force_revision":
                        raise DatabaseConflictError(
                            f"{_WORLD_CONFLICT_PREFIX}：同 slug、同内容版本但内容不同；"
                            "请选择“覆盖为新修订”或“另存副本”"
                        )
                    old_key = _content_version_key(old_version)
                    new_key = _content_version_key(new_version)
                    if (
                        old_key is not None
                        and new_key is not None
                        and new_key < old_key
                        and import_mode != "force_revision"
                    ):
                        raise DatabaseConflictError(
                            f"{_WORLD_CONFLICT_PREFIX}：导入包的内容版本低于现有世界；"
                            "如确需回退，请明确选择“覆盖为新修订”"
                        )
                    import_payload["id"] = existing["id"]
                    import_payload["revision"] = existing["revision"]
                    mode = "updated"
            except DatabaseNotFoundError:
                pass
            item = await self.database.save_world(import_payload, self._actor())
            self._purge_rule_runtime(str(item.get("slug") or ""))
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response({"item": item, "mode": mode, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def character_import(self):
        """导入常驻角色/NPC JSON：校验后批量写入指定世界。"""
        try:
            self._username()
            payload = await self._payload()
            world_ref = (
                payload.get("world_id")
                or payload.get("world_slug")
                or payload.get("worldRef")
                or ""
            )
            if not str(world_ref).strip():
                raise ValueError(
                    "请指定目标世界：world_id 或 world_slug 不能为空"
                )
            world = await self.database.get_world(str(world_ref).strip())
            template_version = int(payload.get("template_version", 1) or 1)
            if template_version not in {1, 2}:
                raise ValueError("只接受 NPC 导入模板 v1 或 v2")
            items = (
                payload.get("items")
                or payload.get("npcs")
                or payload.get("characters")
            )
            if not isinstance(items, list) or not items:
                raise ValueError(
                    "常驻角色数据必须是非空数组（字段 items / npcs）"
                )
            existing_items = await self.database.list_characters(world["id"])
            existing_by_slug = {
                str(item.get("slug")): item for item in existing_items
                if item.get("slug")
            }
            existing_by_name = {
                str(item.get("name")): item for item in existing_items
                if item.get("name")
            }
            registry = EntityRegistry(world) if int(world.get("world_schema_version", 0) or 0) >= 5 else None
            input_slugs: set[str] = set()
            created = []
            for index, raw in enumerate(items):
                if not isinstance(raw, dict):
                    raise ValueError(f"第 {index + 1} 个角色项必须是对象")
                name = str(raw.get("name") or "").strip()
                if not name:
                    raise ValueError(f"第 {index + 1} 个角色缺少 name（名称）")
                role = str(raw.get("role") or "npc").strip() or "npc"
                npc_profile = raw.get("profile")
                if npc_profile is not None and not isinstance(npc_profile, dict):
                    raise ValueError(
                        f"角色「{name}」的 profile 必须是 JSON 对象"
                    )
                profile = dict(npc_profile) if isinstance(npc_profile, dict) else {}
                raw_slug = str(raw.get("slug") or "").strip()
                if raw_slug:
                    if raw_slug in input_slugs:
                        raise ValueError(f"NPC 导入包含重复 slug：{raw_slug}")
                    input_slugs.add(raw_slug)
                    validate_slug(raw_slug)
                if registry is not None:
                    ref_fields = {
                        "capability_refs": "capability",
                        "resource_refs": "resource",
                        "runtime_effect_refs": "runtime_effect",
                        "object_refs": "object",
                    }
                    for field, expected_type in ref_fields.items():
                        values = profile.get(field, [])
                        if not isinstance(values, list):
                            raise ValueError(f"角色「{name}」的 {field} 必须是数组")
                        for ref in values:
                            registry.resolve(ref, expected_type)
                    resources = profile.get("resources", {})
                    if resources and not isinstance(resources, dict):
                        raise ValueError(f"角色「{name}」的 resources 必须是对象")
                    if isinstance(resources, dict):
                        for ref in resources:
                            registry.resolve(ref, "resource")
                private = raw.get("private_direction") or raw.get("prompt") or ""
                if private:
                    profile.setdefault("private_direction", private)
                character_payload = {
                    "world_id": world["id"],
                    "name": name,
                    "role": role,
                    "profile": profile,
                    "prompt": str(private),
                    "enabled": 1,
                }
                prior = existing_by_slug.get(raw_slug) if raw_slug else None
                if prior is None:
                    # v1 did not require stable slugs, so name fallback remains
                    # solely for legacy imports.
                    prior = existing_by_name.get(name)
                if prior:
                    character_payload["id"] = prior["id"]
                    character_payload["revision"] = prior["revision"]
                    character_payload["slug"] = prior["slug"]
                    character_payload["sort_order"] = prior.get("sort_order", 0)
                    character_payload["enabled"] = prior.get("enabled", 1)
                else:
                    character_payload["slug"] = raw_slug or f"npc_{uuid.uuid4().hex[:12]}"
                if "sort_order" in raw:
                    character_payload["sort_order"] = int(raw["sort_order"])
                created.append(
                    await self.database.save_character(
                        character_payload, self._actor()
                    )
                )
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response(
                {
                    "created": len(created),
                    "world_id": world["id"],
                    "world_name": world.get("name"),
                    "items": created,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def sessions(self):
        try:
            self._username()
            result = await self.database.search_sessions(
                str(request.query.get("q", "") or ""),
                str(request.query.get("scope", "all") or "all"),
                request.query.get("page", 1, type=int),
                request.query.get("page_size", 20, type=int),
            )
            result["options"] = await self.database.list_session_options()
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def session_detail(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            return json_response(
                {
                    "session": session,
                    "players": await self.database.list_players(session_id),
                    "roster": await self.database.list_roster(session_id),
                    "turn": await self.database.get_turn_status(session_id),
                    "control": await self.database.get_control_state(session_id),
                    "events": await self.database.recent_events(session_id, 80),
                    "snapshots": await self.database.list_snapshots(session_id),
                    "instance_config": (
                        await self.database.get_instance_config(session_id)
                    ),
                    "timers": await self.database.list_timers(session_id),
                    "timer_policy": (
                        await self.database.get_timer_policy(session_id)
                    ),
                    "token_usage": _with_token_context(
                        await self.database.token_usage_summary(session_id),
                        await self.database.get_instance_config(session_id),
                    ),
                    "choice": await self.database.active_choice_set(session_id),
                    "vote": await self.database.active_vote(session_id),
                    "bans": await self.database.list_bans(session_id),
                    "permissions": (
                        await self.database.list_permission_grants(session_id)
                    ),
                    "economy": (
                        await self.database.economy_summary(session_id)
                    ),
                    "delegations": (
                        await self.database.list_delegations(session_id)
                    ),
                    "pending_operations": (
                        await self.database.pending_operations(session_id)
                    ),
                    "return_requests": (
                        await self.database.list_return_requests(session_id)
                    ),
                    "preflight": (
                        await self.database.opening_preflight(session_id)
                    ),
                    "rule_state": (
                        await self.database.get_session_rule_state(session_id)
                    ),
                    "session_characters": (
                        await self.database.list_session_characters(session_id)
                    ),
                    "story_ledger": (
                        await self.database.list_story_ledger(session_id)
                    ),
                    "scene_clocks": (
                        await self.database.list_scene_clocks(session_id)
                    ),
                    "memories": await self.database.list_memories(
                        session_id,
                        "",
                        500,
                        include_invalidated=True,
                    ),
                    "archive": (
                        await self.database.get_session_archive(session_id)
                    ),
                    "storage": (
                        await self.database.get_storage_info(session_id)
                    ),
                    "operations": (
                        await self.database.list_session_operations(session_id, 50)
                    ),
                    "card_revisions": (
                        await self.database.list_card_revisions(session_id)
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_recovery(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            operations = await self.database.list_session_operations(session_id, 100)
            choice = await self.database.active_choice_set(session_id)
            vote = await self.database.active_vote(session_id)
            return json_response(
                {
                    "recovery": recovery_summary(
                        operations,
                        session_state=str(session.get("state") or ""),
                        has_active_choices=bool(choice),
                        has_active_vote=bool(vote),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_diagnostics(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            report = await build_diagnostic_report(self.database, session_id)
            export_dir = self.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            path = next_timestamped_path(export_dir, "tavern_diagnostic", ".zip")
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            payload = (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            try:
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("diagnostic.json", payload)
                    archive.writestr(
                        "README.txt",
                        "AI 酒馆脱敏故障报告。用户 ID 已哈希，密钥、私聊来源、私人字段与完整系统提示词未导出。\n",
                    )
                replace_with_retry(temporary, path)
            finally:
                unlink_with_retry(temporary, suppress_errors=True)
            return file_response(path, filename=path.name, content_type="application/zip")
        except Exception as exc:
            return self._handle_error(exc)

    async def session_rescue(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            service = EmergencyService(self.database)
            result = await service.execute(
                session_id,
                str(payload.get("action") or ""),
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {"type": "session", "action": "rescue", "session_id": session_id}
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_revisions(self):
        try:
            if request.method == "GET":
                self._username()
                session_id = str(request.query.get("id", "") or "")
                return json_response(
                    {"items": await self.database.list_card_revisions(session_id)}
                )
            payload = await self._payload()
            action = str(payload.get("action") or "request").lower()
            if action == "request":
                item = await self.database.request_card_revision(
                    str(payload.get("session_id") or ""),
                    str(payload.get("participant_ref") or ""),
                    payload.get("profile_patch") or {},
                    payload.get("stats_patch") or {},
                    self._actor(),
                    str(payload.get("note") or ""),
                )
            elif action in {"approve", "reject"}:
                item = await self.database.review_card_revision(
                    str(payload.get("request_id") or ""),
                    action == "approve",
                    self._actor(),
                    str(payload.get("note") or ""),
                )
            else:
                raise ValueError("不支持的角色卡修订操作")
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_remark(self):
        try:
            payload = await self._payload()
            item = await self.database.save_group_remark(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                str(payload.get("remark") or ""),
                self._actor(),
                int(payload.get("revision") or 0),
            )
            await self.broker.publish(
                {
                    "type": "group",
                    "action": "remark",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_usage(self):
        try:
            self._username()
            item = await self.database.group_token_usage_summary(
                str(request.query.get("platform_id", "") or ""),
                str(request.query.get("group_id", "") or ""),
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_quota(self):
        try:
            payload = await self._payload()
            item = await self.database.set_group_token_quota(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                window_seconds=int(
                    payload.get("window_seconds") or 86_400
                ),
                token_limit=int(payload.get("token_limit") or 500_000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "group_quota",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_action(self):
        try:
            payload = await self._payload()
            actor = self._actor()
            action = str(payload.get("action", "")).strip().lower()
            if action == "create":
                platform_id = str(payload.get("platform_id") or "")
                group_id = str(payload.get("group_id") or "")
                session = await self.database.ensure_session(
                    platform_id,
                    group_id,
                    str(payload.get("unified_origin") or ""),
                    str(payload.get("world_ref") or ""),
                    actor,
                    str(payload.get("instance_slug") or ""),
                    str(payload.get("instance_name") or ""),
                )
                config = TavernConfig.from_mapping(self.plugin_config)
                world = await self.database.get_world(session["world_id"])
                world_rules = world.get("rules") or {}
                world_time = (
                    world_rules.get("time_rules")
                    if isinstance(world_rules, dict)
                    else {}
                )
                await self.database.save_instance_time_rules(
                    session["id"],
                    normalize_time_rules(
                        {
                            **dict(config.time_rules),
                            **(
                                dict(world_time)
                                if isinstance(world_time, dict)
                                else {}
                            ),
                        }
                    ),
                    actor,
                )
                await self.database.grant_permission(
                    session["id"],
                    actor,
                    "host",
                    actor,
                )
                await self.allow_group(
                    group_id=group_id,
                    platform_id=platform_id,
                    actor_id=actor,
                    source="web_session_create",
                )
            elif action == "clone":
                session = await self.database.clone_session(
                    str(payload.get("session_id") or ""),
                    actor,
                    instance_slug=str(
                        payload.get("instance_slug") or ""
                    ),
                    instance_name=str(
                        payload.get("instance_name") or ""
                    ),
                    snapshot_ref=str(
                        payload.get("snapshot_ref") or ""
                    ),
                    candidate_world_ref=str(
                        payload.get("candidate_world_ref") or ""
                    ),
                )
            else:
                session_id = str(payload.get("session_id") or "")
                if action == "force_ready":
                    result = await self.database.force_all_ready(
                        session_id,
                        actor,
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "dm_enable":
                    result = await self.database.enable_dm_mode(
                        session_id,
                        str(payload.get("dm_user_id") or actor),
                        actor,
                    )
                    return json_response({"control": result})
                if action == "dm_directive":
                    result = await self.database.set_dm_directive(
                        session_id,
                        str(payload.get("directive") or ""),
                        str(payload.get("dm_user_id") or actor),
                    )
                    return json_response({"control": result})
                if action == "dm_direct":
                    session = await self.database.get_session(session_id)
                    dm_user_id = str(payload.get("dm_user_id") or "")
                    result = await self.database.commit_dm_beat(
                        session_id=session_id,
                        expected_revision=int(session["revision"]),
                        dm_user_id=dm_user_id,
                        instruction=str(payload.get("narrative") or ""),
                        narrative=str(payload.get("narrative") or ""),
                        world_state=session["world_state"],
                        direct=True,
                    )
                    return json_response({"result": result})
                if action == "dm_disable":
                    result = await self.database.disable_dm_mode(session_id, actor)
                    return json_response({"control": result})
                if action == "delete":
                    result = await self.database.delete_session(
                        session_id,
                        actor,
                        str(payload.get("confirm_name") or ""),
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "perform":
                    result = await self.database.activate_story(
                        session_id,
                        actor,
                        resume=bool(payload.get("resume", False)),
                    )
                    if not result["started"]:
                        raise ValueError(
                            "；".join(result.get("blockers") or ["准备未完成"])
                        )
                    await self.database.resume_session_timers(
                        session_id,
                        actor,
                    )
                    session = result["session"]
                else:
                    if action == "pause":
                        await self.database.pause_session_timers(
                            session_id,
                            actor,
                        )
                    if action in {"finish", "abort"}:
                        session = await self.database.finalize_session(
                            session_id,
                            actor,
                            termination_type=(
                                "aborted"
                                if action == "abort"
                                else "completed"
                            ),
                            reason=str(
                                payload.get("reason")
                                or (
                                    "正常完结"
                                    if action == "finish"
                                    else ""
                                )
                            ),
                        )
                        await self.broker.publish(
                            {
                                "type": "session",
                                "action": action,
                                "session_id": session["id"],
                            }
                        )
                        return json_response({"session": session})
                    target_map = {
                        "start": SESSION_PREPARING,
                        "resume": SESSION_PREPARING,
                        "pause": SESSION_PAUSED,
                        "close": SESSION_CLOSED,
                        "maintenance": SESSION_MAINTENANCE,
                    }
                    if action not in target_map:
                        raise ValueError("不支持的会话操作")
                    session = await self.database.transition_session(
                        session_id,
                        target_map[action],
                        actor,
                        str(payload.get("world_ref") or ""),
                    )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": action,
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_state(self):
        try:
            payload = await self._payload()
            state = payload.get("world_state")
            if not isinstance(state, dict):
                raise ValueError("world_state 必须是 JSON 对象")
            session = await self.database.save_manual_state(
                str(payload.get("session_id") or ""),
                state,
                int(payload.get("revision")),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "state_edit",
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_turn_order(self):
        try:
            payload = await self._payload()
            order = payload.get("order")
            if not isinstance(order, list):
                raise ValueError("order 必须是用户 ID 数组")
            turn = await self.database.set_turn_order(
                str(payload.get("session_id") or ""),
                [str(item) for item in order],
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "turn_order",
                    "session_id": str(payload.get("session_id") or ""),
                }
            )
            return json_response({"turn": turn})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_time_rules(self):
        try:
            payload = await self._payload()
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_instance_time_rules(
                str(payload.get("session_id") or ""),
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"instance_config": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_rules(self):
        try:
            payload = await self._payload()
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_session_rule_state(
                str(payload.get("session_id") or ""),
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"rule_state": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_npc(self):
        try:
            payload = await self._payload()
            item = await self.database.save_session_character(
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "npc_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"character": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_timer(self):
        try:
            payload = await self._payload()
            item = await self.database.control_timer(
                str(payload.get("timer_id") or ""),
                str(payload.get("action") or ""),
                self._actor(),
                seconds=int(payload.get("seconds") or 0),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": str(payload.get("action") or ""),
                    "session_id": item["session_id"],
                }
            )
            return json_response({"timer": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_timer_policy(self):
        try:
            payload = await self._payload()
            item = await self.database.set_timer_policy(
                str(payload.get("session_id") or ""),
                str(payload.get("timer_type") or "all"),
                bool(payload.get("enabled", False)),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "policy",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"policy": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_token_quota(self):
        try:
            payload = await self._payload()
            item = await self.database.set_token_quota(
                str(payload.get("session_id") or ""),
                str(payload.get("scope_type") or "session"),
                window_seconds=int(payload.get("window_seconds") or 3600),
                token_limit=int(payload.get("token_limit") or 100000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "quota",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_review(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            approved = bool(payload.get("approved", False))
            note = str(payload.get("note") or "")
            item = await self.database.review_character_card(
                session_id,
                str(payload.get("participant_ref") or ""),
                approved,
                self._actor(),
                note,
            )
            await self.broker.publish(
                {
                    "type": "card",
                    "action": "review",
                    "session_id": item["session_id"],
                }
            )
            await notify_card_review(
                context=self.context,
                database=self.database,
                broker=self.broker,
                config=TavernConfig.from_mapping(self.plugin_config),
                participant=item,
                approved=approved,
                note=note,
                logger=self.logger,
            )
            return json_response({"participant": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_permission(self):
        try:
            payload = await self._payload()
            item = await self.database.grant_permission(
                str(payload.get("session_id") or ""),
                str(payload.get("user_id") or ""),
                str(payload.get("role") or ""),
                self._actor(),
            )
            return json_response({"permission": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_participant(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            participant_ref = str(payload.get("participant_ref") or "")
            action = str(payload.get("action") or "").strip().lower()
            actor = self._actor()
            if action == "retire":
                result = await self.database.retire_participant(
                    session_id,
                    participant_ref,
                    actor,
                    forced=True,
                    reason=str(payload.get("reason") or "web_retire"),
                )
            elif action == "ban":
                duration = payload.get("duration_seconds")
                result = await self.database.create_ban(
                    session_id,
                    participant_ref,
                    actor,
                    scope=str(payload.get("scope") or "instance"),
                    duration_seconds=(
                        int(duration) if duration not in {None, ""} else None
                    ),
                    reason=str(payload.get("reason") or ""),
                )
            elif action == "unban":
                result = {
                    "revoked": await self.database.revoke_ban(
                        session_id,
                        participant_ref,
                        actor,
                    )
                }
            elif action == "designate":
                participant = await self.database.get_participant(
                    session_id,
                    participant_ref=participant_ref,
                )
                result = await self.database.designate_turn(
                    session_id,
                    participant["group_user_id"],
                    actor,
                )
            else:
                raise ValueError("不支持的参与者操作")
            await self.broker.publish(
                {
                    "type": "participant",
                    "action": action,
                    "session_id": session_id,
                }
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def players(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_players(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def player_save(self):
        try:
            item = await self.database.save_player(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def player_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_player(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def memories(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            query = str(request.query.get("q", "") or "")
            limit = request.query.get("limit", 100, type=int)
            return json_response(
                {
                    "items": await self.database.list_memories(
                        session_id,
                        query,
                        limit,
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def memory_save(self):
        try:
            item = await self.database.save_memory(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def memory_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_memory(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshots(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_snapshots(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_create(self):
        try:
            payload = await self._payload()
            item = await self.database.create_snapshot(
                str(payload.get("session_id") or ""),
                str(payload.get("name") or ""),
                self._actor(),
                bool(payload.get("replace", False)),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_restore(self):
        try:
            payload = await self._payload()
            item = await self.database.restore_snapshot(
                str(payload.get("session_id") or ""),
                str(payload.get("snapshot_ref") or ""),
                self._actor(),
            )
            await self.database.pause_session_timers(
                str(payload.get("session_id") or ""),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "restore",
                    "session_id": item["id"],
                }
            )
            return json_response({"session": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_snapshot(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def archive_delete(self):
        try:
            payload = await self._payload()
            item = await asyncio.to_thread(
                self.database.storage.trash_archive,
                str(payload.get("session_id") or ""),
                kind=str(payload.get("kind") or "save"),
                filename=str(payload.get("filename") or ""),
            )
            await self.broker.publish(
                {
                    "type": "storage",
                    "action": "archive_delete",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def audit(self):
        try:
            self._username()
            return json_response(
                {
                    "items": await self.database.list_audit(
                        str(request.query.get("session_id", "") or ""),
                        request.query.get("limit", 100, type=int),
                        request.query.get("offset", 0, type=int),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def providers(self):
        try:
            self._username()
            getter = getattr(self.context, "get_all_providers", None)
            providers = getter() if callable(getter) else []
            items: list[dict[str, str]] = []
            seen: set[str] = set()
            for provider in providers or []:
                meta_getter = getattr(provider, "meta", None)
                meta = meta_getter() if callable(meta_getter) else None
                meta_id = (
                    meta.get("id", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "id", "")
                )
                provider_id = str(
                    meta_id
                    or getattr(provider, "id", "")
                    or ""
                ).strip()
                if not provider_id or provider_id in seen:
                    continue
                seen.add(provider_id)
                meta_name = (
                    meta.get("name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "name", "")
                )
                provider_name = (
                    meta.get("provider_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "provider_name", "")
                )
                name = str(
                    meta_name
                    or provider_name
                    or provider_id
                ).strip()
                meta_model = (
                    meta.get("model", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model", "")
                )
                meta_model_name = (
                    meta.get("model_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model_name", "")
                )
                model = str(
                    meta_model
                    or meta_model_name
                    or getattr(provider, "model_name", "")
                    or ""
                ).strip()
                items.append(
                    {
                        "id": provider_id,
                        "name": name,
                        "model": model,
                    }
                )
            return json_response({"items": items})
        except Exception as exc:
            return self._handle_error(exc)

    async def settings(self):
        try:
            self._username()
            config = TavernConfig.from_mapping(self.plugin_config)
            settings = config.to_mapping()
            revision = await self.database.record_configuration_revision(
                settings,
                self._actor(),
            )
            return json_response(
                {
                    "settings": settings,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                    "readiness": {
                        "has_admin": bool(config.admin_ids),
                        "has_allowed_group": bool(config.allowed_group_ids)
                        or not config.require_group_whitelist,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def settings_save(self):
        try:
            payload = await self._payload()
            self._username()
            normalized = TavernConfig.from_mapping(payload).to_mapping()
            default_world = await self.database.get_world(
                normalized["runtime"]["default_world_slug"]
            )
            if default_world["archived"]:
                raise ValueError("默认世界不能使用已归档世界")
            async with self.config_lock:
                missing = object()
                previous = {
                    section: self.plugin_config.get(section, missing)
                    for section in normalized
                }
                try:
                    for section, value in normalized.items():
                        self.plugin_config[section] = value
                    save_async = getattr(
                        self.plugin_config,
                        "save_config_async",
                        None,
                    )
                    if callable(save_async):
                        await save_async()
                    else:
                        save = getattr(
                            self.plugin_config,
                            "save_config",
                            None,
                        )
                        if callable(save):
                            save()
                except Exception:
                    for section, value in previous.items():
                        if value is missing:
                            self.plugin_config.pop(section, None)
                        else:
                            self.plugin_config[section] = value
                    raise
            persisted = TavernConfig.from_mapping(
                self.plugin_config
            ).to_mapping()
            if persisted != normalized:
                raise RuntimeError("配置保存后回读校验不一致")
            revision = await self.database.record_configuration_revision(
                persisted,
                self._actor(),
            )
            await self.database.write_audit(
                "",
                self._actor(),
                "settings.update",
                "plugin",
                {
                    "admin_count": len(
                        normalized["security"]["admin_ids"]
                    ),
                    "group_count": len(
                        normalized["security"]["allowed_group_ids"]
                    ),
                },
            )
            await self.broker.publish(
                {"type": "settings", "action": "update"}
            )
            return json_response(
                {
                    "settings": persisted,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_export(self):
        try:
            self._username()
            path = await build_backup_archive(
                data_dir=self.data_dir,
                database=self.database,
                export_dir=self.data_dir / "exports",
            )
            return file_response(
                path,
                filename=path.name,
                content_type="application/zip",
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_import(self, mode: str):
        temp_path: Path | None = None
        stage_dir: Path | None = None
        staged_group_files: list[tuple[Path, PurePosixPath]] = []
        try:
            self._username()
            if mode not in {"merge", "replace"}:
                raise ValueError("导入模式必须为 merge 或 replace")
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile):
                raise ValueError("缺少备份文件")
            filename = str(upload.filename or "").lower()
            if not filename.endswith((".json", ".zip")):
                raise ValueError("只接受完整的 Schema 9—12 JSON/ZIP 备份")
            temp_dir = self.data_dir / "imports"
            temp_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".zip" if filename.endswith(".zip") else ".json"
            temp_path = temp_dir / f"{uuid.uuid4().hex}{suffix}"
            await upload.save(temp_path)
            limit = 512 * 1024 * 1024 if suffix == ".zip" else 25 * 1024 * 1024
            if temp_path.stat().st_size > limit:
                raise ValueError(
                    "ZIP 备份不能超过 512 MiB"
                    if suffix == ".zip"
                    else "JSON 备份不能超过 25 MiB"
                )
            if suffix == ".zip":
                with zipfile.ZipFile(temp_path) as archive:
                    _verify_backup_archive(archive)
                    try:
                        info = archive.getinfo("bundle.json")
                    except KeyError as exc:
                        raise ValueError(
                            "ZIP 备份缺少 bundle.json"
                        ) from exc
                    if info.file_size > 25 * 1024 * 1024:
                        raise ValueError("ZIP 内的 bundle.json 过大")
                    bundle = json.loads(
                        archive.read(info).decode("utf-8")
                    )
                    stage_dir = temp_dir / f".stage-{uuid.uuid4().hex}"
                    stage_dir.mkdir(parents=True)
                    staged_group_files = _stage_group_files(
                        archive,
                        stage_dir,
                    )
            else:
                bundle = json.loads(temp_path.read_text(encoding="utf-8"))
            if not isinstance(bundle, dict):
                raise ValueError("备份根节点必须是 JSON 对象")
            counts = await self.database.import_bundle(
                bundle,
                mode,
                self._actor(),
            )
            groups_root = (self.data_dir / "groups").resolve()
            for staged, relative in staged_group_files:
                destination = self.data_dir.joinpath(
                    *relative.parts
                ).resolve()
                if (
                    destination != groups_root
                    and groups_root not in destination.parents
                ):
                    raise ValueError("ZIP 备份含非法群目录路径")
                is_archive = any(
                    part in {"saves", "backups"}
                    for part in relative.parts
                )
                # Active databases and manifests have just been rebuilt from
                # bundle.json by TavernDatabase.import_bundle(). Restoring an
                # older physical copy over them could desynchronise the
                # catalog, so only immutable save/backup archives are copied
                # from the ZIP.
                if not is_archive:
                    continue
                if mode == "merge" and destination.exists():
                    if file_sha256(destination) == file_sha256(staged):
                        continue
                    destination = _collision_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
            await self.broker.publish(
                {"type": "backup", "action": "import", "mode": mode}
            )
            return json_response({"imported": counts})
        except json.JSONDecodeError:
            return error_response("备份 JSON 无法解析", status_code=400)
        except UnicodeDecodeError:
            return error_response("备份文本编码必须为 UTF-8", status_code=400)
        except zipfile.BadZipFile:
            return error_response("ZIP 备份已损坏或格式无效", status_code=400)
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if stage_dir and stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)



    async def session_turn_command(self):
        # A17：后台调整回合顺序（reorder/designate/skip/supersede_choices）。
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = str(payload.get("command") or "").strip().lower()
            if not session_id or not command:
                raise ValueError("缺少 session_id/command")
            await self._require_dm_capability(session_id, user)
            db = self.database
            result: dict[str, Any] = {}
            if command == "reorder":
                order = payload.get("order") or []
                order = [str(x) for x in order if str(x or "").strip()]
                if len(order) < 2 or len(set(order)) != len(order):
                    raise ValueError("行动顺序必须是不重复的玩家 ID 列表")
                await db.supersede_active_choices(session_id, user)
                result = await db.set_turn_order(session_id, order, user)
            elif command == "designate":
                result = await db.designate_turn(
                    session_id, str(payload.get("user_id") or ""), user
                )
            elif command == "skip":
                result = await db.skip_turn(
                    session_id,
                    str(payload.get("user_id") or ""),
                    user,
                    force=True,
                )
            elif command == "supersede_choices":
                result = {
                    "count": await db.supersede_active_choices(session_id, user)
                }
            else:
                raise ValueError("不支持的回合指令")
            await self.broker.publish(
                {"type": "turn", "action": command, "session_id": session_id}
            )
            try:
                session = await db.get_session(session_id)
                turn = await db.get_turn_status(session_id)
                note = (
                    f"🎭 行动顺序已调整（{command}）\n"
                    f"当前行动者："
                    f"{turn.get('current_name') or turn.get('current_user_id') or '—'}"
                )
                if command == "reorder":
                    order_names = [
                        str(x.get("name") or x.get("user_id") or "")
                        for x in turn.get("order", [])
                    ]
                    note += "\n新顺序：" + " → ".join(order_names)
                await self._send_group_text(
                    session_id,
                    str(session.get("unified_origin") or ""),
                    note,
                    kind="turn.command",
                )
            except Exception:
                pass
            return json_response({"ok": True, "result": result})
        except Exception as exc:
            return self._handle_error(exc)

    # ── A16：统一权限助手 ──────────────────────────────────────────
    def _config(self) -> TavernConfig:
        try:
            return TavernConfig.from_mapping(self.plugin_config)
        except Exception:
            return TavernConfig()

    async def _session_control(self, session_id: str) -> dict[str, Any]:
        try:
            return await self.database.get_control_state(session_id)
        except Exception:
            return {"mode": "auto", "active_dm_user_id": ""}

    async def _require_dm_capability(self, session_id: str, user: str) -> None:
        from .permissions import can_manage_dm

        control = await self._session_control(session_id)
        if await can_manage_dm(
            self.database, self._config(), session_id, control, user
        ):
            return
        # A18: WebUI 入口本身受 AstrBot 管理台登录保护（_username 已校验非空），
        # 已认证的 WebUI 操作者视为具备副本 DM / 管理员能力；QQ 身份校验仍保留为
        # 额外放行路径。权限不足改为 403，避免 AstrBot 前端桥把 401 误判为
        # 登录过期而跳转登录页。
        if str(request.username or "").strip():
            return
        raise PolicyRejection("需要副本 DM 或管理员权限")

    async def _require_economy_capability(self, session_id: str, user: str) -> None:
        from .permissions import can_adjust_economy

        control = await self._session_control(session_id)
        if await can_adjust_economy(
            self.database, self._config(), session_id, control, user
        ):
            return
        if str(request.username or "").strip():
            return
        raise PolicyRejection("需要 DM/管理员或 host/mod 权限")

    # ── A16：经济系统 WebUI ───────────────────────────────────────
    async def economy_summary(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            summary = await self.database.economy_summary(session_id)
            return json_response(summary)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_set_enabled(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            enabled = bool(payload.get("enabled", False))
            result = await self.database.set_economy_enabled(
                session_id, enabled, self._actor()
            )
            await self.broker.publish(
                {"type": "economy", "action": "enabled", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_adjust(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_economy_capability(session_id, user)
            result = await self.database.economy_apply(
                session_id=session_id,
                operation_id=str(
                    payload.get("operation_id") or f"web:{uuid.uuid4().hex}"
                ),
                kind=str(payload.get("kind") or "adjust"),
                currency_id=str(payload.get("currency_id") or ""),
                amount=payload.get("amount"),
                from_owner=(
                    (str(payload["from_owner_type"]), str(payload["from_owner_ref"]))
                    if payload.get("from_owner_type") and payload.get("from_owner_ref")
                    else None
                ),
                to_owner=(
                    (str(payload["to_owner_type"]), str(payload["to_owner_ref"]))
                    if payload.get("to_owner_type") and payload.get("to_owner_ref")
                    else None
                ),
                reason=str(payload.get("reason") or ""),
                source="web",
                actor_id=user,
            )
            await self.broker.publish(
                {"type": "economy", "action": "apply", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_transactions(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            limit = int(request.query.get("limit", "100") or "100")
            rows = await self.database.economy_list_transactions(session_id, limit)
            return json_response({"items": rows})
        except Exception as exc:
            return self._handle_error(exc)

    # ── A16：角色托管 / 代操作 WebUI ──────────────────────────────
    async def delegations_list(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            items = await self.database.list_delegations(session_id)
            return json_response({"items": items})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_grant(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            owner = str(payload.get("owner_user_id") or "")
            delegate = str(payload.get("delegate_user_id") or "")
            if not session_id or not owner or not delegate:
                raise ValueError("缺少 session_id/owner_user_id/delegate_user_id")
            source = str(payload.get("source") or "player")
            if source in {"admin", "dm"}:
                await self._require_dm_capability(session_id, user)
            result = await self.database.grant_delegation(
                session_id,
                owner,
                delegate,
                self._actor(),
                permissions=payload.get("permissions"),
                expiry_kind=str(payload.get("expiry_kind") or "none"),
                expires_round=int(payload.get("expires_round") or 0),
                auto_restore=bool(payload.get("auto_restore", False)),
                source=source,
            )
            await self.broker.publish(
                {"type": "delegation", "action": "grant", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_revoke(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            owner = str(payload.get("owner_user_id") or "")
            if not session_id or not owner:
                raise ValueError("缺少 session_id/owner_user_id")
            force = user not in {owner, ""}
            if force:
                await self._require_dm_capability(session_id, user)
            count = await self.database.revoke_delegation(
                session_id, owner, self._actor(), force=force
            )
            await self.broker.publish(
                {"type": "delegation", "action": "revoke", "session_id": session_id}
            )
            return json_response({"count": count})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_restore(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            participant_id = str(payload.get("participant_id") or "")
            if not session_id or not participant_id:
                raise ValueError("缺少 session_id/participant_id")
            await self._require_dm_capability(session_id, user)
            count = await self.database.restore_owner_control(
                session_id, participant_id, self._actor()
            )
            await self.broker.publish(
                {"type": "delegation", "action": "restore", "session_id": session_id}
            )
            return json_response({"count": count})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_forced_choose(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            choice_key = str(payload.get("choice_key") or "")
            if not session_id or not choice_key:
                raise ValueError("缺少 session_id/choice_key")
            await self._require_dm_capability(session_id, user)
            choice_set = await self.database.active_choice_set(session_id)
            participant = (choice_set or {}).get("participant") or {}
            acting_user_id = str(participant.get("group_user_id") or "")
            acting_name = str(
                participant.get("character_name")
                or participant.get("display_name")
                or acting_user_id
            )
            if not acting_user_id:
                raise ValueError("当前没有可强制选择的行动角色")
            # A17：幂等——同一 operation_id 只执行一次，防止重复点击重复消费。
            operation_id = str(payload.get("operation_id") or "").strip() or (
                f"forced:{session_id}:{uuid.uuid4().hex}"
            )
            claim = await self.database.claim_action_operation(
                session_id,
                operation_id,
                "forced_choose",
                acting_user_id,
                user,
                {"choice_key": choice_key},
            )
            if not claim["claimed"]:
                return json_response(
                    {
                        "ok": False,
                        "idempotent_replay": True,
                        "message": "该操作已执行过，未重复提交",
                    }
                )
            session = await self.database.get_session(session_id)
            from types import SimpleNamespace

            event = SimpleNamespace(
                unified_msg_origin=str(session.get("unified_origin", "")),
                message_obj=None,
            )
            reply = await self._engine().process_choice(
                event=event,
                session_id=session_id,
                sender_id=user,
                sender_name="管理员",
                choice_key=choice_key,
                operator_id=user,
                force=True,
            )
            await self.broker.publish(
                {
                    "type": "delegation",
                    "action": "forced_choose",
                    "session_id": session_id,
                    "hook": "forced_choose",
                }
            )
            # A17/A21：后台代选成功后向群聊发送通知（失败不影响已提交操作，
            # 但把发送结果如实返回给前端，避免误报“已通知群聊”）。
            selected_text = ""
            for choice_item in (choice_set.get("choices") or []):
                if isinstance(choice_item, Mapping) and str(
                    choice_item.get("key") or ""
                ).upper() == str(choice_key).upper():
                    selected_text = str(choice_item.get("text") or "")
                    break
            notice = (
                f"🎭 后台代操作\n角色：{acting_name}\n"
                f"操作者：{user}\n选择：{choice_key}. {selected_text}".rstrip()
            )
            parts = [part for part in (reply.story_text, reply.turn_text) if part]
            group_text = notice + (("\n\n" + "\n\n".join(parts)) if parts else "")
            send_result = await self._send_group_text(
                session_id,
                str(session.get("unified_origin") or ""),
                group_text,
                kind="delegation.forced_choose",
            )
            return json_response(
                {
                    "ok": True,
                    "operation_id": operation_id,
                    "story": reply.story_text,
                    "turn": reply.turn_text,
                    "actor_user_id": acting_user_id,
                    "operator_id": user,
                    "notice_sent": bool(send_result.get("ok")),
                    "notice_reason": str(send_result.get("reason") or ""),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def _send_group_text(
        self,
        session_id: str,
        origin: str,
        text: str,
        *,
        kind: str = "webui.notice",
    ) -> dict[str, Any]:
        """Send portable text or persist it for delivery on the next event."""

        config = TavernConfig.from_mapping(self.plugin_config)
        text = render_command_text(text, config.primary_command_trigger)
        policy = "next_event"
        try:
            instance = await self.database.get_instance_config(session_id)
            policy = str(
                normalize_chat_experience(instance.get("world_snapshot") or {})
                ["delivery"]["proactive_fallback"]
            )
        except Exception:
            policy = "next_event"
        result = await deliver_text(self.context, origin, text, proactive=True)
        payload = result.to_dict()
        payload["queued"] = False
        if result.ok:
            return payload
        if policy == "discard":
            payload["reason"] = result.reason + "；世界包策略已丢弃未送达通知"
            return payload
        if str(origin or "").strip() and str(text or "").strip():
            stored_kind = f"webui_only:{kind}" if policy == "webui_only" else kind
            queued = await self.database.queue_delivery(
                session_id=session_id,
                origin=origin,
                kind=stored_kind,
                text=text,
                reason=result.reason,
            )
            payload["queued"] = True
            payload["delivery_id"] = queued["id"]
            payload["reason"] = result.reason + "；已进入待投递队列"
            await self.broker.publish(
                {"type": "delivery", "action": "queued", "session_id": session_id}
            )
        return payload

    async def deliveries(self):
        """List, retry or dismiss persisted text notifications."""

        try:
            user = self._username()
            method = str(getattr(request, "method", "GET") or "GET").upper()
            if method == "GET":
                session_id = str(request.query.get("session_id", "") or "")
                status = str(request.query.get("status", "pending") or "")
                items = await self.database.list_deliveries(
                    session_id=session_id,
                    status=status,
                    limit=int(request.query.get("limit", "100") or 100),
                )
                return json_response({"items": items})
            payload = await self._payload()
            delivery_id = str(payload.get("delivery_id") or "")
            action = str(payload.get("action") or "retry")
            if not delivery_id:
                raise ValueError("缺少 delivery_id")
            items = await self.database.list_deliveries(status="", limit=500)
            item = next((row for row in items if str(row.get("id")) == delivery_id), None)
            if not item:
                raise ValueError("待投递通知不存在")
            if item.get("session_id"):
                await self._require_dm_capability(str(item["session_id"]), user)
            if action == "dismiss":
                updated = await self.database.dismiss_delivery(delivery_id, self._actor())
                await self.broker.publish({"type": "delivery", "action": "dismissed", "session_id": item.get("session_id", "")})
                return json_response({"ok": True, "item": updated})
            result = await deliver_text(
                self.context,
                item.get("origin"),
                item.get("text"),
                proactive=True,
            )
            updated = await self.database.finish_delivery(
                delivery_id,
                success=result.ok,
                error=result.reason,
            )
            return json_response({"ok": result.ok, "item": updated, "delivery": result.to_dict()})
        except Exception as exc:
            return self._handle_error(exc)

    def _engine(self):
        return getattr(self, "_tavern_engine", None)

    # ── A16：人工 DM 控制台 ───────────────────────────────────────
    async def dm_console_state(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            control = await self.database.get_control_state(session_id)
            delegations = await self.database.list_delegations(session_id)
            pending = await self.database.pending_operations(session_id)
            return json_response(
                {
                    "session": {
                        "id": session.get("id"),
                        "state": session.get("state"),
                        "revision": session.get("revision"),
                        "turn_no": session.get("turn_no"),
                        "input_locked": int(session.get("input_locked") or 0),
                    },
                    "control": control,
                    "delegations": delegations,
                    "pending_operations": pending,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dm_command(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = str(payload.get("command") or "")
            if not session_id or not command:
                raise ValueError("缺少 session_id/command")
            await self._require_dm_capability(session_id, user)
            database = self.database
            actor = self._actor()
            instance = await database.get_instance_config(session_id)
            dm_policy = normalize_chat_experience(
                instance.get("world_snapshot") or {}
            )["dm"]
            required_policy = {
                "narrative": "allow_narrative_override",
                "whisper": "allow_secret_whispers",
                "manual_roll": "allow_manual_checks",
                "adjust_relationship": "allow_state_intervention",
                "adjust_economy": "allow_state_intervention",
                "set_next_actor": "allow_state_intervention",
                "lock_action": "allow_state_intervention",
                "lock_input": "allow_state_intervention",
                "replace_choices": "allow_state_intervention",
                "force_end_vote": "allow_state_intervention",
                "vote_as": "allow_state_intervention",
            }.get(command)
            if required_policy and not bool(dm_policy.get(required_policy, True)):
                raise PermissionError(
                    f"当前世界包已关闭该人工 DM 能力（{required_policy}）"
                )
            result: dict[str, Any] = {}
            if command == "narrative":
                result = await database.insert_dm_narrative(
                    session_id,
                    str(payload.get("text") or ""),
                    actor,
                    mode=str(payload.get("mode") or "append"),
                )
            elif command == "announce":
                result = await database.publish_announcement(
                    session_id, str(payload.get("text") or ""), actor
                )
            elif command == "whisper":
                result = await database.whisper_to(
                    session_id,
                    str(payload.get("text") or ""),
                    str(payload.get("participant_id") or ""),
                    actor,
                )
            elif command == "set_next_actor":
                result = await database.designate_turn(
                    session_id, str(payload.get("user_id") or ""), actor
                )
            elif command == "lock_action":
                result = await database.set_action_lock(
                    session_id,
                    str(payload.get("participant_id") or ""),
                    bool(payload.get("locked", True)),
                    actor,
                )
            elif command == "lock_input":
                result = await database.set_input_lock(
                    session_id, bool(payload.get("locked", True)), actor
                )
            elif command == "replace_choices":
                import json as _json

                raw = payload.get("choices")
                choices = raw if isinstance(raw, list) else _json.loads(
                    str(payload.get("choices_json") or "[]")
                )
                choice_set = await database.active_choice_set(session_id)
                if not choice_set:
                    raise ValueError("当前没有可替换的选项")
                result = await database.replace_active_choices(
                    session_id,
                    choice_set["participant"]["id"],
                    choices,
                    actor_id=actor,
                )
            elif command == "force_end_vote":
                result = await database.force_end_vote(
                    session_id, str(payload.get("winner_key") or ""), actor
                )
            elif command == "vote_as":
                result = await database.cast_vote(
                    session_id,
                    str(payload.get("user_id") or ""),
                    str(payload.get("key") or ""),
                )
            elif command == "manual_roll":
                result = await database.record_manual_roll(
                    session_id,
                    str(payload.get("participant_id") or ""),
                    str(payload.get("stat") or ""),
                    int(payload.get("total") or 0),
                    str(payload.get("note") or ""),
                    actor,
                )
            elif command == "adjust_relationship":
                result = await database.apply_relationship_delta(
                    session_id,
                    str(payload.get("source") or ""),
                    str(payload.get("target") or ""),
                    str(payload.get("dimension") or "信任"),
                    int(payload.get("delta") or 0),
                    actor,
                )
            elif command == "adjust_economy":
                await self._require_economy_capability(session_id, user)
                result = await database.economy_apply(
                    session_id=session_id,
                    operation_id=str(
                        payload.get("operation_id") or f"dm:{uuid.uuid4().hex}"
                    ),
                    kind=str(payload.get("kind") or "adjust"),
                    currency_id=str(payload.get("currency_id") or ""),
                    amount=payload.get("amount"),
                    from_owner=(
                        (str(payload["from_owner_type"]), str(payload["from_owner_ref"]))
                        if payload.get("from_owner_type") and payload.get("from_owner_ref")
                        else None
                    ),
                    to_owner=(
                        (str(payload["to_owner_type"]), str(payload["to_owner_ref"]))
                        if payload.get("to_owner_type") and payload.get("to_owner_ref")
                        else None
                    ),
                    reason=str(payload.get("reason") or ""),
                    source="dm",
                    actor_id=user,
                )
            elif command == "pause":
                result = await database.transition_session(
                    session_id, "paused", actor
                )
            elif command == "resume":
                result = await database.transition_session(
                    session_id, "preparing", actor
                )
            elif command == "checkpoint":
                result = await database.create_snapshot(
                    session_id,
                    str(payload.get("name") or "DM检查点"),
                    actor,
                )
            elif command == "cancel_operation":
                result = await database.update_operation(
                    str(payload.get("operation_id") or ""),
                    status="failed",
                    phase="cancelled_by_dm",
                    result={"reason": str(payload.get("reason") or "DM 取消卡死任务")},
                    actor_id=actor,
                )
            else:
                raise ValueError(f"不支持的 DM 指令：{command}")
            delivery: dict[str, Any] | None = None
            session = await database.get_session(session_id)
            public_text = ""
            target_origin = str(session.get("unified_origin") or "")
            if command == "narrative":
                public_text = str(payload.get("text") or "")
            elif command == "announce":
                public_text = "【主持公告】\n" + str(payload.get("text") or "")
            elif command == "manual_roll":
                public_text = (
                    f"【主持检定】{payload.get('stat') or '检定'}："
                    f"{int(payload.get('total') or 0)}"
                )
            elif command == "whisper":
                participant = await database.get_participant(
                    session_id,
                    participant_ref=str(payload.get("participant_id") or ""),
                )
                target_origin = str(participant.get("private_origin") or "")
                public_text = "【主持密语】\n" + str(payload.get("text") or "")
            if public_text:
                delivery = await self._send_group_text(
                    session_id,
                    target_origin,
                    public_text,
                    kind=f"dm.{command}",
                )
            await self.broker.publish(
                {"type": "dm", "action": command, "session_id": session_id}
            )
            return json_response({"ok": True, "result": result, "delivery": delivery})
        except Exception as exc:
            return self._handle_error(exc)


    async def session_token_reset(self):
        """A16：重置副本 Token 统计（不删除剧情；管理员/DM）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            count = await self.database.reset_token_usage(session_id, self._actor())
            return json_response({"ok": True, "count": count})
        except Exception as exc:
            return self._handle_error(exc)

    # ═══ A14：元素反应与通用接口 ═══════════════════════════════════════
    async def world_element_reaction(self):
        """解析一次「属性元素反应」（只读干跑，不写状态）。"""
        try:
            self._username()
            payload = await self._payload()
            world_ref = str(
                payload.get("world_id") or payload.get("id") or payload.get("slug") or ""
            )
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world")
            )
            if not isinstance(world, dict):
                raise ValueError("请提供 world_id 或内联 world")
            parsed = parse_elemental(world)
            resolver = None
            resolver_name = str(parsed.get("resolver") or "")
            if resolver_name and self._extension_registry is not None:
                resolver = self._extension_registry.resolve(
                    "element_resolver", resolver_name
                )
            result = resolve_elemental(
                parsed,
                str(payload.get("source") or ""),
                str(payload.get("target") or ""),
                target_element=str(payload.get("target_element") or "") or None,
                context=payload.get("context") or {},
                resolver=resolver,
            )
            return json_response(
                {
                    "reaction": result,
                    "table": elemental_table(world),
                    "resolver_used": resolver_name or "table",
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_element_table(self):
        """世界元素表（元素 / 亲和 / 反应 / 解析器）。"""
        try:
            self._username()
            world_id = str(request.query.get("id", "") or "")
            if not world_id:
                raise ValueError("缺少世界标识")
            world = await self.database.get_world(world_id)
            return json_response({"table": elemental_table(world)})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_schema(self):
        """世界包 v5 自描述结构（供第三方工具生成合法世界包）。"""
        try:
            self._username()
            return json_response(
                {
                    "schema_version": WORLD_SCHEMA_VERSION,
                    "elemental_contract_version": parse_elemental({})["version"],
                    "top_level_fields": [
                        "slug",
                        "name",
                        "description",
                        "system_prompt",
                        "opening_scene",
                        "initial_state",
                        "characters",
                        "world_schema_version",
                        "minimum_plugin_version",
                        "protocol",
                        "rules",
                        "elemental",
                    ],
                    "rules": {
                        "character_card": {
                            "version": "integer",
                            "auto_approve": "boolean",
                            "edit_requires_review": "boolean",
                            "fields": [{"key": "string", "label": "string", "required": "boolean", "private": "boolean", "max_chars": "integer"}],
                            "stats": {
                                "mode": "none|manual|preset|preset_stack",
                                "budget": "integer",
                                "attributes": [{"key": "string", "label": "string", "minimum": "integer", "maximum": "integer", "default": "integer"}],
                                "modifier_table": {"integer": "integer"},
                            },
                        },
                        "resolution": {
                            "mode": sorted(RESOLUTION_MODES),
                            "dice_system": "string (registered dice system)",
                            "allowed_attributes": ["string"],
                        },
                        "player_limits": {
                            "recommended_min": "integer",
                            "recommended_max": "integer",
                            "minimum_start": "integer",
                            "maximum": "integer",
                        },
                        "strict_choices": "boolean",
                        "opening_choices": ["{key, text, risk, requires_check}"],
                    },
                    "elemental": {
                        "elements": ["string"],
                        "affinities": {"target_ref": {"element": "number -2..2"}},
                        "reactions": [{"a": "element", "b": "element", "result": "string", "effect": "operation"}],
                        "resolver": "string (registered element_resolver)",
                    },
                    "protocol": {
                        "core_version": "integer",
                        "features": {"feature_id": "version"},
                        "required_features": ["string"],
                        "id_aliases": {"id": "alias"},
                        "numeric_policies": {"key": {"min": "number", "max": "number", "overflow": "reject|clamp"}},
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_export(self):
        """导出单个世界包为可下载 JSON。"""
        try:
            self._username()
            world_id = str(
                request.query.get("id", "") or request.query.get("slug", "") or ""
            )
            if not world_id:
                raise ValueError("缺少世界标识")
            world = await self.database.get_world(world_id)
            payload = world_import_payload(world)
            export_dir = self.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            slug = str(world.get("slug") or "world")
            path = next_timestamped_path(export_dir, f"{slug}.world", ".json")
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            return file_response(
                path,
                filename=path.name,
                content_type="application/json",
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_diff(self):
        """两个世界包（或版本）的字段级差异。"""
        try:
            self._username()
            payload = await self._payload()
            left_id = str(payload.get("left_id") or "")
            right_id = str(payload.get("right_id") or "")
            left = (
                await self.database.get_world(left_id) if left_id else payload.get("left")
            )
            right = (
                await self.database.get_world(right_id) if right_id else payload.get("right")
            )
            if not isinstance(left, dict) or not isinstance(right, dict):
                raise ValueError("请提供 left_id/right_id 或内联 left/right")
            return json_response(
                {
                    "changes": _deep_diff(
                        world_import_payload(left), world_import_payload(right)
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_import_batch(self):
        """批量导入世界包（create_only / upsert）。"""
        try:
            self._username()
            payload = await self._payload()
            worlds = payload.get("worlds")
            if not isinstance(worlds, list) or not worlds:
                raise ValueError("worlds 必须是世界包数组")
            if len(worlds) > 50:
                raise ValueError("单次批量导入上限 50 个")
            mode = str(payload.get("mode") or "create_only")
            if mode not in {"create_only", "upsert"}:
                raise ValueError("mode 必须是 create_only 或 upsert")
            results: list[dict[str, Any]] = []
            for index, world in enumerate(worlds):
                entry: dict[str, Any] = {"index": index}
                if not isinstance(world, dict):
                    entry["error"] = "世界包必须是 JSON 对象"
                    results.append(entry)
                    continue
                slug = str(world.get("slug") or "")
                entry["slug"] = slug
                try:
                    report = inspect_world_package(world)
                    if not report["compatible"]:
                        messages = [
                            item["message"]
                            for item in report["issues"]
                            if item["level"] == "error"
                        ]
                        entry["error"] = "体检未通过：" + "；".join(messages[:3])
                        results.append(entry)
                        continue
                    import_payload = world_import_payload(world)
                    existing = None
                    try:
                        existing = await self.database.get_world(slug)
                    except DatabaseNotFoundError:
                        existing = None
                    if existing and mode == "create_only":
                        entry["mode"] = "skipped"
                        entry["reason"] = "slug 已存在（create_only）"
                        results.append(entry)
                        continue
                    if existing:
                        import_payload["id"] = existing["id"]
                        import_payload["revision"] = existing["revision"]
                    item = await self.database.save_world(import_payload, self._actor())
                    if item.get("slug"):
                        self._purge_rule_runtime(str(item["slug"]))
                    entry["id"] = item.get("id")
                    entry["mode"] = "updated" if existing else "created"
                except Exception as exc:
                    entry["error"] = str(exc)[:300]
                results.append(entry)
            await self.broker.publish({"type": "world", "action": "batch_import"})
            imported = sum(
                1
                for r in results
                if r.get("mode") in {"created", "updated"}
            )
            return json_response({"results": results, "imported": imported})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_simulate_batch(self):
        """批量规则干跑（worlds/simulate 的批处理版）。"""
        try:
            self._username()
            payload = await self._payload()
            runs = payload.get("runs")
            if not isinstance(runs, list) or not runs:
                raise ValueError("runs 必须是数组")
            if len(runs) > 20:
                raise ValueError("单次批量模拟上限 20")
            results: list[dict[str, Any]] = []
            for index, run in enumerate(runs):
                entry: dict[str, Any] = {"index": index}
                if not isinstance(run, dict):
                    entry["error"] = "run 必须是对象"
                    results.append(entry)
                    continue
                try:
                    world_ref = str(run.get("world_ref") or "")
                    world = (
                        await self.database.get_world(world_ref)
                        if world_ref
                        else run.get("world")
                    )
                    if not isinstance(world, dict):
                        raise ValueError("缺少 world_ref 或 world")
                    if _json_size(world) > _WORLD_SIMULATE_MAX_BYTES:
                        raise ValueError("世界包过大")
                    if _json_depth(world):
                        raise ValueError("世界包嵌套过深")
                    report = inspect_world_package(world)
                    if not report["compatible"]:
                        raise ValueError("世界包体检未通过")
                    intent = run.get("intent")
                    context = run.get("context", {})
                    if not isinstance(intent, dict) or not isinstance(context, dict):
                        raise ValueError("intent/context 必须是 JSON 对象")
                    entry["slug"] = world.get("slug")
                    entry["result"] = self._cached_rule_runtime(world).resolve_action_intent(
                        intent,
                        context,
                        dry_run=True,
                        world_snapshot_id=f"preview:{world.get('slug', '')}",
                    )
                except Exception as exc:
                    entry["error"] = str(exc)[:300]
                results.append(entry)
            return json_response({"results": results})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_resolution_table(self):
        """世界裁定表（解析模式 / 角色卡 / 能力 / 元素 / 限制）。"""
        try:
            self._username()
            world_id = str(request.query.get("id", "") or "")
            if not world_id:
                raise ValueError("缺少世界标识")
            world = await self.database.get_world(world_id)
            contract: dict[str, Any] = {}
            try:
                from .world_contract import validate_world_contract

                contract = validate_world_contract(world)
            except Exception:
                contract = {}
            rules = world.get("rules") if isinstance(world.get("rules"), dict) else {}
            return json_response(
                {
                    "resolution": contract.get("resolution", {}),
                    "stats": contract.get("stats", {}),
                    "capabilities": contract.get("capabilities", {}),
                    "protocol": contract.get("protocol", {}),
                    "player_limits": rules.get("player_limits", {}),
                    "elemental": elemental_table(world),
                    "check_modifiers": (world.get("initial_state") or {}).get(
                        "check_modifiers", {}
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_turn_preflight(self):
        """行动前预检（只读）：当前行动者 / 选项 / 投票 / 等待流程。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            turn = await self.database.get_turn_status(session_id)
            choice_set = await self.database.active_choice_set(session_id)
            vote = await self.database.active_vote(session_id)
            waiting_for = "vote" if vote else (
                "choice" if choice_set else (
                    "preparation" if session.get("state") == SESSION_PREPARING else (
                        "admin" if session.get("state") == SESSION_PAUSED else ""
                    )
                )
            )
            options: list[dict[str, Any]] = []
            if choice_set and isinstance(choice_set, dict):
                raw_choices = choice_set.get("choices_json") or choice_set.get("choices") or []
                if isinstance(raw_choices, list):
                    for item in raw_choices:
                        if isinstance(item, dict):
                            options.append(
                                {
                                    "key": str(item.get("key") or ""),
                                    "text": str(item.get("text") or ""),
                                    "risk": str(item.get("risk") or ""),
                                    "requires_check": bool(
                                        item.get("requires_check") or item.get("check")
                                    ),
                                }
                            )
            return json_response(
                {
                    "session": {
                        "id": session.get("id"),
                        "state": session.get("state"),
                        "turn_no": session.get("turn_no"),
                        "revision": session.get("revision"),
                        "world_name": session.get("world_name"),
                    },
                    "turn": turn,
                    "active_choices": options,
                    "active_vote": vote,
                    "waiting_for": waiting_for,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_context_compile(self):
        """上下文编译调试快照（只读，不调用模型）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            turn = await self.database.get_turn_status(session_id)
            events = await self.database.recent_events(session_id, 20)
            roster = await self.database.list_roster(session_id)
            memories = await self.database.list_memories(
                session_id, "", 50, include_invalidated=False
            )
            instance = await self.database.get_instance_config(session_id)
            snapshot = instance.get("world_snapshot") or {}
            config = TavernConfig.from_mapping(self.plugin_config)
            world_state = session.get("world_state") or {}
            return json_response(
                {
                    "session": {
                        "id": session.get("id"),
                        "state": session.get("state"),
                        "turn_no": session.get("turn_no"),
                        "revision": session.get("revision"),
                    },
                    "turn": turn,
                    "location": (world_state or {}).get("location", ""),
                    "recent_events": [
                        {
                            "role": e.get("role"),
                            "content": str(e.get("content") or "")[:200],
                            "created_at": e.get("created_at"),
                        }
                        for e in events
                        if isinstance(e, dict)
                    ],
                    "roster_summary": [
                        {
                            "character_name": r.get("character_name") or r.get("display_name"),
                            "card_status": r.get("card_status"),
                            "ready": bool(r.get("ready")),
                        }
                        for r in roster
                        if isinstance(r, dict)
                    ],
                    "memory_count": len(memories),
                    "world_snapshot": {
                        "name": snapshot.get("name"),
                        "slug": snapshot.get("slug"),
                        "revision": snapshot.get("revision"),
                    },
                    "prompt_budget": {
                        "recent_turns": config.recent_turns,
                        "memory_limit": config.memory_limit,
                        "max_input_chars": config.max_input_chars,
                        "max_output_chars": config.max_output_chars,
                        "temperature": config.temperature,
                        "max_tokens": config.max_tokens,
                        "two_phase_checks": config.two_phase_checks,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_inject_fact(self):
        """向受控世界状态注入一条事实（安全快照 + 审计 + 修订号）。"""
        try:
            self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            fact = str(payload.get("fact") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id")
            if not fact:
                raise ValueError("fact 不能为空")
            if len(fact) > 2000:
                raise ValueError("fact 过长（上限 2000 字）")
            session = await self.database.get_session(session_id)
            world_state = dict(session.get("world_state") or {})
            facts = list(world_state.get("facts") or [])
            facts = [str(item) for item in facts if isinstance(item, str)]
            added = fact not in facts
            if added:
                facts.append(fact)
                world_state["facts"] = facts[-500:]
                await self.database.save_manual_state(
                    session_id,
                    world_state,
                    int(session["revision"]),
                    self._actor(),
                )
            await self.database.write_audit(
                session_id,
                self._actor(),
                "world.fact.inject",
                "",
                {"fact": fact[:200], "added": added},
            )
            await self.broker.publish(
                {"type": "session", "action": "inject_fact", "session_id": session_id}
            )
            return json_response(
                {
                    "session_id": session_id,
                    "fact": fact,
                    "added": added,
                    "fact_count": len(world_state.get("facts") or []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_apply_effect(self):
        """校验并干跑一批声明式操作（默认不落库；commit=true 仅写审计）。"""
        try:
            self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            operations = payload.get("operations")
            if not session_id:
                raise ValueError("缺少 session_id")
            if not isinstance(operations, list) or not operations:
                raise ValueError("operations 必须是操作数组")
            instance = await self.database.get_instance_config(session_id)
            world = instance.get("world_snapshot") or {}
            runtime = self._cached_rule_runtime(world)
            validated = runtime.operations.validate(operations)
            session = await self.database.get_session(session_id)
            state = {"world": dict(session.get("world_state") or {})}
            _, changes, narrative = runtime.operations.apply(
                validated, state, dry_run=True
            )
            commit = bool(payload.get("commit"))
            if commit:
                summary = "；".join(
                    str(change.get("path") or change.get("op") or "")
                    for change in changes[:10]
                )
                await self.database.write_audit(
                    session_id,
                    self._actor(),
                    "world.effect.dry_run_commit",
                    "",
                    {
                        "operation_count": len(validated),
                        "changes": summary[:500],
                    },
                )
            return json_response(
                {
                    "validated": validated,
                    "changes": changes,
                    "narrative": narrative,
                    "committed": commit,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_advance_clock(self):
        """推进场景时钟（校验 + 审计 + 修订）。"""
        try:
            self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            clock_id = str(payload.get("clock_id") or "")
            segments = payload.get("segments")
            if not session_id or not clock_id:
                raise ValueError("缺少 session_id 或 clock_id")
            try:
                delta = int(segments)
            except (TypeError, ValueError):
                raise ValueError("segments 必须是整数")
            result = await self.database.advance_scene_clock(
                session_id,
                clock_id,
                delta,
                self._actor(),
                str(payload.get("note") or ""),
            )
            await self.broker.publish(
                {"type": "session", "action": "advance_clock", "session_id": session_id}
            )
            return json_response({"clock": result})
        except Exception as exc:
            return self._handle_error(exc)


    async def extensions(self):
        """已注册扩展点清单（逐项隔离并保证响应可 JSON 序列化）。"""
        try:
            self._username()
            items, errors = _extension_catalog(self._extension_registry)
            return json_response(
                {
                    "kinds": items,
                    "total": sum(len(names) for names in items.values()),
                    "errors": errors,
                    "partial": bool(errors),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def hook_events(self):
        """可订阅事件目录与当前订阅数（逐字段容错）。"""
        try:
            self._username()
            subscriptions, errors = _hook_catalog(self.hooks)
            return json_response(
                {
                    "supported": _safe(lambda: sorted(HOOK_EVENTS), []),
                    "subscriptions": subscriptions,
                    "subscribed_count": sum(subscriptions.values()),
                    "errors": errors,
                    "partial": bool(errors),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def meta_capabilities(self):
        """运行时自描述能力清单（逐字段容错）。"""
        try:
            self._username()
            return json_response(
                {
                    "world_schema_version": _safe(
                        lambda: WORLD_SCHEMA_VERSION, 0
                    ),
                    "elemental_contract_version": _safe(
                        lambda: parse_elemental({})["version"], "1.0"
                    ),
                    "resolution_modes": _safe(
                        lambda: sorted(RESOLUTION_MODES), []
                    ),
                    "operation_types": _safe(
                        lambda: sorted(OPERATION_TYPES), []
                    ),
                    "persistence_scopes": _safe(
                        lambda: sorted(PERSISTENCE_SCOPES), []
                    ),
                    "extension_kinds": _safe(
                        lambda: sorted(ExtensionRegistry._KINDS), []
                    ),
                    "hook_events": _safe(lambda: sorted(HOOK_EVENTS), []),
                    "platforms": _safe(capability_matrix, []),
                    "delivery_mode": "plain_text_with_persistent_fallback",
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def events(self):
        try:
            self._username()

            async def stream():
                async for item in self.broker.subscribe():
                    yield "data: " + json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n\n"

            return stream_response(stream())
        except Exception as exc:
            return self._handle_error(exc)
