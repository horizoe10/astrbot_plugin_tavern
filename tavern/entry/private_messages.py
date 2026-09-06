from __future__ import annotations

from ..card_ai import CardAIComposer, CardAIError
from ..card_web_wizard import CardWebLinkGateway
from .plugin_shared import *
from .startup import StartupMethods
from .delivery import DeliveryMethods
from .messages import MessageMethods
from .commands import CommandMethods
from .legacy_commands import LegacyCommandMethods
from .background_jobs import BackgroundJobMethods
from .webhooks import WebhookMethods
from .shutdown import ShutdownMethods

# Dynamic help title contract: 【321开团 v{PLUGIN_VERSION}｜



class PrivateMessagesMixin:
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.plugin_config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._config_lock = asyncio.Lock()
        runtime = build_runtime(
            context=context,
            plugin_config=self.plugin_config,
            data_dir=self.data_dir,
            config_provider=self.runtime_config,
            logger=logger,
            allow_group=self._allow_group,
            config_lock=self._config_lock,
        )
        self.database = runtime.database
        self.card_web_linker = CardWebLinkGateway(self)
        self.card_commands = CardCommandService(
            self.database,
            ai=CardAIComposer(self._card_ai_generate),
            web=self.card_web_linker,
        )
        self.admin_commands = AdminCommandService(self.database)
        self.growth_commands = GrowthCommandService(self.database)
        self.tendency_commands = TendencyApplicationService(self.database)
        self.turn_commands = TurnCommandHandler()
        self.vote_commands = VoteCommandHandler()
        self.tactical_commands = TacticalCommandService()
        self.challenge_commands = ChallengeCommandService()
        self.world_commands = WorldCommandService(
            _WorldCommandGateway(self)
        )
        self.delivery_service = DeliveryService(
            context=self.context,
            repository=self.database,
            markdown_enabled=lambda target: self._qqbot_markdown_for_platform(
                target.platform_instance_id or target.unified_origin
            ),
        )
        self.delivery_worker = OutboxWorker(
            service=self.delivery_service,
            repository=self.database,
        )
        self.broker = runtime.broker
        self.engine = runtime.engine
        self.ai_turn_runner = AiCompanionTurnRunner(
            self.database,
            self.engine,
        )
        self.web_console = runtime.web_console
        self.hooks = runtime.hooks
        self.extensions = runtime.extensions
        self.public_api = runtime.public_api
        self.modules = runtime.modules
        self.world_twp = runtime.world_twp
        self._builtin_world_status: dict[str, dict[str, Any]] = {
            spec.key: {
                "key": spec.key,
                "name": spec.display_name,
                "target_content_version": spec.content_version,
                "state": "pending",
                "using_previous_version": False,
                "message": "等待安装检查",
                "last_error": "",
            }
            for spec in builtin_world_specs()
        }
        self.web_console.builtin_world_status_provider = (
            self.builtin_world_status
        )
        self.web_console.builtin_world_retry = self.retry_builtin_world
        self.web_console.builtin_world_ensure = self.ensure_builtin_worlds
        self.web_console.delivery_service = self.delivery_service
        self._builtin_world_install_lock = asyncio.Lock()
        self._background = BackgroundTaskSupervisor(logger)
        self.event_outbox_worker = EventOutboxWorker(
            self.database,
            self.hooks,
        )
        self.storage_sync_worker = StorageSyncWorker(self.database)
        self.author_job_worker = AuthorJobWorker(self.database)
        self._timer_task: asyncio.Task[None] | None = None
        self._backup_task: asyncio.Task[None] | None = None
        self._webhook_task: asyncio.Task[None] | None = None
        self._event_outbox_task: asyncio.Task[None] | None = None
        self._storage_sync_task: asyncio.Task[None] | None = None
        self._author_job_task: asyncio.Task[None] | None = None
        self._webhook_status: dict[str, Any] = {
            "state": "idle",
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error": "",
        }
        self._panel_server = None
        self._panel_thread = None
        self._panel_runtime_status: dict[str, str] = {
            "state": "stopped",
            "message": "独立面板尚未启动。",
            "recovery": "插件加载后会按当前配置启动。",
        }
        self.web_console.remote_panel_runtime_status_provider = (
            lambda: dict(self._panel_runtime_status)
        )
        # 计时通知的去重与频控状态：
        # QQ 官方接口对主动消息有频控（40034100），
        # 一旦同一轮吐出多条提醒就会被整段拒绝并反复刷屏重试。
        self._timer_notice_last_sent: dict[str, float] = {}
        self._timer_notice_last_at: float = 0.0
        self._builtin_world_bootstrap_task = (
            self._schedule_builtin_world_bootstrap()
        )
        self.application_router = self._build_application_router()
        self.application_orchestrator = ApplicationCommandOrchestrator(
            self.application_router
        )

    async def _private_review_web_entry(self, event, command) -> str:
        """私聊 /团 审核：为主持人签发网页审核链接（/cw/review）。

        返回空字符串表示未处理（非主持人也没有任何待审核时交给后续流程）。
        """

        from ..presentation.reviews import _pending_review_cards

        sender_id = str(event.get_sender_id() or "")
        config = self.runtime_config()
        argument = str(command.argument or "").strip()

        hosted: list[tuple[Mapping[str, Any], list[Any]]] = []
        for session in await self.database.list_sessions():
            if str(session.get("state") or "") == SESSION_FINISHED:
                continue
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            roles = await self.database.permission_roles(
                session_id, sender_id
            )
            if "host" not in roles and not config.is_admin(sender_id):
                continue
            roster = await self.database.list_roster(session_id)
            pending = list(_pending_review_cards(roster))
            if pending:
                hosted.append((session, pending))
        if not hosted:
            return "【网页审核】当前没有待审核的角色卡。"

        if argument:
            selected = None
            if argument.isdigit() and 1 <= int(argument) <= len(hosted):
                selected = hosted[int(argument) - 1]
            else:
                matches = [
                    item
                    for item in hosted
                    if str(item[0].get("instance_name") or "").strip()
                    == argument
                ]
                if len(matches) == 1:
                    selected = matches[0]
            if selected is None:
                lines = ["【网页审核】没有匹配的副本。可选择的副本："]
                for index, (session, pending) in enumerate(hosted, 1):
                    lines.append(
                        f"{index}. {session.get('instance_name')}"
                        f"（待审 {len(pending)} 张）"
                    )
                return "\n".join(lines)
            hosted = [selected]

        if len(hosted) > 1:
            lines = [
                "【网页审核】你主持的多个副本都有待审核角色卡，"
                "请发送 /团 审核 <序号>："
            ]
            for index, (session, pending) in enumerate(hosted, 1):
                lines.append(
                    f"{index}. {session.get('instance_name')}"
                    f"（待审 {len(pending)} 张）"
                )
            return "\n".join(lines)

        bypass_host = config.is_admin(sender_id)
        url, error = await self.card_web_linker.issue_review_link(
            sender_id,
            bypass_host=bypass_host,
        )
        if error:
            return f"【网页审核】{error}"
        session, pending = hosted[0]
        return (
            f"【网页审核】{session.get('instance_name')}"
            f" · 待审 {len(pending)} 张\n"
            f"{url}\n"
            "链接 15 分钟内有效，可在网页中查看角色卡详情并通过 / 驳回；"
            "审核会话 30 分钟内可刷新继续。"
        )

    async def _card_ai_generate(
        self,
        origin: str,
        prompt: str,
        system_prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """为建卡 AI 指令（/团 随机、/团 补全）挑选可用模型并生成文本。

        依次尝试插件配置的叙事主模型与备用模型；全部失败时抛出最后一个
        异常，由 ``CardAIComposer`` 统一转换为玩家可见的失败文案。
        """

        config = self.runtime_config()
        primary = str(getattr(config, "provider_id", "") or "").strip()
        if not primary:
            try:
                primary = await self.context.get_current_chat_provider_id(
                    umo=origin
                )
            except Exception:
                primary = ""
        candidates = [primary]
        candidates.extend(
            str(item or "").strip()
            for item in tuple(
                getattr(config, "fallback_provider_ids", ()) or ()
            )
        )
        ordered = list(dict.fromkeys(item for item in candidates if item))
        last_error: Exception | None = None
        for provider_id in ordered:
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt or None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "321开团建卡 AI 模型 %s 调用失败", provider_id, exc_info=True
                )
                continue
            completion = str(
                getattr(response, "completion_text", "") or ""
            ).strip()
            if completion:
                return completion
        if last_error is not None:
            raise last_error
        raise CardAIError(
            "没有可用的语言模型，请先在插件配置中选择叙事模型。"
        )

    async def on_loaded(self):
        config = self.runtime_config()
        cleaned = {"audit_logs": 0}
        try:
            cleaned = await self.database.cleanup(config.audit_retention_days)
        except Exception:
            logger.exception("启动清理失败；继续初始化内置世界与后台服务")
        await self.ensure_builtin_worlds()
        self.database.defer_storage_sync = True
        self._event_outbox_task = await self._background.start(
            "ai-tavern-event-outbox",
            self.event_outbox_worker.run,
            restart="on_failure",
        )
        self._storage_sync_task = await self._background.start(
            "ai-tavern-storage-sync",
            self.storage_sync_worker.run,
            restart="on_failure",
        )
        self._author_job_task = await self._background.start(
            "ai-tavern-author-jobs",
            self.author_job_worker.run,
            restart="on_failure",
        )
        self._timer_task = await self._background.start(
            "ai-tavern-timers", self._timer_loop
        )
        self._backup_task = await self._background.start(
            "ai-tavern-auto-backup", self._backup_loop
        )
        self._webhook_task = await self._background.start(
            "ai-tavern-webhooks", self._webhook_loop
        )
        await self.delivery_worker.start()
        # 1.0.0-A6：独立 Web 面板（监听地址/端口可配置；账号密码与白名单见
        # data_dir/remote_panel.json，由 AstrBot 控制台“独立面板”页或 CLI 维护）。
        if config.remote_panel_enabled:
            self._panel_runtime_status = {
                "state": "starting",
                "message": "独立面板正在启动。",
                "recovery": "请稍候刷新状态。",
            }
            try:
                started = start_panel_server(
                    host=config.remote_panel_host,
                    port=config.remote_panel_port,
                    data_dir=self.database.data_dir,
                    database=self.database,
                    logger=logger,
                    plugin_version=PLUGIN_VERSION,
                    schema_version=DATABASE_SCHEMA_VERSION,
                    web_console=self.web_console,
                    event_loop=asyncio.get_running_loop(),
                    static_root=(
                        Path(__file__).resolve().parents[2] / "pages" / "console"
                    ),
                    allow_insecure_http=config.remote_panel_allow_insecure_http,
                    external_scheme=config.remote_panel_external_scheme,
                    secure_cookie=config.remote_panel_secure_cookie,
                    trusted_proxy_cidrs=config.remote_panel_trusted_proxy_cidrs,
                    card_ai=self.card_commands.ai,
                )
                if started:
                    self._panel_server, self._panel_thread = started
                    self._panel_runtime_status = {
                        "state": "running",
                        "message": f"独立面板正在 {config.remote_panel_host}:{config.remote_panel_port} 监听。",
                        "recovery": "如无法访问，请确认本机防火墙与端口占用。",
                    }
                else:
                    self._panel_server = None
                    self._panel_thread = None
                    self._panel_runtime_status = {
                        "state": "failed",
                        "message": "独立面板启动失败。",
                        "recovery": "请检查监听地址、端口占用、凭据文件和已构建 WebUI 后重试。",
                    }
            except Exception:
                self._panel_server = None
                self._panel_thread = None
                self._panel_runtime_status = {
                    "state": "failed",
                    "message": "独立面板启动失败。",
                    "recovery": "请检查监听地址、端口占用、凭据文件和已构建 WebUI 后重试。",
                }
                logger.exception("321开团独立面板启动失败")
        else:
            self._panel_runtime_status = {
                "state": "disabled",
                "message": "独立面板已在配置中关闭。",
                "recovery": "需要独立访问时，请启用独立 Web 面板并重新加载插件。",
            }
        logger.info(
            "321开团已加载：数据库=%s，清理审计=%s",
            self.database.path,
            cleaned.get("audit_logs", 0),
        )
    async def on_private_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "message_str", "") or "").strip()
        try:
            activated_text = await self._activate_pending_private_card(event)
            await self._deliver_pending(self._event_origin(event))
            command = await self._parse_command_relaxed(
                event,
                message,
                str(event.get_sender_id() or ""),
                "private",
            )
            active_draft = await self.database.card_draft_for_private(
                self._event_origin(event)
            )
        except Exception:
            logger.exception("321开团私聊入口初始化失败")
            event.stop_event()
            yield await self._message_result(
                event,
                "【私聊操作失败】\n"
                "操作：读取当前角色卡流程。\n"
                "原因：系统暂时无法读取保存状态。\n"
                "自动处理：没有修改草稿或推进步骤。\n"
                "下一步：请稍后重试；若持续失败，请管理员在 WebUI 健康检查中确认服务状态。",
            )
            return
        if activated_text:
            event.stop_event()
            handled, fallback = await self._deliver_card_candidate_bundle(
                event,
                activated_text,
                command,
            )
            if not handled:
                yield await self._message_result(
                    event,
                    fallback or activated_text,
                )
            return
        try:
            consumed, response = await self._handle_private_card_service(
                event,
                command,
                message,
                active_draft=active_draft,
            )
            if consumed:
                if response:
                    yield await self._message_result(event, response)
                return
        except Exception:
            logger.exception("321开团私聊建卡应用服务失败")
            event.stop_event()
            yield await self._message_result(
                event,
                "【私聊操作失败】\n"
                "操作：处理本次角色卡输入。\n"
                "原因：系统发生未预期错误。\n"
                "自动处理：本次输入没有继续执行，草稿仍保留在上次成功状态。\n"
                "下一步：发送 /团 当前 查看当前步骤后重试。",
            )
            return
        try:
            sender_id = str(event.get_sender_id() or "")
            consumed, response = await self._handle_growth_command_service(
                event,
                command,
                roles=(
                    ("admin",)
                    if self.runtime_config().is_admin(sender_id)
                    else ()
                ),
                is_private=True,
            )
            if consumed:
                if response:
                    yield await self._message_result(event, response)
                return
        except (DatabaseNotFoundError, PermissionError, ValueError) as exc:
            event.stop_event()
            yield await self._message_result(
                event,
                "【技能成长操作失败】\n"
                f"原因：{exc}\n"
                "自动处理：系统没有升级技能或修改成长记录。\n"
                "下一步：重新发送 /团 成长 查看当前状态。",
            )
            return
        if command.action == "review":
            try:
                response = await self._private_review_web_entry(event, command)
            except Exception:
                logger.exception("321开团私聊网页审核入口失败")
                response = (
                    "【网页审核】生成审核链接失败。\n"
                    "下一步：请稍后重试；若持续失败，请联系管理员检查独立面板状态。"
                )
            if response:
                event.stop_event()
                yield await self._message_result(event, response)
                return
        if active_draft:
            try:
                fields = active_draft.get("fields")
                fields = fields if isinstance(fields, Mapping) else {}
                state = fields.get(WIZARD_DELIVERY_KEY)
                bundle = build_candidate_bundle(
                    active_draft,
                    platform_id=self._platform_id(event),
                )
            except Exception:
                logger.exception("321开团候选预检失败")
                event.stop_event()
                yield await self._message_result(
                    event,
                    "【候选读取失败】\n"
                    "操作：准备当前字段的候选列表。\n"
                    "原因：世界内容缺少有效候选说明或格式不完整。\n"
                    "自动处理：系统没有推进角色卡，已保留当前草稿。\n"
                    "下一步：发送 /团 预览 查看已填内容，并联系主持人修复世界包。",
                )
                return
            if (
                isinstance(state, Mapping)
                and isinstance(bundle, Mapping)
                and cursor_status(bundle, state).get("valid")
                and int(state.get("next_part", 0) or 0)
                < int(state.get("total_parts", 0) or 0)
                and str(state.get("status") or "") in {"pending", "failed"}
                and command.action
                not in {
                    "card_current",
                    "card_next",
                    "card_detail",
                    "card_preview",
                    "card_previous",
                    "card_modify",
                    "card_cancel",
                    "card_restart",
                    "card_abandon",
                }
            ):
                event.stop_event()
                yield await self._message_result(
                    event,
                    "【角色卡候选尚未发送完整】\n"
                    f"已发送 {int(state.get('next_part', 0) or 0)}/"
                    f"{int(state.get('total_parts', 0) or 0)} 段。\n"
                    "系统已保存发送位置，本条输入没有推进角色卡。\n"
                    "下一步：发送 /团 当前 重试；也可发送 "
                    "/团 预览、/团 上一步、/团 取消建卡。",
                )
                return
        try:
            response = await self._handle_private_supplement_message(
                event,
                command,
                message,
            )
            if response:
                event.stop_event()
                handled, fallback = await self._deliver_card_candidate_bundle(
                    event,
                    str(response),
                    command,
                )
                if handled:
                    return
                yield await self._message_result(event, fallback or response)
        except Exception:
            logger.exception("321开团私聊消息边界捕获异常")
            event.stop_event()
            yield await self._message_result(
                event,
                "【私聊操作失败】\n"
                "操作：处理本次角色卡输入。\n"
                "原因：系统发生未预期错误。\n"
                "自动处理：本次输入没有继续执行，草稿仍保留在上次成功状态。\n"
                "下一步：发送 /团 当前 查看当前步骤后重试。",
            )
