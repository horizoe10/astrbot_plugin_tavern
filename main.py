from __future__ import annotations

from astrbot.api.event import filter

from .tavern.entry.plugin_shared import *
from .tavern.entry.startup import StartupMethods
from .tavern.entry.delivery import DeliveryMethods
from .tavern.entry.messages import MessageMethods
from .tavern.entry.commands import CommandMethods
from .tavern.entry.legacy_commands import LegacyCommandMethods
from .tavern.entry.background_jobs import BackgroundJobMethods
from .tavern.entry.webhooks import WebhookMethods
from .tavern.entry.shutdown import ShutdownMethods

# Dynamic help title contract: 【321开团 v{PLUGIN_VERSION}｜


def _claim_native_command_handlers(*method_groups):
    """Assign mixin command handlers to this plugin entry for AstrBot binding.

    AstrBot registers and later binds handlers by ``handler.__module__``.  These
    methods are implemented in mixins, but their native command decorators live
    on ``TavernPlugin`` below, so the entry module is their registration owner.
    """

    for method_group in method_groups:
        for name, handler in vars(method_group).items():
            if name.startswith("tavern_") and callable(handler):
                handler.__module__ = __name__


_claim_native_command_handlers(MessageMethods, CommandMethods)

from .tavern.entry.private_messages import PrivateMessagesMixin
from .tavern.entry.group_messages import GroupMessagesMixin
from .tavern.entry.plugin_lifecycle import PluginLifecycleMixin

class TavernPlugin(PrivateMessagesMixin, GroupMessagesMixin, PluginLifecycleMixin, StartupMethods, DeliveryMethods, MessageMethods, CommandMethods, LegacyCommandMethods, BackgroundJobMethods, WebhookMethods, ShutdownMethods, Star):

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """Run the runtime startup hook under the real plugin module owner."""

        await PrivateMessagesMixin.on_loaded(self)

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=110,
    )
    async def on_private_message(self, event: AstrMessageEvent):
        """Consume ordinary private replies as well as private commands."""

        async for result in PrivateMessagesMixin.on_private_message(self, event):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def on_group_message(self, event: AstrMessageEvent):
        """Keep the non-command group story listener bound to this plugin."""

        async for result in GroupMessagesMixin.on_group_message(self, event):
            yield result

    @filter.command_group("团", priority=200)
    def tavern(self):
        """321开团原生指令组。"""

    @filter.regex(r"^团(?:\s+.*)?$", priority=190)
    async def handle_unrouted_tavern_command(self, event: AstrMessageEvent):
        """Handle bare-prefix commands and reject unknown native subcommands.

        AstrBot does not dispatch a command-group handler when text starts with
        the group name but no registered subcommand matches.  Without this
        lower-priority fallback, ``/团 不存在`` is treated as an LLM prompt and
        produces no deterministic command response.  Registered native
        commands run first at priority 200 and stop propagation; this handler
        therefore only receives an otherwise-unrouted command.  It also keeps
        the established missing-slash compatibility path functional.
        """

        getter = getattr(event, "get_message_str", None)
        message = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        ).strip()
        command = parse_tavern_command("/" + message)
        if command.matched and command.action != "unknown":
            # Help 走图片分支：绕过 _run_native_command，避免 webhooks 输出一大坨文本。
            if command.action == "help":
                group_id = self._group_id(event)
                scope = "private" if not group_id else "group"
                image_path = await self._render_help_image(event, scope=scope)
                if image_path:
                    event.stop_event()  # 必须 stop_event，否则 priority=100 的 on_group_message 会再发 contextual_help 大段文本
                    yield event.image_result(image_path)
                    return
                # 渲染失败时仍走文本兜底
            response = await self._run_native_command(event, command.action)
            if response:
                yield await self._message_result(event, response)
            return

        event.stop_event()
        raw_action = "".join(
            char for char in str(command.raw_action or "") if char.isprintable()
        )[:32]
        yield await self._message_result(
            event,
            "【开团指令无法识别】\n"
            f"失败操作：执行“{raw_action or '空命令'}”。\n"
            "原因：当前版本没有这个子指令。\n"
            "自动处理：系统未修改副本、角色卡或世界数据。\n"
            "下一步：发送 /团 帮助 查看当前可用指令。",
        )

    tavern_start = tavern.command(
        "开启", alias={"启动"}, priority=200
    )(MessageMethods.tavern_start)
    tavern_perform = tavern.command(
        "开演", alias={"开始故事"}, priority=200
    )(MessageMethods.tavern_perform)
    tavern_pause = tavern.command("暂停", priority=200)(
        MessageMethods.tavern_pause
    )
    tavern_cancel_generation = tavern.command("取消", priority=200)(
        MessageMethods.tavern_cancel_generation
    )
    tavern_retry_turn = tavern.command("重试本轮", priority=200)(
        MessageMethods.tavern_retry_turn
    )
    tavern_recover = tavern.command("恢复", priority=200)(
        MessageMethods.tavern_recover
    )
    tavern_resume = tavern.command("继续", priority=200)(
        CommandMethods.tavern_resume
    )
    tavern_close = tavern.command("关闭", priority=200)(
        CommandMethods.tavern_close
    )
    tavern_finish = tavern.command("完结", priority=200)(
        CommandMethods.tavern_finish
    )
    tavern_abort = tavern.command("强制终止", priority=200)(
        CommandMethods.tavern_abort
    )
    tavern_safety_pause = tavern.command("安全暂停", priority=200)(
        CommandMethods.tavern_safety_pause
    )
    tavern_maintenance = tavern.command("维护", priority=200)(
        CommandMethods.tavern_maintenance
    )
    tavern_status = tavern.command("状态", priority=200)(
        CommandMethods.tavern_status
    )
    tavern_dm = tavern.command("主持", priority=200)(
        CommandMethods.tavern_dm
    )
    tavern_save = tavern.command("存档", priority=200)(
        CommandMethods.tavern_save
    )
    tavern_delete_save = tavern.command("删档", priority=200)(
        CommandMethods.tavern_delete_save
    )
    tavern_load = tavern.command("读档", priority=200)(
        CommandMethods.tavern_load
    )
    tavern_rollback = tavern.command("回滚", priority=200)(
        CommandMethods.tavern_rollback
    )
    tavern_worlds = tavern.command("世界列表", priority=200)(
        CommandMethods.tavern_worlds
    )
    tavern_instances = tavern.command(
        "副本列表", alias={"副本"}, priority=200
    )(CommandMethods.tavern_instances)
    tavern_join = tavern.command("加入", priority=200)(
        CommandMethods.tavern_join
    )
    tavern_card = tavern.command("建卡", priority=200)(
        CommandMethods.tavern_card
    )
    tavern_card_fill = tavern.command("填写", priority=200)(
        CommandMethods.tavern_card_fill
    )
    tavern_card_random = tavern.command(
        "随机", alias={"sj"}, priority=200
    )(CommandMethods.tavern_card_random)
    tavern_card_expand = tavern.command(
        "补全", alias={"bq"}, priority=200
    )(CommandMethods.tavern_card_expand)
    tavern_card_web = tavern.command("网页建卡", priority=200)(
        CommandMethods.tavern_card_web
    )
    tavern_card_previous = tavern.command("上一步", priority=200)(
        CommandMethods.tavern_card_previous
    )
    tavern_card_modify = tavern.command("修改", priority=200)(
        CommandMethods.tavern_card_modify
    )
    tavern_card_current = tavern.command("当前步骤", priority=200)(
        CommandMethods.tavern_card_current
    )
    tavern_card_preview = tavern.command("预览", priority=200)(
        CommandMethods.tavern_card_preview
    )
    tavern_card_stats_reset = tavern.command("重填数值", priority=200)(
        CommandMethods.tavern_card_stats_reset
    )
    tavern_card_timer_notice = tavern.command("建卡提醒", priority=200)(
        CommandMethods.tavern_card_timer_notice
    )
    tavern_card_confirm = tavern.command("确认建卡", priority=200)(
        CommandMethods.tavern_card_confirm
    )
    tavern_card_cancel = tavern.command("取消建卡", priority=200)(
        CommandMethods.tavern_card_cancel
    )
    tavern_rescue = tavern.command("救援", priority=200)(
        CommandMethods.tavern_rescue
    )
    tavern_card_next = tavern.command("下一批", priority=200)(
        CommandMethods.tavern_card_next
    )
    tavern_card_detail = tavern.command("查看选项", priority=200)(
        CommandMethods.tavern_card_detail
    )
    tavern_card_restart = tavern.command("重新建卡", priority=200)(
        CommandMethods.tavern_card_restart
    )
    tavern_card_rename = tavern.command("修改角色名", priority=200)(
        CommandMethods.tavern_card_rename
    )
    tavern_card_nickname = tavern.command("修改昵称", priority=200)(
        CommandMethods.tavern_card_nickname
    )
    tavern_card_abandon = tavern.command("放弃席位", priority=200)(
        CommandMethods.tavern_card_abandon
    )
    tavern_character = tavern.command("角色", priority=200)(
        CommandMethods.tavern_character
    )
    tavern_tendency = tavern.command("我的倾向", priority=200)(
        CommandMethods.tavern_tendency
    )
    tavern_ready = tavern.command("准备", priority=200)(
        CommandMethods.tavern_ready
    )
    tavern_force_ready = tavern.command("强制全员准备", priority=200)(
        CommandMethods.tavern_force_ready
    )
    tavern_roster = tavern.command("阵容", priority=200)(
        CommandMethods.tavern_roster
    )
    tavern_review = tavern.command("审核", priority=200)(
        CommandMethods.tavern_review
    )
    tavern_choose = tavern.command("选择", priority=200)(
        CommandMethods.tavern_choose
    )
    tavern_reroll = tavern.command("重整选项", priority=200)(
        CommandMethods.tavern_reroll
    )
    tavern_inspiration = tavern.command("灵感", priority=200)(
        CommandMethods.tavern_inspiration
    )
    tavern_inspiration_reroll = tavern.command(
        "灵感重投", priority=200
    )(CommandMethods.tavern_inspiration_reroll)
    tavern_vote = tavern.command("投票", priority=200)(
        CommandMethods.tavern_vote
    )
    tavern_team = tavern.command(
        "全队", alias={"全队行动", "提议全队"}, priority=200
    )(CommandMethods.tavern_team)
    tavern_countdown = tavern.command("倒计时", priority=200)(
        CommandMethods.tavern_countdown
    )
    tavern_usage = tavern.command(
        "用量", alias={"Token用量"}, priority=200
    )(CommandMethods.tavern_usage)
    tavern_quota = tavern.command(
        "限额", alias={"Token限额"}, priority=200
    )(CommandMethods.tavern_quota)
    tavern_delete_session = tavern.command("删除副本", priority=200)(
        CommandMethods.tavern_delete_session
    )
    tavern_away = tavern.command("暂离", priority=200)(
        CommandMethods.tavern_away
    )
    tavern_return_queue = tavern.command("返回队列", priority=200)(
        CommandMethods.tavern_return_queue
    )
    tavern_return_request = tavern.command("申请返场", priority=200)(
        CommandMethods.tavern_return_request
    )
    tavern_leave = tavern.command("退出", priority=200)(
        CommandMethods.tavern_leave
    )
    tavern_order = tavern.command(
        "顺序", alias={"轮次"}, priority=200
    )(CommandMethods.tavern_order)
    tavern_skip = tavern.command("跳过", priority=200)(
        CommandMethods.tavern_skip
    )
    tavern_next = tavern.command("强制下一位", priority=200)(
        CommandMethods.tavern_next
    )
    tavern_help = tavern.command("帮助", priority=200)(
        CommandMethods.tavern_help
    )




