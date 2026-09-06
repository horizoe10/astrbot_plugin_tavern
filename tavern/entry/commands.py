from __future__ import annotations

from .plugin_shared import *


class CommandMethods:
    async def tavern_resume(self, event: AstrMessageEvent):
        """在恢复准备完成后正式续演。"""

        response = await self._run_native_command(event, "resume")
        if response:
            yield await self._message_result(event, response)

    async def tavern_close(self, event: AstrMessageEvent):
        """关闭当前开团会话。"""

        response = await self._run_native_command(event, "close")
        if response:
            yield await self._message_result(event, response)

    async def tavern_finish(self, event: AstrMessageEvent):
        """二次确认后归档当前故事。"""

        response = await self._run_native_command(event, "finish")
        if response:
            yield await self._message_result(event, response)

    async def tavern_abort(self, event: AstrMessageEvent):
        """二次确认后异常终止并永久归档当前故事。"""

        response = await self._run_native_command(event, "abort")
        if response:
            yield await self._message_result(event, response)

    async def tavern_safety_pause(self, event: AstrMessageEvent):
        """任一出场玩家都可立即冻结故事与全部计时。"""

        response = await self._run_native_command(event, "safety_pause")
        if response:
            yield await self._message_result(event, response)

    async def tavern_maintenance(self, event: AstrMessageEvent):
        """将当前开团会话切换至维护状态。"""

        response = await self._run_native_command(event, "maintenance")
        if response:
            yield await self._message_result(event, response)

    async def tavern_status(self, event: AstrMessageEvent):
        """查看当前开团会话状态。"""

        response = await self._run_native_command(event, "status")
        if response:
            yield await self._message_result(event, response)

    async def tavern_dm(self, event: AstrMessageEvent):
        """切换真人 DM、推进叙事、交棒或恢复自动模式。"""

        response = await self._run_native_command(event, "dm")
        if response:
            yield await self._message_result(event, response)

    async def tavern_save(self, event: AstrMessageEvent):
        """保存当前开团会话。"""

        response = await self._run_native_command(event, "save")
        if response:
            yield await self._message_result(event, response)

    async def tavern_delete_save(self, event: AstrMessageEvent):
        """删除普通手动存档。"""

        response = await self._run_native_command(event, "delete_save")
        if response:
            yield await self._message_result(event, response)

    async def tavern_load(self, event: AstrMessageEvent):
        """读取当前开团存档。"""

        response = await self._run_native_command(event, "load")
        if response:
            yield await self._message_result(event, response)

    async def tavern_rollback(self, event: AstrMessageEvent):
        """回滚当前开团会话的上一回合。"""

        response = await self._run_native_command(event, "rollback")
        if response:
            yield await self._message_result(event, response)

    async def tavern_worlds(self, event: AstrMessageEvent):
        """列出可用世界包。"""

        response = await self._run_native_command(event, "worlds")
        if response:
            yield await self._message_result(event, response)

    async def tavern_instances(self, event: AstrMessageEvent):
        """列出当前群可选择的剧情副本。"""

        response = await self._run_native_command(event, "instances")
        if response:
            yield await self._message_result(event, response)

    async def tavern_join(self, event: AstrMessageEvent):
        """加入当前群的多人回合队列。"""

        response = await self._run_native_command(event, "join")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card(self, event: AstrMessageEvent):
        """在群内查看建卡码，或在私聊中绑定建卡码。"""

        response = await self._run_native_command(event, "card")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_fill(self, event: AstrMessageEvent):
        """在私聊中填写当前角色卡字段。"""

        response = await self._run_native_command(event, "card_fill")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_previous(self, event: AstrMessageEvent):
        """返回并重新填写上一个可见建卡字段。"""

        response = await self._run_native_command(event, "card_previous")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_modify(self, event: AstrMessageEvent):
        """按玩家可见字段名称修改已有角色卡字段。"""

        response = await self._run_native_command(event, "card_modify")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_current(self, event: AstrMessageEvent):
        """重新显示当前建卡步骤与对应预设。"""

        response = await self._run_native_command(event, "card_current")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_preview(self, event: AstrMessageEvent):
        """在私聊中预览完整角色卡。"""

        response = await self._run_native_command(event, "card_preview")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_stats_reset(self, event: AstrMessageEvent):
        """保留文字角色资料，仅重新分配角色数值。"""

        response = await self._run_native_command(
            event,
            "card_stats_reset",
        )
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_timer_notice(self, event: AstrMessageEvent):
        """在私聊中查询、开启或关闭角色卡倒计时提示。"""

        response = await self._run_native_command(
            event,
            "card_timer_notice",
        )
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_confirm(self, event: AstrMessageEvent):
        """在私聊中提交角色卡审核。"""

        response = await self._run_native_command(event, "card_confirm")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_cancel(self, event: AstrMessageEvent):
        """在私聊中取消角色卡草稿，并保留席位与私聊绑定。"""

        response = await self._run_native_command(event, "card_cancel")
        if response:
            yield await self._message_result(event, response)

    async def tavern_rescue(self, event: AstrMessageEvent):
        """在世界声明的救援窗口内救援一名濒危角色。"""

        response = await self._run_native_command(event, "rescue")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_next(self, event: AstrMessageEvent):
        """继续发送当前字段的下一批候选。"""

        response = await self._run_native_command(event, "card_next")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_detail(self, event: AstrMessageEvent):
        """按当前字段的全局序号查看完整候选说明。"""

        response = await self._run_native_command(event, "card_detail")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_restart(self, event: AstrMessageEvent):
        """保留席位与私聊绑定，创建全新的角色卡草稿。"""

        response = await self._run_native_command(event, "card_restart")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_rename(self, event: AstrMessageEvent):
        """在确认建卡前直接修改角色名。"""

        response = await self._run_native_command(event, "card_rename")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_nickname(self, event: AstrMessageEvent):
        """在确认建卡前直接修改昵称或副本代号。"""

        response = await self._run_native_command(event, "card_nickname")
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_random(self, event: AstrMessageEvent):
        """由 AI 随机生成当前建卡字段的设定内容并填入。"""

        async for result in self._run_card_ai_command(event, "card_random"):
            yield result

    async def tavern_card_expand(self, event: AstrMessageEvent):
        """基于玩家的初始设定，由 AI 补全当前建卡字段。"""

        async for result in self._run_card_ai_command(event, "card_expand"):
            yield result

    async def tavern_card_web(self, event: AstrMessageEvent):
        """签发网页建卡链接（一次性、15 分钟有效）。"""

        response = await self._run_native_command(event, "card_web")
        if response:
            yield await self._message_result(event, response)

    async def _run_card_ai_command(
        self,
        event: AstrMessageEvent,
        action: str,
    ):
        """先发送生成中提示，再执行 AI 建卡指令并转发结果。

        模型生成通常需要数秒到数十秒；提示只在私聊中发送，群聊里
        这两个指令会被私聊限制文案直接拒绝，无需额外噪音。
        """

        if not self._group_id(event):
            yield await self._message_result(
                event,
                "【AI设定生成中】正在调用语言模型撰写设定，"
                "通常需要十几秒，请稍候…",
            )
        response = await self._run_native_command(event, action)
        if response:
            yield await self._message_result(event, response)

    async def tavern_card_abandon(self, event: AstrMessageEvent):
        """二次确认后释放尚未建立正式角色的席位。"""

        response = await self._run_native_command(event, "card_abandon")
        if response:
            yield await self._message_result(event, response)

    async def tavern_character(self, event: AstrMessageEvent):
        """查看自己的副本角色状态。"""

        response = await self._run_native_command(event, "character")
        if response:
            yield await self._message_result(event, response)

    async def tavern_tendency(self, event: AstrMessageEvent):
        """查看、忽略或恢复本人在当前副本中的倾向依据。"""

        response = await self._run_native_command(event, "tendency")
        if response:
            yield await self._message_result(event, response)

    async def tavern_ready(self, event: AstrMessageEvent):
        """在准备大厅确认本次出场。"""

        response = await self._run_native_command(event, "ready")
        if response:
            yield await self._message_result(event, response)

    async def tavern_force_ready(self, event: AstrMessageEvent):
        """由主持人将全部合格出场角色设为已准备。"""

        response = await self._run_native_command(event, "force_ready")
        if response:
            yield await self._message_result(event, response)

    async def tavern_roster(self, event: AstrMessageEvent):
        """查看当前角色卡、准备与入场状态。"""

        response = await self._run_native_command(event, "roster")
        if response:
            yield await self._message_result(event, response)

    async def tavern_review(self, event: AstrMessageEvent):
        """列出、查看并处理待审核角色卡。"""

        response = await self._run_native_command(event, "review")
        if response:
            yield await self._message_result(event, response)

    async def tavern_choose(self, event: AstrMessageEvent):
        """选择当前回合的 A/B/C/D 行动。"""

        response = await self._run_native_command(event, "choose")
        if response:
            unsent = await self._send_event_parts(
                event,
                _story_reply_parts(response),
            )
            if unsent:
                yield await self._message_result(
                    event,
                    self._render_unsent_parts(unsent),
                )

    async def tavern_reroll(self, event: AstrMessageEvent):
        """免费重整本回合选项一次。"""

        await self._send_event_text(
            event,
            PlayerMessage.dynamic(
                title="重整行动选项",
                summary="重整请求已收到，正在准备一组新的行动方向。",
                sections=("自动处理：当前故事和角色状态保持不变。",),
                source="story_generation_progress",
            ),
        )
        response = await self._run_native_command(event, "reroll")
        if response:
            yield await self._message_result(event, response)

    async def tavern_inspiration(self, event: AstrMessageEvent):
        """查看灵感，或在选择检定选项时消耗一点取得优势。"""

        getter = getattr(event, "get_message_str", None)
        raw_message = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        )
        choosing = len(raw_message.strip().split()) >= 3
        response = await self._run_native_command(event, "inspiration")
        if response:
            if choosing:
                unsent = await self._send_event_parts(
                    event,
                    _story_reply_parts(response),
                )
                if unsent:
                    yield await self._message_result(
                        event,
                        self._render_unsent_parts(unsent),
                    )
            else:
                yield await self._message_result(event, response)

    async def tavern_inspiration_reroll(self, event: AstrMessageEvent):
        """选择检定选项并消耗一点灵感重投完整骰池。"""

        response = await self._run_native_command(
            event,
            "inspiration_reroll",
        )
        if response:
            unsent = await self._send_event_parts(
                event,
                _story_reply_parts(response),
            )
            if unsent:
                yield await self._message_result(
                    event,
                    self._render_unsent_parts(unsent),
                )

    async def tavern_vote(self, event: AstrMessageEvent):
        """参与当前集体决策。"""

        response = await self._run_native_command(event, "vote")
        if response:
            yield await self._message_result(event, response)

    async def tavern_team(self, event: AstrMessageEvent):
        """0.11.3：发起「全队行动」集体表决（不占用个人行动机会）。

        主入口为 A—D 字母选项（🛡全队），本指令保留为便捷入口。"""

        response = await self._run_native_command(event, "team")
        if response:
            yield await self._message_result(event, response)

    async def tavern_countdown(self, event: AstrMessageEvent):
        """查询、总开关或逐类开关副本倒计时。"""

        response = await self._run_native_command(event, "countdown")
        if response:
            yield await self._message_result(event, response)

    async def tavern_usage(self, event: AstrMessageEvent):
        """查看当前群与副本的模型 Token 用量。"""

        response = await self._run_native_command(event, "usage")
        if response:
            yield await self._message_result(event, response)

    async def tavern_quota(self, event: AstrMessageEvent):
        """设置当前群或副本的滚动 Token 限额。"""

        response = await self._run_native_command(event, "quota")
        if response:
            yield await self._message_result(event, response)

    async def tavern_delete_session(self, event: AstrMessageEvent):
        """删除已关闭或已归档副本并移入回收目录。"""

        response = await self._run_native_command(event, "delete_session")
        if response:
            yield await self._message_result(event, response)

    async def tavern_away(self, event: AstrMessageEvent):
        """暂离回合队列但保留席位。"""

        response = await self._run_native_command(event, "away")
        if response:
            yield await self._message_result(event, response)

    async def tavern_return_queue(self, event: AstrMessageEvent):
        """从下一轮队尾重新加入行动。"""

        response = await self._run_native_command(event, "return_queue")
        if response:
            yield await self._message_result(event, response)

    async def tavern_return_request(self, event: AstrMessageEvent):
        """为已退场角色申请剧情返场。"""

        response = await self._run_native_command(event, "return_request")
        if response:
            yield await self._message_result(event, response)

    async def tavern_leave(self, event: AstrMessageEvent):
        """退出当前群的多人回合队列。"""

        response = await self._run_native_command(event, "leave")
        if response:
            yield await self._message_result(event, response)

    async def tavern_order(self, event: AstrMessageEvent):
        """查看当前轮次与行动顺序。"""

        response = await self._run_native_command(event, "order")
        if response:
            yield await self._message_result(event, response)

    async def tavern_skip(self, event: AstrMessageEvent):
        """当前玩家主动跳过自己的行动。"""

        response = await self._run_native_command(event, "skip")
        if response:
            yield await self._message_result(event, response)

    async def tavern_next(self, event: AstrMessageEvent):
        """管理员强制跳过当前行动者。"""

        response = await self._run_native_command(event, "next")
        if response:
            yield await self._message_result(event, response)

    async def tavern_help(self, event: AstrMessageEvent):
        """显示开团指令帮助（渲染为图片）。
        私聊 / 群聊都可用。图片渲染失败时回退到纯文本。"""

        import logging as _logging
        _logging.getLogger("ai_tavern.cmd").info(
            "tavern_help ENTRY group_id=%r", self._group_id(event)
        )
        scope = "private" if not self._group_id(event) else "group"
        image_path = await self._render_help_image(event, scope=scope)
        if image_path:
            event.stop_event()  # 必须 stop_event，否则 priority=100 的 on_group_message 会再发 contextual_help 大段文本
            yield event.image_result(image_path)
            return
        response = await self._run_native_command(event, "help")
        if response:
            yield await self._message_result(event, response)

    async def _timer_loop(self) -> None:
        # 自愈循环：异常时原地重试，不再另起一个 task。
        # 异常分支不得派生新循环，避免叠加并行轮询和重复提醒。
        while True:
            try:
                notifications = await self.database.process_due_timers()
                for item in notifications:
                    await self._send_timer_notice(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("321开团计时器轮询异常，将在稍后重试")
            await asyncio.sleep(TIMER_POLL_INTERVAL_SECONDS)

    def _timer_notice_should_skip(self, item: Mapping[str, Any]) -> bool:
        """同一计时器短时间内重复触发时丢弃，防止刷屏。"""

        key = "|".join(
            (
                str(item.get("session_id") or ""),
                str(item.get("timer_type") or ""),
                str(item.get("participant_id") or ""),
                str(item.get("kind") or ""),
            )
        )
        now = time.monotonic()
        last = self._timer_notice_last_sent.get(key)
        if last is not None and now - last < TIMER_NOTICE_DEDUP_SECONDS:
            return True
        self._timer_notice_last_sent[key] = now
        if len(self._timer_notice_last_sent) > 512:
            cutoff = now - TIMER_NOTICE_DEDUP_SECONDS * 4
            self._timer_notice_last_sent = {
                cached_key: seen
                for cached_key, seen in self._timer_notice_last_sent.items()
                if seen >= cutoff
            }
        return False

    async def _send_timer_notice(self, item: Mapping[str, Any]) -> None:
        if self._timer_notice_should_skip(item):
            return
        try:
            session = await self.database.get_session(
                str(item.get("session_id") or "")
            )
            instance_config = await self.database.get_instance_config(
                session["id"]
            )
            timer_type = str(item.get("timer_type") or "")
            if timer_type != "card_completion" and not (
                instance_config["time_rules"].get(
                    "announce_timeouts",
                    True,
                )
            ):
                return
            kind = str(item.get("kind") or "")
            labels = {
                "turn": "行动回合",
                "vote": "集体投票",
                "card_code": "私聊建卡码",
                "card_completion": "角色卡创建",
                "ready": "准备确认",
                "preparation": "准备大厅",
                "standby": "候补保留",
                "all_idle": "全员无互动",
            }
            if kind == "idle_pause":
                text = (
                    "【副本已自动暂停】\n"
                    "全员超过设定时间没有进行跑团互动。\n\n"
                    "自动处理\n"
                    "系统已冻结全部计时，并保留角色卡、投票和当前行动权。\n\n"
                    "下一步\n"
                    "/团 恢复\n\n"
                    "全员重新准备后，由主持人发送：\n"
                    "/团 继续"
                )
            elif kind == "reminder":
                remaining = _format_remaining_time(
                    item.get("remaining_seconds")
                )
                titles = {
                    "turn": "回合时间提醒",
                    "vote": "投票时间提醒",
                    "card_code": "建卡入口提醒",
                    "card_completion": "角色卡完成提醒",
                    "ready": "准备确认提醒",
                    "preparation": "准备时间提醒",
                    "standby": "候补席位提醒",
                }
                policies = {
                    "turn": "系统会保留当前行动权和原有选项，不会替玩家自动选择。",
                    "vote": "系统会按当前副本的投票规则结算，尚未提交的玩家不会被代投。",
                    "card_code": "建卡席位会继续保留；验证码到期后可由系统安全补发。",
                    "card_completion": "角色卡草稿会保留；关闭提醒可发送：\n/团 建卡提醒 关",
                    "ready": "当前角色和席位会保留，不会自动替你确认准备。",
                    "preparation": "已完成的准备内容会保留，不会重复写入。",
                    "standby": "保留期结束后将按副本规则释放候补位置。",
                }
                text = render_message_type(
                    "timer.reminder",
                    {
                        "title": titles.get(timer_type, "时间提醒"),
                        "subject": labels.get(timer_type, "当前事项"),
                        "duration": remaining,
                        "result": policies.get(
                            timer_type,
                            "系统会保留已经完成的内容，并按当前副本规则处理。",
                        ),
                    },
                    audience=(
                        "player"
                        if timer_type == "card_completion"
                        else "public"
                    ),
                )
            else:
                timeout_titles = {
                    "turn": "本回合已超时",
                    "vote": "本次投票已到期",
                    "card_code": "建卡入口已到期",
                    "card_completion": "角色卡创建已超时",
                    "ready": "准备确认已超时",
                    "preparation": "准备阶段已到期",
                    "standby": "候补保留已到期",
                }
                timeout_results = {
                    "turn": "行动权和原有选项已经保留，系统没有替玩家选择。",
                    "vote": "系统已按副本投票规则保存并结算现有票数。",
                    "card_code": "原验证码已停止使用；角色卡草稿和席位仍然保留。",
                    "card_completion": "角色卡草稿仍然保留，没有提交不完整角色卡。",
                    "ready": "当前角色和席位仍然保留，准备状态没有被代为确认。",
                    "preparation": "已完成的准备内容仍然保留。",
                    "standby": "系统已按副本规则处理候补位置。",
                }
                text = render_message_type(
                    "timer.timeout",
                    {
                        "title": timeout_titles.get(timer_type, "时间已到"),
                        "subject": labels.get(timer_type, "当前事项"),
                        "result": timeout_results.get(
                            timer_type,
                            "系统已按当前副本的超时规则处理，并保留可恢复内容。",
                        ),
                        "next_command": "/团 当前",
                    },
                    audience=(
                        "player"
                        if timer_type == "card_completion"
                        else "public"
                    ),
                )
            origin = str(session.get("unified_origin") or "")
            targets = item.get("targets")
            target_items: list[Mapping[str, Any]] = []
            if isinstance(targets, Sequence) and not isinstance(
                targets,
                (str, bytes),
            ):
                seen: set[str] = set()
                for target in targets:
                    if not isinstance(target, Mapping):
                        continue
                    user_id = str(target.get("user_id") or "")
                    if not user_id or user_id in seen:
                        continue
                    seen.add(user_id)
                    target_items.append(target)

            private_delivery = timer_type == "card_completion"
            if private_delivery:
                target_user_id = next(
                    (
                        str(target_item.get("user_id") or "")
                        for target_item in target_items
                        if target_item.get("user_id")
                    ),
                    "",
                )
                if not target_user_id:
                    return
                target = await self._resolve_private_delivery_target(
                    session_id=str(session["id"]),
                    platform_id=str(session.get("platform_id") or ""),
                    user_id=target_user_id,
                )
                if target is None:
                    # 参与者已退场或无有效席位：不再向撤销绑定投递提醒。
                    return
                outcome = await self.delivery_service.send(
                    session_id=str(session["id"]),
                    target=target,
                    kind="card_reminder",
                    text=text,
                    audience=AUDIENCE_PRIVATE_OWNER,
                    dedupe_key=(
                        f"timer:{item.get('timer_id') or session['id']}:"
                        f"{timer_type}:{kind}:{item.get('deadline_at') or ''}"
                    ),
                    meta={
                        "recipient_name": str(
                            target_items[0].get("display_name") or ""
                        )
                        if target_items
                        else "",
                    },
                )
                if not outcome.ok:
                    logger.warning(
                        "321开团建卡提醒等待后台投递：session=%s status=%s",
                        session["id"],
                        outcome.status,
                    )
                return
            readable_targets = [
                str(target.get("display_name") or "").strip()
                for target in target_items
                if str(target.get("display_name") or "").strip()
            ]
            missing_public_name = bool(target_items) and any(
                not str(target.get("display_name") or "").strip()
                for target in target_items
            )
            if missing_public_name:
                text = (
                    "【计时提醒未完整发送】\n"
                    "操作：向指定玩家发送本次计时提醒。\n"
                    "原因：至少一名接收者缺少可公开显示的名称。\n"
                    "自动处理：系统已跳过定向提醒，群消息中没有显示账号标识。\n"
                    "下一步：请联系主持人修复角色资料后查看：\n\n"
                    "/团 阵容"
                )
            elif kind == "reminder" and timer_type in {"vote", "preparation"} and not readable_targets:
                return
            elif readable_targets:
                text = "、".join(f"@{name}" for name in readable_targets) + "\n" + text
            # 主动消息之间保持最小间隔，避免一次吐出多条时触发
            # QQ 官方频控（40034100），进而被整批拒绝。
            gap = time.monotonic() - self._timer_notice_last_at
            if gap < TIMER_NOTICE_MIN_GAP_SECONDS:
                await asyncio.sleep(TIMER_NOTICE_MIN_GAP_SECONDS - gap)
            self._timer_notice_last_at = time.monotonic()
            sent = await self._send_or_queue(
                session_id=str(session["id"]),
                origin=origin,
                text=text,
                kind=f"timer.{timer_type}.{kind}",
                dedupe_key=(
                    f"timer:{item.get('timer_id') or session['id']}:{timer_type}:"
                    f"{kind}:{item.get('deadline_at') or ''}"
                ),
            )
            if not sent:
                logger.warning(
                    "321开团计时通知已进入待投递队列：session=%s",
                    session["id"],
                )
        except Exception:
            logger.exception(
                "321开团计时通知发送失败：session=%s",
                item.get("session_id"),
            )

    @staticmethod
    def _looks_like_bare_tavern(text: str) -> bool:
        """Whether ``text`` is a tavern command missing its ``/`` prefix."""

        return text == "团" or (
            text.startswith("团") and text[1:2].isspace()
        )

    async def _parse_command_relaxed(
        self,
        event: AstrMessageEvent,
        message: str,
        actor_id: str = "",
        target: str = "",
    ) -> ParsedCommand:
        """Parse a tavern command, tolerating a missing ``/`` prefix.

        某些适配器会在原生命令管线前处理命令前缀；这里仅对可确认的
        ``/``，并且把「视为被 @」的唤醒标记推迟到 handler 阶段才补写。
        而 AstrBot 的 ``CommandFilter`` 在更早的唤醒检查阶段就会因
        ``is_at_or_wake_command`` 为假而把全部原生指令过滤掉，导致指令
        静默失效。因此这里不再依赖唤醒标记，直接按裸指令兜底解析。

        0.12.0-A3：兜底解析命中时写入 ``command.relaxed_parse`` 审计，
        供总览「群内指令」统计斜杠兜底解析命中数。
        """

        command = parse_tavern_command(message)
        if command.matched:
            return command
        text = str(message or "").strip()
        if not self._looks_like_bare_tavern(text):
            return command
        relaxed = parse_tavern_command("/" + text)
        if not relaxed.matched:
            return command
        if relaxed.action not in ("unknown", "help"):
            if actor_id:
                try:
                    await self.database.write_audit(
                        "",
                        actor_id,
                        "command.relaxed_parse",
                        target,
                        {
                            "action": relaxed.action,
                            "platform_id": self._platform_id(event),
                        },
                    )
                except Exception:
                    logger.debug(
                        "321开团兜底解析审计写入失败",
                        exc_info=True,
                    )
            return relaxed
        # 裸「开团」与无法识别的动作只在确实被唤醒时回应，
        # 避免把普通群聊里的日常用词误当成指令。
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return relaxed
        return command

    async def _handle_command(
        self,
        *,
        event: AstrMessageEvent,
        command: ParsedCommand,
        config: TavernConfig,
        group_id: str,
        platform_id: str,
        sender_id: str,
    ) -> str | None:
        """Route every group command through the application authority."""

        command = self._normalize_application_command(command)
        session = await self.database.get_session_by_group(
            platform_id,
            group_id,
        )
        roles = set(
            await self.database.permission_roles(
                str(session["id"]),
                sender_id,
            )
            if session
            else ()
        )
        if config.is_admin(sender_id):
            roles.add("admin")
        ctx = from_astrbot_event(
            event,
            session_id=str((session or {}).get("id") or ""),
            roles=roles,
            metadata={
                "transport_event_id": transport_event_id(event),
                "command_source": "bot_group",
            },
            platform_id=platform_id,
            group_id=group_id,
            user_id=sender_id,
            is_private=False,
        )
        if session:
            ctx = RequestContext(
                **{
                    **ctx.to_dict(),
                    "expected_revision": int(session.get("revision") or 0),
                }
            )
        invocation = _BotApplicationInvocation(
            parsed=command,
            event=event,
            config=config,
            group_id=group_id,
            platform_id=platform_id,
            sender_id=sender_id,
        )
        _, orchestrator = self._application_authority()
        result = await orchestrator.execute(
            ctx,
            invocation,
        )
        return render_bot_result(result)
