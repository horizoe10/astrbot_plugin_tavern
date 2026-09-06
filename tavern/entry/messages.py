from __future__ import annotations

from .plugin_shared import *


class MessageMethods:
    async def _deliver_card_candidate_bundle(
        self,
        event: AstrMessageEvent,
        response: str,
        command: ParsedCommand,
    ) -> tuple[bool, str | None]:
        """Deliver every remaining candidate part and persist progress."""

        origin = self._event_origin(event)
        draft = await self.database.card_draft_for_private(origin)
        if not draft:
            return False, None
        bundle = build_candidate_bundle(
            draft,
            platform_id=self._platform_id(event),
        )
        if not bundle:
            await self.database.set_card_delivery_state(origin, {})
            return False, None
        prompt = format_card_prompt(draft)
        should_deliver = (
            command.action in {"card_current", "card_next"}
            or bool(prompt and str(response).endswith(prompt))
        )
        if not should_deliver or command.action == "card_detail":
            return False, None
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
        remaining = pending_parts(
            bundle,
            cursor,
        )
        if not remaining:
            return False, None
        logical_batch = int(remaining[0].get("logical_batch", 0) or 0)
        prefix = (
            response[: -len(prompt)]
            if prompt and str(response).endswith(prompt)
            else ""
        )
        prefix = generation_notice + prefix
        start_part = int(remaining[0].get("part", 0) or 0)
        failure_count = max(
            0,
            int(cursor.get("failure_count", 0) or 0),
        )
        await self.database.set_card_delivery_state(
            origin,
            delivery_state(
                bundle,
                next_part=start_part,
                status="pending",
                failure_count=failure_count,
            ),
        )
        total_parts = int(bundle.get("part_count", 0) or 0)
        for index, part in enumerate(remaining):
            part_no = int(part.get("part", 0) or 0)
            part_logical_batch = int(
                part.get("logical_batch", logical_batch) or 0
            )
            text = str(part.get("text") or "")
            if index == 0 and prefix:
                text = prefix.rstrip() + "\n" + text
            if not await self._send_event_text(event, text):
                await self.database.set_card_delivery_state(
                    origin,
                    delivery_state(
                        bundle,
                        next_part=part_no,
                        status="failed",
                        error="平台发送失败",
                        error_code="transport_send_failed",
                        failure_count=failure_count + 1,
                    ),
                )
                feedback = (
                    "【候选发送失败】\n"
                    f"操作：发送「{bundle.get('field_label') or '当前字段'}」"
                    f"第 {part_logical_batch + 1} 批候选。\n"
                    "原因：平台没有确认本段消息发送成功。\n"
                    "自动处理：系统已保存未发送位置，角色卡没有推进。\n"
                    "下一步：发送 /团 当前 重试；也可发送 "
                    "/团 预览、/团 上一步、/团 取消建卡。"
                )
                if failure_count + 1 >= 2 and text:
                    feedback += (
                        "\n\n【连续失败降级】以下为本次未送达内容，"
                        "可直接按全局序号作答：\n\n"
                        + text
                    )
                return False, feedback
            next_part = part_no + 1
            await self.database.set_card_delivery_state(
                origin,
                delivery_state(
                    bundle,
                    next_part=next_part,
                    status=(
                        "completed"
                        if next_part >= total_parts
                        else "pending"
                    ),
                    failure_count=0,
                ),
            )
        return True, None

    async def _message_result(self, event: Any, text: Any, config: Any = None):
        """Build the sole synchronous BOT result: Markdown first, once."""

        output = prepare_player_output(text, default_title="酒馆消息")
        if not output.markdown:
            return None
        result = event.plain_result(output.markdown)
        marker = getattr(result, "use_markdown", None)
        if callable(marker):
            marked = marker(True)
            if marked is not None:
                result = marked
        else:
            # Older host objects exposed only the metadata field. Unsupported
            # adapters ignore it and keep their normal plain-text fallback.
            try:
                setattr(result, "use_markdown_", True)
            except Exception:
                logger.warning("321开团宿主结果不支持 Markdown 标志")
        return result


    async def _render_help_image(
        self,
        event: Any,
        *,
        scope: str,
    ) -> str | None:
        """把开团帮助渲染成 PNG，返回本地路径；失败返回 None。

        私聊精简版 / 群聊全指令版共用同一套渲染器。图片写到插件
        数据目录下的 temp 子目录，发送后由宿主负责传输。
        """

        from ..help_renderer import (
            render_group_help_image,
            render_private_help_image,
        )
        try:
            config = self.runtime_config()
            prefix = str(config.trigger_prefix or "").strip()
        except Exception:
            prefix = "t"
        output_dir = str(
            getattr(self, "data_dir", None) or "/tmp"
        ) + "/temp/help"
        try:
            if scope == "private":
                image_path = render_private_help_image(
                    output_dir=output_dir,
                    plugin_version=str(PLUGIN_VERSION),
                )
            else:
                image_path = render_group_help_image(
                    output_dir=output_dir,
                    trigger_prefix=prefix,
                    plugin_version=str(PLUGIN_VERSION),
                )
        except Exception:
            logger.exception("321开团帮助图渲染失败，回退文本")
            return None
        return str(image_path)

    async def _write_security_audit(
        self,
        *,
        sender_id: str,
        action: str,
        group_id: str,
        platform_id: str,
        reason: str,
    ) -> None:
        try:
            await self.database.write_audit(
                "",
                sender_id,
                "security.command_denied",
                group_id,
                {
                    "action": action or "unknown",
                    "platform_id": platform_id,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception("321开团写入安全审计失败")

    @staticmethod
    def _format_private_supplement_status(
        context: Mapping[str, Any],
    ) -> str:
        offers = [
            item
            for item in (context.get("offers") or [])
            if isinstance(item, Mapping)
        ]
        if not offers:
            return (
                "【角色补充】当前没有待确认项目。\n"
                "系统没有修改角色卡。\n"
                "下一步：回到群聊继续故事；有新补充时会主动私聊提醒。"
            )
        lines = [
            "【角色补充】",
            f"「{context.get('character_name') or '角色'}」"
            f"当前有 {len(offers)} 项待确认。",
        ]
        for index, offer in enumerate(offers, start=1):
            lines.append(
                f"{index}. "
                + supplement_list_line(
                    stage=str(offer.get("stage") or ""),
                    field_label=str(
                        offer.get("field_label") or "角色资料"
                    ),
                    state=str(offer.get("state") or "offered"),
                    expired=bool(offer.get("expired")),
                ).removeprefix("· ")
            )
        current = offers[0]
        lines.extend(
            [
                "",
                f"当前处理：{current.get('field_label') or '角色资料'}",
            ]
        )
        candidates = [
            item
            for item in (current.get("candidates") or [])
            if isinstance(item, Mapping)
        ]
        if current.get("free_text"):
            lines.append("请直接回复具体内容。")
        elif candidates:
            for index, option in enumerate(candidates, start=1):
                description = str(option.get("description") or "")
                suffix = f"——{description}" if description else ""
                lines.append(
                    f"{index}. {option.get('label') or ''}{suffix}"
                )
            lines.append(
                "回复序号确认；多选可用逗号分隔，例如：1，3。"
            )
            lines.append("更换候选：拒绝 2")
        lines.extend(
            [
                "暂不处理：暂缓",
                "取消本次：取消",
                "查看状态：/团 当前",
            ]
        )
        return "\n".join(lines)

    async def _handle_private_supplement_message(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        message: str,
    ) -> str | None:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        context = await self.database.supplement_context_for_private(origin)
        if context is None:
            return None
        offers = [
            item
            for item in (context.get("offers") or [])
            if isinstance(item, Mapping)
        ]
        if command.matched and command.action in {
            "card_current",
            "supplement",
        }:
            return self._format_private_supplement_status(context)
        if command.matched or not offers:
            return None
        current = offers[0]
        if bool(current.get("expired")):
            return (
                "【角色补充失败】\n"
                "操作：确认当前角色补充。\n"
                "原因：该补充已过期。\n"
                "自动处理：系统没有修改角色卡。\n"
                "下一步：发送 /团 当前 重新获取可用项目。"
            )
        parsed = parse_supplement_reply(message)
        action = str(parsed.get("action") or "unknown")
        offer_id = str(current.get("offer_id") or "")
        session_id = str(context.get("session_id") or "")
        actor = str(event.get_sender_id() or "")
        expected_revision = int(current.get("revision") or 0)
        candidates = [
            item
            for item in (current.get("candidates") or [])
            if isinstance(item, Mapping)
        ]

        def selected_ids() -> list[str]:
            indexes = [
                int(item)
                for item in (parsed.get("indexes") or [])
                if isinstance(item, int)
            ]
            if (
                not indexes
                or len(set(indexes)) != len(indexes)
                or any(index < 1 or index > len(candidates) for index in indexes)
            ):
                raise ValueError(
                    "候选序号无效。发送 /团 当前 查看当前字段的全局序号。"
                )
            return [
                str(candidates[index - 1].get("id") or "")
                for index in indexes
            ]

        def supplement_key(action_name: str, payload: Mapping[str, Any]) -> str:
            return operation_key(
                session_id,
                f"supplement.{action_name}",
                actor_id=actor,
                source_id=transport_event_id(event),
                payload={
                    "offer": offer_id,
                    "revision": expected_revision,
                    **dict(payload),
                },
            )

        if action == "confirm":
            result = await self.database.confirm_supplement_offer(
                session_id,
                offer_id,
                candidate_ids=selected_ids(),
                actor=actor,
                private_origin=origin,
                expected_revision=expected_revision,
                idempotency_key=supplement_key(
                    "confirm",
                    {"candidate_ids": selected_ids()},
                ),
            )
            return (
                "【角色补充已确认】\n"
                f"{result.get('field_label') or '角色资料'}已写入"
                f"「{context.get('character_name') or '角色'}」的角色卡。\n"
                "系统已更新角色卡阶段，并仅向群聊发送安全公开摘要。\n"
                "下一步：发送 /团 当前 查看是否还有待确认项目。"
            )
        if action == "text":
            if not current.get("free_text"):
                return (
                    "【角色补充未确认】\n"
                    "原因：当前项目需要按序号选择，不能输入任意文本。\n"
                    "系统没有修改角色卡。\n"
                    "下一步：发送 /团 当前 查看有效序号。"
                )
            result = await self.database.confirm_supplement_offer(
                session_id,
                offer_id,
                text_value=str(parsed.get("text") or ""),
                actor=actor,
                private_origin=origin,
                expected_revision=expected_revision,
                idempotency_key=supplement_key(
                    "confirm",
                    {"text": str(parsed.get("text") or "")},
                ),
            )
            return (
                "【角色补充已确认】\n"
                f"{result.get('field_label') or '角色资料'}已安全写入角色卡。\n"
                "群聊只会看到公开摘要，不会公开你的私密内容。\n"
                "下一步：发送 /团 当前 查看剩余项目。"
            )
        if action == "postpone":
            await self.database.postpone_supplement_offer(
                session_id,
                offer_id,
                actor=actor,
                private_origin=origin,
                expected_revision=expected_revision,
                idempotency_key=supplement_key("postpone", {}),
            )
            return (
                "【角色补充已暂缓】\n"
                "系统没有修改角色卡；该项目会在后续剧情窗口重新出现。\n"
                "下一步：回到群聊继续故事，或发送 /团 当前 查看状态。"
            )
        if action == "cancel":
            await self.database.cancel_supplement_offer(
                session_id,
                offer_id,
                actor=actor,
                private_origin=origin,
                expected_revision=expected_revision,
                idempotency_key=supplement_key("cancel", {}),
            )
            return (
                "【角色补充已取消】\n"
                "系统没有修改角色卡；第一幕保底检查仍可能重新提出缺失项目。\n"
                "下一步：发送 /团 当前 查看其他待确认项目。"
            )
        if action == "reject":
            await self.database.reject_supplement_offer(
                session_id,
                offer_id,
                candidate_ids=selected_ids(),
                actor=actor,
                private_origin=origin,
                expected_revision=expected_revision,
                idempotency_key=supplement_key(
                    "reject",
                    {"candidate_ids": selected_ids()},
                ),
            )
            refreshed = await self.database.supplement_context_for_private(
                origin
            )
            return (
                "【候选已更换】\n"
                "系统已记录你拒绝的候选，后续不会立即原样重复。\n\n"
                + self._format_private_supplement_status(
                    refreshed or context
                )
            )
        return (
            "【角色补充未处理】\n"
            "原因：无法识别本次回复。\n"
            "系统没有修改角色卡。\n"
            "下一步：发送 /团 当前 查看可用序号、暂缓和取消方式。"
        )


    async def _run_native_command(
        self,
        event: AstrMessageEvent,
        action: str,
    ) -> str | None:
        """Dispatch a command already matched by AstrBot's native router."""

        event.stop_event()
        getter = getattr(event, "get_message_str", None)
        message = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        )
        parts = message.strip().split(maxsplit=2)
        command = ParsedCommand(
            matched=True,
            action=action,
            argument=parts[2].strip() if len(parts) > 2 else "",
            raw_action=parts[1].strip() if len(parts) > 1 else action,
        )
        config = self.runtime_config()
        group_id = self._group_id(event)
        sender_id = str(event.get_sender_id() or "")
        platform_id = self._platform_id(event)
        logger.info(
            "321开团原生命令：platform=%s group=%s sender=%s command=%s",
            platform_id,
            group_id,
            sender_id,
            action,
        )
        if not group_id:
            if action == "review":
                # 私聊 /团 审核：为主持人签发网页审核链接（/cw/review）。
                # 注意：本方法开头已 stop_event，若不在此拦截，
                # 会落入下方「仅支持群聊」兜底文案。
                return await self._private_review_web_entry(event, command)
            if action in PRIVATE_CARD_ACTIONS:
                activated_text = await self._activate_pending_private_card(event)
                if activated_text:
                    handled, fallback = await self._deliver_card_candidate_bundle(
                        event,
                        activated_text,
                        command,
                    )
                    if handled:
                        return None
                    return fallback or activated_text
                active_draft = await self.database.card_draft_for_private(
                    self._event_origin(event)
                )
                consumed, response = await self._handle_private_card_service(
                    event,
                    command,
                    message,
                    active_draft=active_draft,
                )
                return response if consumed else None
            return "【开团】该指令仅支持群聊。"
        if action in PRIVATE_ONLY_CARD_ACTIONS:
            return "【开团】该建卡命令请在与 Bot 的私聊中使用。"
        try:
            return await self._handle_command(
                event=event,
                command=command,
                config=config,
                group_id=group_id,
                platform_id=platform_id,
                sender_id=sender_id,
            )
        except Exception as exc:
            report_failure(
                logger,
                stage="command",
                operation=str(action),
                exc=exc,
                context={
                    "group": group_id,
                    "command": str(command or "")[:60],
                },
            )
            if isinstance(exc, TavernStoryGenerationError):
                return render_player_message(
                    story_generation_failure_message(
                        exc,
                        operation=(
                            "重试并结算本轮行动"
                            if action == "retry_turn"
                            else "生成本轮故事"
                        ),
                    )
                )
            if action in PLAYER_ACTIONS and isinstance(
                exc, _PLAYER_FACING_COMMAND_ERRORS
            ):
                return _player_command_failure(exc)
            can_respond = config.is_admin(sender_id) or (
                action == "status" and config.public_status
            )
            if can_respond:
                return (
                    "【管理操作失败】\n"
                    "失败操作：处理本次管理命令。\n"
                    "原因：系统发生未预期错误。\n"
                    "自动处理：本次操作没有继续执行，已有副本状态保持不变。\n"
                    "下一步：请稍后重试；若持续失败，请管理员在 WebUI 健康检查中确认服务状态。"
                )
            return None

    async def tavern_start(self, event: AstrMessageEvent):
        """列出副本，或按命令后的副本标识开启。"""

        response = await self._run_native_command(event, "start")
        if response:
            yield await self._message_result(event, response)

    async def tavern_perform(self, event: AstrMessageEvent):
        """完成准备检查并正式开始故事。"""

        response = await self._run_native_command(event, "perform")
        if response:
            yield await self._message_result(event, response)

    async def tavern_pause(self, event: AstrMessageEvent):
        """暂停当前开团会话。"""

        response = await self._run_native_command(event, "pause")
        if response:
            yield await self._message_result(event, response)

    async def tavern_cancel_generation(self, event: AstrMessageEvent):
        """取消当前尚未提交的故事生成，不匹配建卡/托管专用取消。"""

        response = await self._run_native_command(event, "cancel_generation")
        if response:
            yield await self._message_result(event, response)

    async def tavern_retry_turn(self, event: AstrMessageEvent):
        """复用已锁定的决定、检定和操作回执重试本轮。"""

        response = await self._run_native_command(event, "retry_turn")
        if response:
            yield await self._message_result(event, response)

    async def tavern_recover(self, event: AstrMessageEvent):
        """进入恢复准备大厅，但不恢复剧情或计时。"""

        response = await self._run_native_command(event, "recover")
        if response:
            yield await self._message_result(event, response)
