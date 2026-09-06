from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from ..config import TavernConfig
from ..constants import (
    DATABASE_SCHEMA_VERSION,
    MANAGEMENT_ACTIONS,
    MUTATING_ACTIONS,
    PLAYER_ACTIONS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from ..database import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from ..engine import (
    TavernBusyError,
    TavernEngine,
    TavernEngineError,
    TavernOperationCancelled,
    TavernPlayerDisabledError,
    TavernStoryGenerationError,
    TavernTurnOrderError,
    story_generation_failure_message,
)
from ..ai_companions import AiCompanionTurnRunner
from ..bootstrap import build_runtime
from ..help_topics import contextual_help
from ..recaps import build_recap
from ..operations import operation_key, transport_event_id
from ..lifecycle import (
    attribute_maps,
    card_stat_allocation,
    find_profession_preset,
    format_choices,
    normalize_time_rules,
    parse_choice_input,
    parse_duration,
    player_limits,
    resolve_profession_stats,
    uses_profession_preset_stats,
)
from ..security import (
    ParsedCommand,
    parse_story_trigger,
    parse_tavern_command,
    validate_platform_id,
)
from ..backup_service import build_backup_archive, prune_backups
from ..platform_delivery import (
    markdown_supported,
    normalize_platform,
)
from ..delivery import (
    AUDIENCE_GROUP,
    AUDIENCE_PRIVATE_OWNER,
    DeliveryService,
    DeliveryTarget,
    OutboxWorker,
    TARGET_KIND_GROUP,
    TARGET_KIND_PRIVATE,
)
from ..builtin_worlds import (
    builtin_world_spec_by_key,
    builtin_world_specs,
    resolve_builtin_archive,
    validate_installed_builtin,
)
from ..copy import render_message
from ..chat_experience import normalize_chat_experience
from ..host_lifecycle import BackgroundTaskSupervisor
from ..remote_panel import start_panel_server
from ..supplement import parse_supplement_reply, supplement_list_line
from ..entry import from_astrbot_event
from ..commands.card_commands import (
    PRIVATE_CARD_ACTIONS,
    CardCommandService,
)
from ..commands.models import (
    INTENT_CANDIDATE_BUNDLE,
    INTENT_GROUP_NOTICE,
    INTENT_PRIVATE_REPLY,
    INTENT_PERSIST_VERIFIED_TARGET,
    INTENT_REVOKE_PRIVATE_TARGET,
    CommandResult,
)
from ..commands.session_commands import (
    INTENT_BROKER_EVENT,
    INTENT_SYNC_GROUP_TARGET,
    SESSION_ACTIONS,
    format_gameplay_brief,
    SessionCommandService,
)
from ..commands.admin_commands import AdminCommandService
from ..commands.growth_commands import GrowthCommandService
from ..commands.dm_commands import DMCommandService, DMRequest
from ..commands.turn_commands import (
    CommandResult as TurnCommandResult,
    RequestContext as TurnRequestContext,
    TurnCommandHandler,
)
from ..commands.vote_commands import (
    VoteCommandHandler,
    render_vote_outcome,
    render_vote_resolution_success,
)
from ..commands.world_commands import WorldCommandService
from ..commands.tactical_commands import TACTICAL_ACTIONS, TacticalCommandService
from ..commands.challenge_commands import CHALLENGE_ACTIONS, ChallengeCommandService
from ..runtime.command_router import ApplicationRouter, CommandSpec
from ..runtime.contracts import CommandResult as ApplicationCommandResult
from ..runtime.orchestrator import ApplicationCommandOrchestrator
from ..runtime.request import RequestContext
from ..runtime.tendency_service import TendencyApplicationService
from ..bot_result_renderer import render_bot_result
from ..messaging import render_message_type
from ..messaging.player import (
    PlayerMessage,
    prepare_player_output,
    render_player_message,
    render_player_text,
    qqbot_markdown_for_event,
)
from ..messaging.turn_bundle import (
    TurnMessageBundle,
    deserialize_player_message,
    reply_message_parts,
    serialize_player_message,
    split_turn_bundle_for_delivery,
)
from ..messaging.delivery_parts import send_ordered_parts
from ..workers import (
    AuthorJobWorker,
    EventOutboxWorker,
    StorageSyncWorker,
)


INSTANCE_LIST_PAGE_SIZE = 5
INSTANCE_INTRO_MAX_CHARS = 220
REVIEW_LIST_PAGE_SIZE = 5
# 计时轮询与通知频控
TIMER_POLL_INTERVAL_SECONDS = 15
# 同一个计时器在该窗口内只允许推送一次，防止重复行造成刷屏。
TIMER_NOTICE_DEDUP_SECONDS = 25
# 相邻两条主动通知之间的最小间隔，规避 QQ 官方主动消息频控。
TIMER_NOTICE_MIN_GAP_SECONDS = 2.0
# 自动备份轮询间隔（秒）：每小时最多检查 60 次，配合 interval_hours 生效。
BACKUP_POLL_SECONDS = 60
PRIVATE_ONLY_CARD_ACTIONS = PRIVATE_CARD_ACTIONS - {"card"}
_INSTANCE_PAGE_PATTERNS = (
    re.compile(r"^第\s*(\d{1,6})\s*页$"),
    re.compile(r"^页\s*(\d{1,6})$"),
    re.compile(r"^列表\s*(\d{1,6})$"),
)


HELP_TEXT_TEMPLATE = f"""\
【321开团 v{PLUGIN_VERSION}｜TWP 世界协议、全平台文本跑团与真人主持】
主持：/团 开启 <副本> → /团 开演
恢复：/团 暂停 → /团 恢复 → 全员准备 → /团 继续
玩家：/团 加入｜角色｜准备｜阵容｜暂离｜返回队列｜退出
建卡：私聊 /团 建卡 <验证码>｜当前步骤｜上一步｜修改 <字段>
AI代写：/团 随机｜/团 补全 <初始设定>（建卡私聊中可用）
网页建卡：/团 网页建卡（私聊签发链接，浏览器逐项填写）
改名：/团 修改角色名 <名称>｜/团 修改昵称 <昵称>
草稿：/团 重新建卡｜取消建卡｜放弃席位 确认
回合：{{prefix}} A｜/团 选择 A｜/团 重整选项
物资：{{prefix}} 道具 <名称>｜{{prefix}} 技能 <名称>｜/团 赠予 <道具> <目标>
商店：/团 商店｜/团 购买 <商品>
裁定：/团 灵感｜/团 灵感 A 优势｜/团 灵感重投 A
集体：/团 投票 A（不消耗个人行动）
命运：/团 命运预览｜/团 命运确认 <编号>｜/团 命运拒绝 <编号>
救援：/团 救援 <角色完整名称或副本昵称>
记录：/团 回顾｜存档列表｜存档 <名称>｜删档 <名称>｜读档｜回滚
管理：审核｜强制全员准备｜AI队友 <数量> <确认|自动|暂停>｜倒计时｜用量｜限额｜移至｜指定
战术：战况｜行动/防守/援助/撤退/谈判｜锁定行动｜推进战术｜纠正战术｜结束战术
挑战：挑战｜挑战行动｜退出挑战｜挑战谈判｜确认挑战｜推进挑战｜结束挑战
主持：/团 主持 开启｜指引｜推进｜直述｜交棒｜自动｜状态｜接管
帮助：/团 帮助 建卡｜回合｜投票｜回顾｜管理
安全：任一出场玩家可发送 /团 安全暂停
结束：/团 关闭｜/团 完结 确认｜/团 强制终止 确认 <原因>

普通群聊默认旁路；活动挑战或战术冲突的声明阶段会把直接回复转成草稿，仍需确认指令才会提交。"""


def _help_text(trigger_prefix: object) -> str:
    prefix = str(trigger_prefix or "t").strip() or "t"
    return HELP_TEXT_TEMPLATE.format(prefix=prefix)


class _WorldCommandGateway:
    """Bridge the platform-neutral world service to plugin dependencies."""

    def __init__(self, plugin: "TavernPlugin") -> None:
        self.plugin = plugin

    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[Mapping[str, Any]]:
        await self.plugin.ensure_builtin_worlds()
        return await self.plugin.database.list_worlds(
            include_archived=include_archived
        )

    async def builtin_world_status(self) -> list[Mapping[str, Any]]:
        return self.plugin.builtin_world_status()

    async def install_builtin_world(self, key: str) -> Mapping[str, Any]:
        return await self.plugin.retry_builtin_world(key)


class _SessionCommandGateway:
    """Bridge the platform-neutral session service to the live plugin."""

    def __init__(
        self,
        plugin: "TavernPlugin",
        event: AstrMessageEvent,
    ) -> None:
        self.plugin = plugin
        self.event = event

    def __getattr__(self, name: str) -> Any:
        """Forward repository methods to the database facade."""

        return getattr(self.plugin.database, name)

    def is_group_allowed(self, group_id: str) -> bool:
        return self.plugin.runtime_config().is_group_allowed(group_id)

    def public_status_enabled(self) -> bool:
        return self.plugin.runtime_config().public_status

    def unauthorized_behavior(self) -> str:
        return self.plugin.runtime_config().unauthorized_command_behavior

    def trigger_prefix(self) -> str:
        return self.plugin.runtime_config().trigger_prefix

    def time_rules(self) -> Mapping[str, Any]:
        return self.plugin.runtime_config().time_rules

    async def write_security_audit(
        self,
        *,
        sender_id: str,
        action: str,
        group_id: str,
        platform_id: str,
        reason: str,
    ) -> None:
        await self.plugin._write_security_audit(
            sender_id=sender_id,
            action=action,
            group_id=group_id,
            platform_id=platform_id,
            reason=reason,
        )

    async def allow_group(
        self,
        group_id: str,
        platform_id: str,
        actor_id: str,
    ) -> bool:
        return await self.plugin._allow_group(
            group_id=group_id,
            platform_id=platform_id,
            actor_id=actor_id,
            source="session_command_service",
        )

    async def send_card_code_private(
        self,
        session_id: str,
        participant: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        outcome = await self.plugin._send_card_code_private(
            event=self.event,
            session_id=session_id,
            participant=participant,
        )
        return {
            "ok": bool(getattr(outcome, "ok", False)),
            "status": str(getattr(outcome, "status", "") or ""),
        }

    async def validate_world_runtime(
        self,
        world_snapshot: Mapping[str, Any],
    ) -> None:
        self.plugin.engine.validate_world_runtime(world_snapshot)

    async def release_session_lock(self, session_id: str) -> None:
        await self.plugin.engine.release_session_lock(session_id)

    async def cancel_and_wait_operation(
        self,
        session_id: str,
        actor_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Mapping[str, Any]:
        result = await self.plugin.database.request_session_operation_cancel(
            session_id,
            actor_id,
        )
        if not result.get("found"):
            return {
                **dict(result),
                "message": (
                    "【取消故事生成】当前没有尚未提交的故事生成。\n"
                    "自动处理：系统没有修改副本、投票或检定。\n"
                    "下一步：发送 /团 状态 查看当前副本。"
                ),
            }
        operation_id = str(result.get("operation_id") or "")
        deadline = asyncio.get_running_loop().time() + max(
            0.1,
            float(timeout_seconds),
        )
        state = dict(result)
        while asyncio.get_running_loop().time() < deadline:
            current = await self.plugin.database.get_operation_state(operation_id)
            state = dict(current or state)
            if str(state.get("status") or "") in {
                "cancelled",
                "completed",
                "failed_retryable",
                "needs_recovery",
            }:
                break
            await asyncio.sleep(0.05)
        status = str(state.get("status") or "")
        request = state.get("result") or result.get("request") or {}
        if status == "cancel_requested":
            await self.plugin.database.update_operation(
                operation_id,
                status="needs_recovery",
                phase="cancel_wait_timeout",
                result={"last_error_code": "commit.uncertain"},
                actor_id=actor_id,
            )
            vote_id = str((result.get("request") or {}).get("vote_id") or "")
            if vote_id:
                await self.plugin.database.update_vote_resolution_status(
                    vote_id,
                    "needs_recovery",
                )
            status = "needs_recovery"
        if status == "completed":
            message = (
                "【取消故事生成】本轮故事已经先一步提交，未执行取消。\n"
                "自动处理：系统保留已提交故事，没有回滚世界。\n"
                "下一步：发送 /团 状态 查看最新回合。"
            )
        elif status == "needs_recovery":
            message = (
                "【取消故事生成】取消握手未能确认提交边界。\n"
                "自动处理：系统已锁住新回合并丢弃后续迟到结果。\n"
                "下一步：请在控制台核对回执，再发送 /团 重试本轮。"
            )
        else:
            message = (
                "【故事生成已取消】\n"
                "原因：本轮故事尚未提交。\n"
                "自动处理：系统已丢弃迟到模型结果；世界没有改变，"
                "已锁定的投票和检定不会重复。\n"
                "下一步：重新发送行动，或由主持人发送 /团 恢复。"
            )
        return {**dict(state), "status": status, "message": message}

    async def retry_session_operation(
        self,
        session_id: str,
        actor_id: str,
    ) -> Mapping[str, Any]:
        operation = await self.plugin.database.latest_retryable_session_operation(
            session_id
        )
        if operation is None:
            return {"message": "【重试本轮失败】当前没有可安全重试的操作。"}
        request = dict(operation.get("request") or {})
        operation_id = str(operation.get("operation_id") or "")
        operation_type = str(operation.get("operation_type") or "")
        if operation_type == "vote_resolution":
            vote = await self.plugin.database.pending_vote_resolution(session_id)
            if vote is None:
                return {
                    "message": (
                        "【重试本轮失败】表决状态已经变化。\n"
                        "自动处理：系统没有重投、重掷或修改世界。\n"
                        "下一步：发送 /团 状态，必要时在控制台核对回执。"
                    )
                }
            reply = await self.plugin.engine.process_vote_resolution(
                event=self.event,
                session_id=session_id,
                vote=vote,
                progress=lambda text: self.plugin._send_event_text(self.event, text),
            )
            return {
                "message": (
                    "【本轮重试已完成】\n"
                    "系统复用了原表决、原检定和原操作回执，没有重复副作用。\n\n"
                    + reply.text
                )
            }
        sender_id = str(request.get("actor_id") or actor_id)
        reply = await self.plugin.engine.process(
            event=self.event,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_id,
            content=str(request.get("player_input") or ""),
            workflow=(
                dict(request.get("workflow") or {})
                if isinstance(request.get("workflow"), Mapping)
                else {}
            ),
            progress=lambda text: self.plugin._send_event_text(self.event, text),
            force_actor=actor_id != sender_id,
            operation_id_override=operation_id,
            operation_request_override=request,
        )
        return {
            "message": (
                "【本轮重试已完成】\n"
                "系统复用了原操作与检定凭证，没有重复副作用。\n\n"
                + reply.text
            )
        }


_SESSION_STATE_LABELS = {
    SESSION_CLOSED: "已关闭",
    SESSION_PREPARING: "准备中",
    SESSION_RUNNING: "运行中",
    SESSION_PAUSED: "已暂停",
    SESSION_FINISHED: "已完结",
    SESSION_MAINTENANCE: "维护中",
}


from ..errors import report_failure
from ..presentation import (
    parse_instance_list_page,
    _compact_instance_intro,
    format_turn_status,
    format_instance_list,
    _instance_list_footer,
    format_roster,
    format_vote,
    format_recovered_timer,
    world_preset_brief,
    _profession_preset_line,
    _format_profession_step_prompt,
    format_card_prompt,
    format_card_preview,
    _review_reference,
    _pending_review_cards,
    _resolve_pending_review,
    format_pending_reviews,
    format_pending_reviews_compact,
    format_review_card,
    _format_remaining_time,
    _story_reply_parts,
)


@dataclass(frozen=True, slots=True)
class _BotApplicationInvocation:
    """Host-bound invocation consumed only by the BOT adapter handlers."""

    parsed: ParsedCommand
    event: AstrMessageEvent
    config: TavernConfig
    group_id: str
    platform_id: str
    sender_id: str

    @property
    def action(self) -> str:
        return self.parsed.action
from ..card_delivery import (
    WIZARD_DELIVERY_KEY,
    build_candidate_bundle,
    candidate_detail_text,
    cursor_status,
    delivery_state,
    pending_parts,
)


def _team_index_from_argument(argument: str) -> int:
    """0.11.3：解析「全队 2」/「全队」→ 全队行动候选项下标（0 基）。"""
    text = str(argument or "").strip()
    if text.isdigit():
        value = int(text)
        return max(0, value - 1) if value >= 1 else 0
    return 0


_PLAYER_FACING_COMMAND_ERRORS = (
    DatabaseNotFoundError,
    DatabaseConflictError,
    InvalidTransitionError,
    PermissionError,
    ValueError,
)

_MAX_PLAYER_ERROR_CHARS = 400


def _player_command_failure(exc: Exception) -> str:
    """把已知业务异常转换为玩家可见的【开团】失败消息。

    只透传代码中已经面向玩家撰写的领域文案（如救援失败原因），
    不暴露异常类型、堆栈、稳定 ID 等内部信息；消息超长时截断。
    """
    reason = str(exc).strip() or "操作未被接受"
    if len(reason) > _MAX_PLAYER_ERROR_CHARS:
        reason = reason[:_MAX_PLAYER_ERROR_CHARS].rstrip() + "…"
    if "系统没有修改" in reason and "下一步" in reason:
        return f"【开团】{reason}"
    return (
        "【开团】操作失败：" + reason + "\n"
        "系统没有修改任何数据。\n"
        "下一步：发送 /团 帮助 查看可用命令。"
    )



_COMMAND_UNHANDLED = object()

__all__ = [name for name in globals() if not name.startswith('__')]
