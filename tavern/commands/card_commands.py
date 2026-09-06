"""D1-ARC-001 建卡命令应用层（不持有 AstrBot 事件对象）。

本模块是私聊建卡的唯一业务入口；候选批次计算、确认、群通知意图与
私聊入口预检均在此形成平台无关的应用服务：

- 输入：``RequestContext``（平台无关上下文）、``ParsedCommand``（已解析
  命令）、``CardFlowProtocol``（数据层依赖协议）；
- 输出：``CommandResult``（外显文本 + 投递意图 + handled）；
- 复用 ``tavern/card_delivery.py``、``tavern/presentation.py``、
  ``tavern/delivery/target.py`` 的既有纯函数，不复制数据规则；
- 不 import AstrBot 消息类；不执行平台发送。

入口层职责：

1. 解析命令（``parse_tavern_command`` / 宽松兜底解析）；
2. 调用 :meth:`CardCommandService.preflight_candidate_delivery`，若返回
   ``CommandResult`` 则直接回复其文本并结束；
3. 调用 :meth:`CardCommandService.handle_private`；``handled=False`` 时
   交给其他私聊处理器；否则回复 ``text`` 并分发 ``delivery`` 意图：
   - ``candidate_bundle``：先按 ``payload["start_part"]`` 用
     ``tavern.card_delivery.delivery_state`` 持久化游标，再逐段发送
     ``payload["parts"]``（首段前置 ``payload["prefix"]``），每段成功后
     用下一 part 序号更新游标、失败时持久化 failed 状态并回复
     :func:`candidate_failure_feedback`；
   - ``group_notice``：按 payload 调 ``_send_or_queue``；
   - ``persist_verified_target``：调投递目标持久化；
   - ``revoke_private_target``：调私聊目标降级；
4. 本服务未捕获的异常（含候选批次构建失败）向上抛出，入口层按
   「私聊操作失败」兜底文案处理。

``PRIVATE_CARD_ACTIONS`` 是本服务负责的命令集合（自 main.py 抽取，路由
接线时用本常量替换 main.py 中的同名集合，避免双源）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from ..card_ai import CardAIComposer, CardAIError
from ..card_web_wizard import web_active_until
from ..card_delivery import (
    WIZARD_DELIVERY_KEY,
    build_candidate_bundle,
    candidate_detail_text,
    cursor_status,
    pending_parts,
)
from ..database import DatabaseNotFoundError
from ..delivery.target import DeliveryTarget
from ..messaging.player import PlayerMessage, render_player_message
from ..presentation import (
    _format_remaining_time,
    format_card_prompt,
    format_card_preview,
)
from ..security import ParsedCommand
from ..runtime.contracts import CommandError

from .models import (
    INTENT_CANDIDATE_BUNDLE,
    INTENT_GROUP_NOTICE,
    INTENT_PRIVATE_REPLY,
    INTENT_PERSIST_VERIFIED_TARGET,
    INTENT_REVOKE_PRIVATE_TARGET,
    CardFlowProtocol,
    CommandResult,
    DeliveryIntent,
    RequestContext,
)

logger = logging.getLogger(__name__)

# 私聊建卡命令集合（唯一来源；main.py 路由接线时替换其本地同名常量）。
PRIVATE_CARD_ACTIONS = frozenset(
    {
        "card",
        "card_fill",
        "card_previous",
        "card_modify",
        "card_current",
        "card_next",
        "card_detail",
        "card_stats_reset",
        "card_timer_notice",
        "card_preview",
        "card_confirm",
        "card_cancel",
        "card_restart",
        "card_rename",
        "card_nickname",
        "card_abandon",
        "card_random",
        "card_expand",
        "card_web",
    }
)

# 候选投递尚未完整时仍放行的命令（与 main.py 私聊入口一致）。
_CANDIDATE_PREFLIGHT_EXEMPT = frozenset(
    {
        "card_current",
        "card_next",
        "card_detail",
        "card_preview",
        "card_previous",
        "card_modify",
        "card_cancel",
        "card_restart",
        "card_abandon",
        "card_web",
    }
)

_PRIVATE_CARD_HELP_TEXT = (
    "【私聊建卡帮助】\n\n"
    "继续当前步骤\n"
    "/团 当前\n\n"
    "查看已填写资料\n"
    "/团 预览\n\n"
    "AI 设定助手（由模型代写或扩写当前字段）\n"
    "/团 随机\n"
    "/团 补全 <初始设定>\n\n"
    "网页建卡（浏览器逐项填写，含 AI 按钮）\n"
    "/团 网页建卡\n\n"
    "返回或修改\n"
    "/团 上一步\n"
    "/团 修改 <完整字段名称>\n\n"
    "重新生成角色数值\n"
    "/团 重填数值\n\n"
    "完成或撤销\n"
    "/团 确认建卡\n"
    "/团 取消建卡\n\n"
    "释放当前席位会关闭未完成草稿；执行前需要再次确认。\n"
    "/团 放弃席位 确认"
)

_CANDIDATE_READ_FAILED_TEXT = (
    "【候选读取失败】\n"
    "失败操作：准备当前字段的候选列表。\n"
    "原因：世界内容缺少有效候选说明或格式不完整。\n"
    "自动处理：系统没有推进角色卡，已保留当前草稿。\n"
    "下一步：发送 /团 预览 查看已填内容，并联系主持人修复世界包。"
)

_CANDIDATE_INCOMPLETE_TEXT = (
    "【角色卡候选尚未发送完整】\n\n"
    "失败操作：发送当前步骤的全部候选。\n\n"
    "原因：平台只确认了 {next_part}/{total_parts} 段。\n\n"
    "自动处理：系统已保存下一段位置；已送达内容不会重复发送，"
    "本条输入也没有推进角色卡。\n\n"
    "下一步\n\n"
    "/团 当前"
)


def candidate_failure_feedback(
    bundle: Mapping[str, Any],
    *,
    logical_batch: int,
    failure_count: int,
    unsent_text: str,
) -> str:
    """候选发送失败时的玩家文案（纯文本，无平台 I/O）。

    由入口层在某段候选发送失败后调用；不携带内部字段。
    """

    field_label = (
        str(bundle.get("field_label") or "当前字段")
        if isinstance(bundle, Mapping)
        else "当前字段"
    )
    feedback = (
        "【候选发送失败】\n"
        f"失败操作：发送「{field_label}」第 {int(logical_batch) + 1} 批候选。\n"
        "原因：平台没有确认本段消息发送成功。\n"
        "自动处理：系统已保存未发送位置，角色卡没有推进。\n"
        "下一步\n/团 当前"
    )
    if int(failure_count) + 1 >= 2 and unsent_text:
        feedback += (
            "\n\n【连续失败降级】以下为本次未送达内容，"
            "可直接按全局序号作答：\n\n" + str(unsent_text)
        )
    return feedback


class CardCommandService:
    """建卡命令应用服务：只编排应用服务，不触碰平台事件对象。"""

    def __init__(
        self,
        database: CardFlowProtocol,
        ai: CardAIComposer | None = None,
        web: Any = None,
    ) -> None:
        self.database = database
        # AI 设定助手（/团 随机、/团 补全）；未注入时对应指令返回未启用提示。
        self.ai = ai
        # 网页建卡链接网关（/团 网页建卡）；未注入时返回未启用提示。
        self.web = web

    def handles(self, action: str) -> bool:
        """该命令动作是否属于建卡命令（供路由分发使用）。"""

        return action in PRIVATE_CARD_ACTIONS

    async def preflight_candidate_delivery(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult | None:
        """候选投递预检：候选读取失败或未发送完整时返回拦截文案。

        与 main.py 私聊入口一致：非豁免命令在候选未发送完整时被拦截，
        只放行查看/修改/取消类命令；返回 None 表示放行。
        """

        origin = ctx.origin
        draft = await self.database.card_draft_for_private(origin)
        if web_active_until(
            draft.get("fields") if isinstance(draft, Mapping) else None
        ) > time.time():
            # 网页建卡激活期间，聊天侧不再拦截/推送候选，避免双端刷屏。
            return None
        if not draft:
            return None
        try:
            fields = draft.get("fields")
            fields = fields if isinstance(fields, Mapping) else {}
            state = fields.get(WIZARD_DELIVERY_KEY)
            bundle = build_candidate_bundle(draft, platform_id=ctx.platform)
        except Exception:
            logger.exception("321开团候选预检失败")
            return CommandResult.reply(_CANDIDATE_READ_FAILED_TEXT)
        if (
            isinstance(state, Mapping)
            and isinstance(bundle, Mapping)
            and cursor_status(bundle, state).get("valid")
            and int(state.get("next_part", 0) or 0)
            < int(state.get("total_parts", 0) or 0)
            and str(state.get("status") or "") in {"pending", "failed"}
            and command.action not in _CANDIDATE_PREFLIGHT_EXEMPT
        ):
            return CommandResult.reply(
                _CANDIDATE_INCOMPLETE_TEXT.format(
                    next_part=int(state.get("next_part", 0) or 0),
                    total_parts=int(state.get("total_parts", 0) or 0),
                )
            )
        return None

    async def handle_private(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        raw_message: str,
    ) -> CommandResult:
        """处理一条私聊消息（命令或裸输入）。

        - 已确认的领域异常（``DatabaseNotFoundError`` / ``PermissionError`` /
          ``ValueError``）转换为「【私聊建卡】…」玩家文案；
        - 未预期异常（含候选批次构建失败）向上抛出，由入口层兜底；
        - 候选批次投递以结构化意图返回，平台发送与游标持久化由入口层执行。
        """

        origin = ctx.origin
        draft = await self.database.card_draft_for_private(origin)
        if not command.matched and not draft:
            return CommandResult.ignored()
        if draft and draft.get("needs_revision"):
            notice = str(
                draft.get("content_update_notice")
                or (
                    "世界内容已更新，先前选择已不再可用；"
                    "系统已保留其他建卡资料，请重新选择当前项目。"
                )
            )
            text = (
                "【角色卡需要重新选择】\n"
                f"{notice}\n\n"
                + format_card_prompt(draft)
            )
            return CommandResult.reply(
                text,
                delivery=self._plan_candidate_delivery(
                    ctx,
                    command,
                    text,
                    draft,
                ),
            )
        try:
            dispatched = await self._dispatch(ctx, command, raw_message, draft)
            text, delivery = dispatched[:2]
            delivery_draft = (
                dispatched[2]
                if len(dispatched) > 2
                and isinstance(dispatched[2], Mapping)
                else draft
            )
        except (DatabaseNotFoundError, PermissionError, ValueError) as exc:
            return CommandResult.failed(
                CommandError(
                    "card.operation.failed",
                    operation="处理角色卡",
                    reason=str(exc),
                    automatic_action="系统没有提交无效资料；当前草稿和已完成步骤均已保留。",
                    next_command="/团 当前",
                    audience="player",
                )
            )
        delivery = delivery + self._plan_candidate_delivery(
            ctx,
            command,
            text,
            delivery_draft,
        )
        return CommandResult.reply(text, delivery=delivery)

    async def _dispatch(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        raw_message: str,
        draft: Mapping[str, Any] | None,
    ) -> (
        tuple[str, tuple[DeliveryIntent, ...]]
        | tuple[str, tuple[DeliveryIntent, ...], Mapping[str, Any]]
    ):
        """按命令动作分派；这是私聊建卡业务的唯一命令分支。"""

        origin = ctx.origin
        source_event_id = self._transport_event_id(ctx)
        if command.matched and command.action == "card":
            if not command.argument:
                if draft:
                    return (
                        "【私聊建卡】\n"
                        "当前私聊已经绑定席位；以下是下一步内容。\n\n"
                        + format_card_prompt(draft),
                        (),
                    )
                return (
                    "【私聊建卡尚未开始】\n"
                    "失败操作：读取你在群内预留的建卡席位。\n"
                    "原因：当前账号没有可自动匹配的待建卡席位。\n"
                    "自动处理：系统没有创建或修改角色卡。\n"
                    "下一步：先在目标群发送 /团 加入，"
                    "然后回到这里发送 /团 建卡。",
                    (),
                )
            bound = await self.database.bind_card_code(
                command.argument,
                ctx.user_id,
                origin,
            )
            if bound.get("binding_code_reissued"):
                return (
                    "【建卡码已过期，系统已自动补发】\n"
                    f"新建卡码：{bound['binding_code']}\n"
                    f"有效期至：{bound.get('binding_expires_at')}\n\n"
                    "请重新发送：\n"
                    f"/团 建卡 {bound['binding_code']}",
                    (),
                )
            intents: list[DeliveryIntent] = []
            bound_target = DeliveryTarget.from_origin(
                origin,
                verified_binding=True,
                source="verified_private",
            )
            if bound_target is not None:
                intents.append(
                    DeliveryIntent(
                        INTENT_PERSIST_VERIFIED_TARGET,
                        {
                            "target": bound_target,
                            "session_id": str(bound.get("session_id") or ""),
                        },
                    )
                )
            return (
                "【私聊身份绑定成功】\n" + format_card_prompt(bound),
                tuple(intents),
            )
        if command.matched and command.action == "card_preview":
            preview = await self.database.preview_card_draft(origin)
            return (format_card_preview(preview), ())
        if command.matched and command.action == "card_detail":
            if not draft:
                raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
            raw_ordinal = str(command.argument or "").strip()
            if not raw_ordinal.isdigit() or int(raw_ordinal) <= 0:
                raise ValueError(
                    "查看候选失败：请填写当前候选的全局序号。"
                    "\n下一步：发送 /团 查看选项 3"
                )
            bundle = build_candidate_bundle(
                draft,
                platform_id=ctx.platform,
            )
            detail = (
                candidate_detail_text(bundle, int(raw_ordinal))
                if isinstance(bundle, Mapping)
                else None
            )
            if not detail:
                raise ValueError(
                    "查看候选失败：当前字段没有这个序号。"
                    "\n系统没有修改角色卡。"
                    "\n下一步：发送 /团 当前 查看有效序号。"
                )
            return ("【候选详情】\n" + detail, ())
        if command.matched and command.action == "card_next":
            if not draft:
                raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
            bundle = build_candidate_bundle(
                draft,
                platform_id=ctx.platform,
            )
            fields = draft.get("fields")
            fields = fields if isinstance(fields, Mapping) else {}
            state = fields.get(WIZARD_DELIVERY_KEY)
            remaining = (
                pending_parts(
                    bundle,
                    state if isinstance(state, Mapping) else {},
                )
                if isinstance(bundle, Mapping)
                else []
            )
            if not remaining:
                return ("【候选已全部发送】请按全局序号或完整名称作答。", ())
            return ("【继续发送候选】", ())
        if command.matched and command.action == "card_current":
            current = await self.database.card_draft_for_private(origin)
            if not current:
                raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
            return (format_card_prompt(current), (), current)
        if command.matched and command.action == "card_previous":
            previous = await self.database.previous_card_step(origin)
            return ("【已返回上一步】\n" + format_card_prompt(previous), ())
        if command.matched and command.action == "card_modify":
            if not command.argument:
                raise ValueError(
                    "修改字段失败：命令后缺少字段名称。"
                    "\n系统没有修改角色卡。"
                    "\n下一步：发送 /团 修改 <完整字段名称>"
                )
            modified = await self.database.modify_card_field(
                origin,
                command.argument,
            )
            return ("【已进入字段修改】\n" + format_card_prompt(modified), ())
        if command.matched and command.action in {"card_rename", "card_nickname"}:
            if not command.argument:
                label = "角色名" if command.action == "card_rename" else "昵称"
                raise ValueError(f"请在命令后填写新的{label}")
            target = "name" if command.action == "card_rename" else "code"
            await self.database.modify_card_field(origin, target)
            updated = await self.database.fill_card_draft(
                origin,
                command.argument,
                source_event_id=source_event_id,
            )
            label = "角色名" if command.action == "card_rename" else "昵称"
            return (f"【{label}已更新】\n\n" + format_card_prompt(updated), ())
        if command.matched and command.action == "card_restart":
            restarted = await self.database.restart_card_draft(origin)
            return (
                "【已重新开始建卡】\n\n"
                "旧草稿已保留为历史记录，不会进入正式角色卡。\n\n"
                + format_card_prompt(restarted),
                (),
            )
        if command.matched and command.action == "card_stats_reset":
            reset = await self.database.reset_card_draft_stats(origin)
            if reset.get("profession_reset"):
                return (
                    "【主副属性已重置】\n"
                    "职业与职业固定基础属性均已保留；"
                    "其他角色资料没有改变。\n"
                    "请重新选择主属性和副属性。\n"
                    + format_card_prompt(reset),
                    (),
                )
            return (
                "【角色数值已保留】已保留你已分配的数值，"
                "可重新调整未使用的剩余点数。\n"
                + format_card_prompt(reset),
                (),
            )
        if command.matched and command.action == "card_timer_notice":
            setting = str(command.argument or "").strip().casefold()
            if setting in {"开", "开启", "on", "true", "1"}:
                enabled: bool | None = True
            elif setting in {"关", "关闭", "off", "false", "0"}:
                enabled = False
            elif setting:
                raise ValueError(
                    "请使用 /团 建卡提醒 开 或 /团 建卡提醒 关"
                )
            else:
                enabled = None
            result = await self.database.set_card_completion_reminder(
                origin,
                enabled,
            )
            state = "已开启" if result["enabled"] else "已关闭"
            remaining = (
                _format_remaining_time(result.get("remaining_seconds"))
                if result.get("has_deadline")
                else "不限时"
            )
            suffix = (
                "之后每 2 分钟私聊提示一次剩余时间。"
                if result["enabled"]
                else "建卡计时仍会继续，到期结果仍会私聊通知。"
            )
            return (
                f"【建卡倒计时提示{state}】"
                f"当前剩余 {remaining}。{suffix}",
                (),
            )
        if command.matched and command.action == "card_confirm":
            result = await self.database.confirm_card_draft(origin)
            if result.get("needs_revision"):
                issue_lines = []
                for issue in result.get("dependency_issues") or []:
                    label = str(
                        issue.get("field_label")
                        or issue.get("field")
                        or "相关字段"
                    )
                    issue_lines.append(f"- {label}")
                return (
                    "【角色卡需要修正】\n\n"
                    "你先前选择的职业或上游预设已经变化，"
                    "以下依赖字段已清空：\n\n"
                    + ("\n".join(issue_lines) or "- 依赖字段")
                    + "\n\n请重新选择后再确认。\n\n"
                    + format_card_prompt(result),
                    (),
                )
            character_name = str(result.get("character_name") or "").strip()
            if not character_name:
                return (
                    "【角色卡公开通知未发送】\n"
                    "操作：保存角色卡并发布建成通知。\n"
                    "原因：角色卡缺少可公开显示的角色名称。\n"
                    "自动处理：角色卡资料已保留；系统已取消群内通知，"
                    "未显示内部标识。\n"
                    "下一步：\n\n"
                    "/团 修改 角色名",
                    (),
                )
            status = (
                "角色卡已自动通过，可以回群发送 /团 准备。"
                if result.get("auto_approved")
                else "角色卡已提交主持人审核：主持人在团局群发送 /团 审核 即可查看并审批，通过后再回群准备。"
            )
            extras = []
            pending_count = int(result.get("card_stage_pending_count") or 0)
            if result.get("card_stage") in {"staged_pending", "stage_locked"}:
                pending_label = (
                    f"其余 {pending_count} 项"
                    if pending_count > 0
                    else "其余资料"
                )
                extras.append(
                    f"📌 后续补充：{pending_label}会在剧情中逐步确认；"
                    "不会影响本次建卡确认、审核或开演。"
                )
            if result.get("seeded_starter_loadout"):
                extras.append(
                    "🎒 开局物资："
                    + "、".join(result["seeded_starter_loadout"])
                )
            if result.get("seeded_funds"):
                extras.append(
                    "💰 初始资金：" + "、".join(result["seeded_funds"])
                )
            text = (
                f"【角色卡已确认】\n\n"
                f"「{character_name}」已经保存。\n\n"
                f"当前情况\n{status}"
                + ("\n\n" + "\n".join(extras) if extras else "")
            )
            return (text, await self._group_created_intents(result))
        if command.matched and command.action == "card_cancel":
            await self.database.cancel_card_draft(origin)
            return (
                "【当前草稿已取消】\n\n"
                "你的席位仍然保留，角色尚未正式建立。\n\n"
                "重新开始：\n\n"
                "/团 重新建卡\n\n"
                "彻底放弃席位：\n\n"
                "/团 放弃席位 确认",
                (),
            )
        if command.matched and command.action == "card_abandon":
            if str(command.argument or "").strip() not in {
                "确认",
                "confirm",
                "CONFIRM",
            }:
                return (
                    "【确认放弃席位】\n\n"
                    "这会释放当前副本席位并撤销未使用的建卡码，"
                    "未成型角色不会进入返场流程。\n\n"
                    "确认执行：\n\n"
                    "/团 放弃席位 确认",
                    (),
                )
            await self.database.abandon_card_seat(origin)
            return (
                "【席位已放弃】\n\n"
                "未完成的角色草稿已关闭，席位已经释放。\n\n"
                "以后想重新加入时，请回群发送：\n\n"
                "/团 加入",
                (
                    DeliveryIntent(
                        INTENT_REVOKE_PRIVATE_TARGET,
                        {
                            "platform_id": ctx.platform,
                            "user_id": ctx.user_id,
                            "reason": "seat_abandoned",
                        },
                    ),
                ),
            )
        if command.matched and command.action == "card_web":
            return await self._issue_web_link(ctx, draft)
        if command.matched and command.action in {
            "card_random",
            "card_expand",
        }:
            return await self._ai_generate_and_fill(
                ctx,
                draft,
                mode=(
                    "random"
                    if command.action == "card_random"
                    else "expand"
                ),
                user_draft=str(command.argument or "").strip(),
            )
        if command.matched and command.action == "card_fill":
            value = command.argument
        elif command.matched:
            return (_PRIVATE_CARD_HELP_TEXT, ())
        else:
            value = raw_message
        result = await self.database.fill_card_draft(
            origin,
            value,
            source_event_id=source_event_id,
        )
        if result.get("duplicate"):
            return (
                "【私聊建卡】这条消息已经处理过，当前步骤未重复推进。\n"
                + format_card_prompt(result),
                (),
            )
        confirmation = self._selection_confirmation_intent(result)
        return (
            format_card_prompt(result),
            (confirmation,) if confirmation is not None else (),
            result,
        )

    async def _ai_generate_and_fill(
        self,
        ctx: RequestContext,
        draft: Mapping[str, Any] | None,
        *,
        mode: str,
        user_draft: str = "",
    ) -> tuple[str, tuple[DeliveryIntent, ...], Mapping[str, Any]]:
        """AI 生成当前字段设定，并按标准填写流程校验落库。

        生成值不绕过任何数据规则：仍交给 ``fill_card_draft`` 做字段
        校验、依赖清理与游标推进；校验失败时把生成内容原样交还，
        供玩家手动发送填写。
        """

        if not draft:
            raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
        if mode == "expand" and not user_draft:
            raise ValueError(
                "补全设定失败：命令后缺少你的初始设定。"
                "\n系统没有修改角色卡。"
                "\n下一步：发送 /团 补全 <初始设定>，例如"
                " /团 补全 一个腐朽的木制魔杖和一本老旧的魔法书"
            )
        if self.ai is None:
            raise ValueError(
                "AI 设定助手未启用：插件没有可用的语言模型接入。"
                "\n系统没有修改角色卡。"
                "\n下一步：请手动填写当前字段，"
                "或联系管理员检查插件的叙事模型配置。"
            )
        try:
            value, field_label, generated = await self.ai.compose_field_value(
                ctx.origin,
                draft,
                mode=mode,
                user_draft=user_draft,
            )
        except CardAIError as exc:
            return (
                "【AI设定生成失败】\n"
                f"失败操作：为当前字段生成 AI 设定。\n"
                f"原因：{exc}\n"
                "自动处理：系统没有修改角色卡。\n"
                "下一步：可重新发送指令重试，或手动填写当前字段。",
                (),
            )
        try:
            result = await self.database.fill_card_draft(
                ctx.origin,
                value,
                source_event_id=self._transport_event_id(ctx),
            )
        except ValueError as exc:
            if "草稿已过期" in str(exc):
                raise
            return (
                "【AI设定未能自动填入】\n"
                f"失败操作：写入「{field_label}」。\n"
                f"原因：{exc}\n"
                "自动处理：系统没有修改角色卡，生成内容保留如下。\n"
                "下一步：可复制下方内容直接发送填写，或重新生成。\n\n"
                + generated,
                (),
            )
        if result.get("duplicate"):
            return (
                "【私聊建卡】这条消息已经处理过，当前步骤未重复推进。\n"
                + format_card_prompt(result),
                (),
                result,
            )
        title = "AI随机设定" if mode == "random" else "AI补全设定"
        body = (
            f"【{title}·{field_label}】\n\n"
            + generated
            + "\n\n———\n已写入角色卡并推进到下一步。"
            "不满意可发送 /团 上一步 后重新生成；"
            "查看全部资料可发送 /团 预览。\n\n"
            + format_card_prompt(result)
        )
        return (body, (), result)

    async def _issue_web_link(
        self,
        ctx: RequestContext,
        draft: Mapping[str, Any] | None,
    ) -> tuple[str, tuple[DeliveryIntent, ...]]:
        """签发网页建卡魔法链接（一次性、15 分钟有效）。"""

        if not draft:
            raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
        if self.web is None:
            return (
                "网页建卡未启用：插件缺少网页建卡网关。",
                (),
            )
        url, error = await self.web.issue_link(ctx.origin, draft)
        if error:
            return (error, ())
        return (
            "【网页建卡已就绪】\n\n"
            "请在本机或手机浏览器打开下面的链接（15 分钟内有效，仅可使用一次）：\n"
            f"{url}\n\n"
            "网页里可以逐项填写资料，每项旁有「随机」「补全」AI 按钮；"
            "预览、修改和确认建卡也都能在网页完成。\n"
            "链接泄露给他人等于交出你的建卡权，请勿转发；"
            "链接失效后重新发送 /团 网页建卡 即可。",
            (),
        )

    async def _group_created_intents(
        self,
        result: Mapping[str, Any],
    ) -> tuple[DeliveryIntent, ...]:
        """角色卡建成群通知意图（best-effort，与 main.py 行为一致）。"""

        try:
            session_id = str(result.get("session_id") or "")
            if not session_id:
                return ()
            session = await self.database.get_session(session_id)
            origin = str(session.get("unified_origin") or "") if session else ""
            name = str(result.get("character_name") or "").strip()
            if not name:
                logger.warning(
                    "角色卡建成群通知已取消：角色缺少公开名称"
                )
                return ()
            review_text = (
                "已自动通过审核。"
                if result.get("auto_approved")
                else "等待审核。"
            )
            notice_text = (
                "【角色卡已提交】\n\n"
                f"「{name}」的角色卡已经建立，{review_text}\n\n"
                "下一步\n"
                "/团 准备"
            )
            dedupe_key = (
                f"card-created:{result.get('id') or session_id + ':' + name}"
            )
            return (
                DeliveryIntent(
                    INTENT_GROUP_NOTICE,
                    {
                        "session_id": session_id,
                        "origin": origin,
                        "text": notice_text,
                        "kind": "card.created",
                        "dedupe_key": dedupe_key,
                    },
                ),
            )
        except Exception:
            logger.warning("321开团角色卡建成群通知意图构建失败", exc_info=True)
            return ()

    def _plan_candidate_delivery(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        response: str,
        draft: Mapping[str, Any] | None,
    ) -> tuple[DeliveryIntent, ...]:
        """计算候选批次投递计划（提取自 ``_deliver_card_candidate_bundle``）。

        只做纯计算：批次、前缀、待发送段与游标信息全部由既有纯函数产出，
        平台发送与 ``set_card_delivery_state`` 持久化留给入口层。
        """

        if (
            not draft
            or command.action == "card_detail"
            or web_active_until(draft.get("fields")) > time.time()
        ):
            # 网页建卡激活期间聊天侧候选静默；网页操作会刷新该截止时间。
            return ()
        bundle = build_candidate_bundle(draft, platform_id=ctx.platform)
        if not bundle:
            return ()
        prompt = format_card_prompt(draft)
        should_deliver = (
            command.action in {"card_current", "card_next"}
            or bool(prompt and str(response).endswith(prompt))
        )
        if not should_deliver:
            return ()
        fields = draft.get("fields")
        fields = fields if isinstance(fields, Mapping) else {}
        cursor = fields.get(WIZARD_DELIVERY_KEY)
        cursor = cursor if isinstance(cursor, Mapping) else {}
        status = cursor_status(bundle, cursor)
        generation_notice = (
            "【候选列表已更新】世界内容或文案规则发生变化，"
            "系统已从当前字段第一批重新发送。\n\n"
            if not status.get("valid")
            else ""
        )
        remaining = pending_parts(bundle, cursor)
        if not remaining:
            return ()
        logical_batch = int(remaining[0].get("logical_batch", 0) or 0)
        prefix = (
            str(response)[: -len(prompt)]
            if prompt and str(response).endswith(prompt)
            else ""
        )
        prefix = generation_notice + prefix
        start_part = int(remaining[0].get("part", 0) or 0)
        failure_count = max(0, int(cursor.get("failure_count", 0) or 0))
        total_parts = int(bundle.get("part_count", 0) or 0)
        return (
            DeliveryIntent(
                INTENT_CANDIDATE_BUNDLE,
                {
                    "bundle": bundle,
                    "cursor": dict(cursor),
                    "prefix": prefix,
                    "logical_batch": logical_batch,
                    "parts": remaining,
                    "start_part": start_part,
                    "total_parts": total_parts,
                    "failure_count": failure_count,
                },
            ),
        )

    @staticmethod
    def _selection_confirmation_intent(
        result: Mapping[str, Any],
    ) -> DeliveryIntent | None:
        """Build confirmation from the value that was actually committed."""

        confirmation = result.get("selection_confirmation")
        if not isinstance(confirmation, Mapping):
            return None
        field_label = str(
            confirmation.get("field_label") or "当前项目"
        ).strip()
        raw_labels = confirmation.get("value_labels")
        labels = (
            [str(item).strip() for item in raw_labels if str(item).strip()]
            if isinstance(raw_labels, (list, tuple))
            else []
        )
        if not labels:
            value_label = str(
                confirmation.get("value_label") or ""
            ).strip()
            if value_label:
                labels = [value_label]
        if not labels:
            return None
        selected_text = "、".join(f"【{label}】" for label in labels)
        return DeliveryIntent(
            INTENT_PRIVATE_REPLY,
            {
                "text": (
                    "【选择已记录】\n"
                    f"你为「{field_label}」选择了：{selected_text}。"
                )
            },
        )

    @staticmethod
    def _transport_event_id(ctx: RequestContext) -> str:
        """从上下文元数据读取传输事件 ID（入口层注入的安全标量）。"""

        metadata = ctx.metadata
        if isinstance(metadata, Mapping):
            return str(metadata.get("transport_event_id") or "").strip()
        return ""


__all__ = [
    "PRIVATE_CARD_ACTIONS",
    "CardCommandService",
    "candidate_failure_feedback",
]
