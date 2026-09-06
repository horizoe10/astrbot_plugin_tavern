from __future__ import annotations

from .plugin_shared import *


async def _legacy_instance_time_rules(
    database: Any,
    config: TavernConfig,
    world_time: Mapping[str, Any] | None,
    actor_id: str,
) -> dict[str, Any]:
    recorded = await database.record_configuration_revision(
        config.to_mapping(),
        actor_id,
    )
    global_revision = int(recorded.get("revision") or 0)
    if global_revision < 1:
        raise RuntimeError("当前全局提醒设置没有可冻结的修订")
    merged = {
        **dict(config.time_rules),
        **(dict(world_time) if isinstance(world_time, Mapping) else {}),
    }
    reminder = dict(config.time_rules.get("story_generation_reminder") or {})
    reminder.update(
        {
            "source": "global_default",
            "revision": 0,
            "source_revision": global_revision,
        }
    )
    merged["story_generation_reminder"] = reminder
    return dict(normalize_time_rules(merged))


class LegacyCommandMethods:
    async def _handle_legacy_command_part_1(
        self,
        *,
        event,
        command,
        config,
        group_id,
        platform_id,
        sender_id,
        session,
        roles,
        is_admin,
        is_host,
        is_moderator,
        is_active_dm,
        control_state,
    ):
        if command.action == "worlds":
            ctx = from_astrbot_event(
                event,
                session_id=str((session or {}).get("id") or ""),
                roles=("admin",) if is_admin else (),
                platform_id=platform_id,
                group_id=group_id,
                user_id=sender_id,
            )
            result = await self.world_commands.handle(ctx, command)
            return result.text

        if command.action == "instances":
            page = parse_instance_list_page(
                command.argument,
                allow_bare_number=True,
            )
            if page is None:
                return (
                    "【开团】格式：/团 副本列表 [页码]\n"
                    "例如：/团 副本列表 2"
                )
            instances = await self.database.list_group_sessions(
                platform_id,
                group_id,
            )
            if not instances:
                await self.ensure_builtin_worlds()
            worlds = (
                await self.database.list_worlds()
                if not instances
                else None
            )
            return format_instance_list(
                instances,
                worlds,
                page=page,
            )

        if command.action == "start":
            list_page = parse_instance_list_page(command.argument)
            if not command.argument or list_page is not None:
                instances = await self.database.list_group_sessions(
                    platform_id,
                    group_id,
                )
                if not instances:
                    await self.ensure_builtin_worlds()
                worlds = (
                    await self.database.list_worlds()
                    if not instances
                    else None
                )
                prefix = (
                    "当前群已完成授权，但尚未启动任何副本。\n"
                    if auto_bound
                    else ""
                )
                return prefix + format_instance_list(
                    instances,
                    worlds,
                    page=list_page or 1,
                )

            start_ref = str(command.argument or "").strip()
            group_sessions = await self.database.list_group_sessions(
                platform_id,
                group_id,
            )
            selected = None
            if start_ref.isdigit():
                selection = int(start_ref)
                if 1 <= selection <= len(group_sessions):
                    selected = group_sessions[selection - 1]
            if selected is None:
                name_matches = [
                    item
                    for item in group_sessions
                    if str(item.get("instance_name") or "").strip()
                    == start_ref
                ]
                if len(name_matches) == 1:
                    selected = name_matches[0]
                elif len(name_matches) > 1:
                    raise ValueError(
                        "当前群有多个同名副本，请使用副本列表中的序号。"
                    )
            if selected is None:
                selected = await self.database.get_session_by_group_ref(
                    platform_id,
                    group_id,
                    start_ref,
                )
            if not selected:
                await self.ensure_builtin_worlds()
                worlds = await self.database.list_worlds()
                if start_ref.isdigit():
                    selection = int(start_ref)
                    if 1 <= selection <= len(worlds):
                        start_ref = str(worlds[selection - 1]["id"])
                else:
                    world_matches = [
                        world
                        for world in worlds
                        if str(world.get("name") or "").strip()
                        == start_ref
                    ]
                    if len(world_matches) == 1:
                        start_ref = str(world_matches[0]["id"])
                    elif len(world_matches) > 1:
                        raise ValueError(
                            "存在多个同名世界，请使用世界列表中的序号。"
                        )
            created = not selected or bool(
                selected
                and selected.get("state") == SESSION_FINISHED
            )
            if not selected:
                session = await self.database.ensure_session(
                    platform_id,
                    group_id,
                    str(event.unified_msg_origin or ""),
                    start_ref,
                    sender_id,
                )
            elif selected.get("state") == SESSION_FINISHED:
                session = await self.database.ensure_session(
                    platform_id,
                    group_id,
                    str(event.unified_msg_origin or ""),
                    str(selected["world_id"]),
                    sender_id,
                    str(selected["instance_slug"]),
                    str(selected["instance_name"]),
                )
            else:
                session = selected
            if created:
                created_world = await self.database.get_world(
                    session["world_id"]
                )
                world_rules = created_world.get("rules") or {}
                world_time = (
                    world_rules.get("time_rules")
                    if isinstance(world_rules, Mapping)
                    else {}
                )
                merged_time_rules = await _legacy_instance_time_rules(
                    self.database,
                    config,
                    world_time,
                    sender_id,
                )
                await self.database.save_instance_time_rules(
                    session["id"],
                    merged_time_rules,
                    sender_id,
                )
            elif int(session.get("turn_no") or 0) > 0:
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
            session = await self.database.transition_session(
                session["id"],
                SESSION_PREPARING,
                sender_id,
            )
            await self.database.grant_permission(
                session["id"],
                sender_id,
                "host",
                sender_id,
            )
            instance = await self.database.get_instance_config(
                session["id"]
            )
            world = instance["world_snapshot"]
            limits = player_limits(world)
            roster = await self.database.list_roster(session["id"])
            summary = str(
                session["world_state"].get("scene_summary")
                or world.get("description")
                or "尚无剧情回顾"
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "prepare",
                    "hook": "session_created",
                    "session_id": session["id"],
                }
            )
            return (
                f"【开团已开启】《{session['instance_name']}》"
                "\n当前阶段：准备中（故事尚未推进）"
                f"\n世界：{session['world_name']}"
                f"\n推荐人数：{limits['recommended_min']}"
                f"—{limits['recommended_max']} 人"
                f" · 最低 {limits['minimum_start']} 人"
                f" · 强制上限 {limits['maximum']} 人"
                + (
                    "\n已自动将当前群加入允许群列表。"
                    if auto_bound
                    else ""
                )
                + "\n\n"
                + format_gameplay_brief(world)
                + f"\n\n【故事回顾】{summary}"
                + "\n\n"
                + format_roster(roster)
                + (
                    (
                        "\n\n这是已有剧情进度的副本，暂停时的对话、"
                        "行动者、投票与选项均已保留。"
                        "\n全员确认准备后，主持人发送 /团 继续；"
                        "不要使用 /团 开演。"
                    )
                    if int(session.get("turn_no") or 0) > 0
                    else (
                        "\n\n玩家发送 /团 加入，按提示私聊建卡，"
                        "完成后发送 /团 准备。"
                        "\n主持人最后发送 /团 开演；此时不会自动开演。"
                    )
                )
            )

        if command.action == "status":
            if not session:
                return "【开团状态】尚未为本群创建会话。"
            location = session["world_state"].get("location", "未记录")
            turn = await self.database.get_turn_status(session["id"])
            roster = await self.database.list_roster(session["id"])
            vote = await self.database.active_vote(session["id"])
            choice = await self.database.active_choice_set(session["id"])
            rules = await self.database.get_session_rule_state(
                session["id"]
            )
            progress = rules.get("progress") or {}
            total_milestones = int(
                progress.get("total_milestones") or 0
            )
            completed_milestones = int(
                progress.get("completed_milestones") or 0
            )
            progress_text = (
                f"{completed_milestones}/{total_milestones}"
                f"（{round(completed_milestones * 100 / total_milestones)}%）"
                if total_milestones > 0
                else "未设置正式里程碑"
            )
            current = (
                turn["current_name"]
                or (
                    "已指定，等待角色资料"
                    if turn.get("current_user_id")
                    else "等待玩家加入"
                )
            )
            workflow = (
                f"集体投票第 {vote['stage']} 轮"
                if vote
                else (
                    "等待 A/B/C/D 选择"
                    if choice
                    else "无活动流程"
                )
            )
            control = await self.database.get_control_state(session["id"])
            control_text = (
                "真人主持 · "
                f"{'已指定' if control.get('active_dm_user_id') else '未指定'}"
                f" · 第 {control.get('beat_no', 0)} 段"
                if control.get("mode") == "dm"
                else "AI 自动"
            )
            return (
                "【开团状态】\n"
                f"状态：{_SESSION_STATE_LABELS.get(session['state'], '状态异常')}\n"
                f"副本：《{session['instance_name']}》\n"
                f"世界：{session['world_name']}\n"
                f"剧情回合：{session['turn_no']}\n"
                f"多人轮次：第 {turn['round_no']} 轮\n"
                f"当前行动者：{current}\n"
                f"流程：{workflow}\n"
                f"控制模式：{control_text}\n"
                f"角色数：{len(roster)}\n"
                f"章节：{progress.get('chapter') or '未记录'}\n"
                f"当前目标："
                f"{progress.get('current_objective') or '未记录'}\n"
                f"里程碑：{progress_text}\n"
                f"行动格式：{config.trigger_prefix} A\n"
                f"地点：{location}"
            )

        if not session:
            return "【开团】本群尚未创建会话，请先使用 /团 开启。"
        return _COMMAND_UNHANDLED

    async def _handle_legacy_command_part_2(
        self,
        *,
        event,
        command,
        config,
        group_id,
        platform_id,
        sender_id,
        session,
        roles,
        is_admin,
        is_host,
        is_moderator,
        is_active_dm,
        control_state,
    ):
        if command.action in {"fate_preview", "fate_accept", "fate_refuse"}:
            if is_admin or is_host or is_active_dm:
                return (
                    "【命运预览操作失败】\n"
                    "操作：由角色本人查看或处理致命命运预览。\n"
                    "原因：主持人或管理员不能代替角色本人确认。\n"
                    "自动处理：系统没有修改角色命运或救援窗口。\n"
                    "下一步：请让目标角色本人使用已加入副本的账号操作。"
                )
            participant = await self.database.get_participant(
                session["id"],
                user_id=sender_id,
                include_retired=False,
            )
            if (
                str(participant.get("card_status") or "") != "approved"
                or str(participant.get("participation_status") or "")
                not in {"active", "standby", "away"}
            ):
                return (
                    "【命运预览操作失败】\n"
                    "操作：读取当前账号自己的致命命运预览。\n"
                    "原因：当前账号没有可处理的出场角色。\n"
                    "自动处理：系统没有读取他人预览，也没有修改角色状态。\n"
                    "下一步：请先完成建卡并加入当前副本，然后重试：\n\n"
                    "/团 命运预览"
                )
            previews = await self.database.list_actor_fate_previews(
                session["id"],
                str(participant["id"]),
                status="",
            )
            pending = [
                (index + 1, preview)
                for index, preview in enumerate(previews)
                if str(preview.get("status") or "") == "pending_consent"
            ]
            if command.action == "fate_preview":
                if not pending:
                    return (
                        "【命运预览】\n"
                        "当前没有等待你本人确认的致命命运。\n"
                        "系统没有修改角色状态或开启救援窗口。"
                    )
                blocks = []
                for number, preview in pending:
                    alternatives = [
                        str(item).strip()
                        for item in preview.get("alternatives") or ()
                        if str(item).strip()
                    ][:8]
                    blocks.append(
                        f"{number}. 「{str(preview.get('actor_name') or '当前角色')}」\n"
                        f"危险来源：{str(preview.get('source') or '未说明')}\n"
                        f"致命原因：{str(preview.get('reason') or '未说明')}\n"
                        f"替代方案：{'；'.join(alternatives) or '进入世界声明的救援窗口'}\n"
                        "确认后：只进入非终态救援窗口，不会被直接判定死亡\n"
                        f"有效期：{str(preview.get('expires_on') or '由世界规则决定')}"
                    )
                return (
                    "【本人命运预览】\n"
                    "致命命运尚未生效，请阅读后由你本人选择：\n\n"
                    + "\n\n".join(blocks)
                    + "\n\n确认进入救援：\n/团 命运确认 <编号>"
                    + "\n\n拒绝本次预览：\n/团 命运拒绝 <编号>"
                )
            raw_number = str(command.argument or "").strip()
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                number = 0
            if number < 1 or number > len(previews):
                return (
                    "【命运预览操作失败】\n"
                    "操作：处理本人选择的致命命运预览。\n"
                    "原因：没有找到该编号，或编号已经从当前列表移除。\n"
                    "自动处理：系统没有修改角色命运或救援窗口。\n"
                    "下一步：先刷新本人列表，再使用列表中的编号：\n\n"
                    "/团 命运预览"
                )
            selected = previews[number - 1]
            fate_context = from_astrbot_event(
                event,
                session_id=str(session["id"]),
                roles=roles,
                platform_id=platform_id,
                group_id=group_id,
                user_id=sender_id,
            )
            decision = (
                "accept" if command.action == "fate_accept" else "refuse"
            )
            result = await self.database.resolve_actor_fate_preview(
                session_id=str(session["id"]),
                preview_operation_id=str(selected.get("operation_id") or ""),
                participant_id=str(participant["id"]),
                decision=decision,
                expected_revision=int(
                    selected.get("expected_fate_revision") or 0
                ),
                actor_id=sender_id,
                idempotency_key=fate_context.idempotency_key,
            )
            if str(result.get("status") or "") == "expired":
                return (
                    "【命运预览操作失败】\n"
                    "操作：处理本人选择的致命命运预览。\n"
                    "原因：该预览已经超过世界规则声明的有效期。\n"
                    "自动处理：系统没有修改角色命运或开启救援窗口。\n"
                    "下一步：请等待规则重新生成可确认的预览。"
                )
            if decision == "refuse":
                return (
                    "【命运预览已拒绝】\n"
                    "原命运状态已保留，本次致命后果没有生效，"
                    "也没有开启救援窗口。"
                )
            return (
                "【命运预览已确认】\n"
                "角色已进入世界声明的非终态救援窗口，"
                "尚未被直接判定死亡。\n"
                "下一步：队伍可按当前角色状态提供的救援操作施救。"
            )

        if command.action == "rescue":
            rescue_context = from_astrbot_event(
                event,
                session_id=str(session["id"]),
                roles=roles,
                platform_id=platform_id,
                group_id=group_id,
                user_id=sender_id,
            )
            result = await self.database.resolve_actor_rescue(
                session_id=session["id"],
                actor_ref=str(command.argument or "").strip(),
                command="rescue",
                actor_id=sender_id,
                idempotency_key=rescue_context.idempotency_key,
            )
            state = dict(result.get("state") or {})
            character_name = str(result.get("character_name") or "").strip()
            if not character_name:
                message = (
                    "【救援结果展示失败】\n"
                    "操作：展示本次角色救援结果。\n"
                    "原因：目标角色缺少可公开显示的名称。\n"
                    "自动处理：救援结果已按规则保留；系统未显示内部标识。\n"
                    "下一步：请联系主持人修复角色资料后查看：\n\n"
                    "/团 角色"
                )
            elif result.get("outcome") == "succeeded":
                message = (
                    "【救援完成】\n"
                    f"「{character_name}」已转为"
                    f"〈{state.get('state_label') or '可行动状态'}〉。"
                )
            else:
                message = (
                    "【救援失败】\n"
                    f"「{character_name}」已转为"
                    f"〈{state.get('state_label') or '终态'}〉。"
                )
            terminal = result.get("terminal")
            if (
                isinstance(terminal, Mapping)
                and terminal.get("matched")
                and terminal.get("decision") != "manual"
            ):
                await self.engine.release_session_lock(session["id"])
                message += "\n副本已按世界终局规则完成永久归档。"
            return message

        if command.action == "join":
            sender_name = str(event.get_sender_name() or "").strip()
            if not sender_name:
                return (
                    "【加入副本失败】\n"
                    "操作：为当前账号预留玩家席位。\n"
                    "原因：平台没有提供可公开显示的玩家名称。\n"
                    "自动处理：系统没有创建席位，也没有用账号标识代替名称。\n"
                    "下一步：请先设置平台昵称，然后重试：\n\n"
                    "/团 加入"
                )
            result = await self.database.reserve_participant(
                session["id"],
                sender_id,
                sender_name,
            )
            # D1-DEL-002 §3.1：保存当前群真实目标。
            await self._sync_group_delivery_target(
                event=event,
                session_id=str(session["id"]),
            )
            if not str(result.get("private_origin") or "").strip():
                # 重加入（席位归档后再次占位）会清除私聊来源：
                # 旧已验证目标必须同步降级，避免后续误投。
                await self._revoke_private_delivery_target(
                    platform_id=self._platform_id(event),
                    user_id=sender_id,
                    reason="rejoin_unbound",
                )
            if result.get("binding_code"):
                title = (
                    "【建卡码已自动补发】"
                    if result.get("binding_code_reissued")
                    else "【席位已预留】"
                )
                outcome = await self._send_card_code_private(
                    event=event,
                    session_id=str(session["id"]),
                    participant=result,
                )
                delivery_line = (
                    "建卡入口已发送到你的私聊。"
                    if outcome.ok
                    else "建卡入口已进入待投递队列，系统会在后台继续重试。"
                )
                return (
                    f"{title}\n"
                    f"{delivery_line}\n"
                    "群聊不会显示建卡码。\n\n"
                    "如果暂未收到：先主动打开与 Bot 的私聊并发送任意消息，"
                    "再回群发送 /团 建卡 重试；"
                    "仍失败时请主持人在 WebUI 的待投递面板重试。"
                )
            return (
                "【你已加入当前副本】\n"
                f"角色卡：{result.get('card_status')}"
                f" · 状态：{result.get('participation_status')}\n"
                "如已通过审核，请发送 /团 准备。"
            )
        if command.action == "card":
            participant = next(
                (
                    item
                    for item in await self.database.list_roster(
                        session["id"]
                    )
                    if item["group_user_id"] == sender_id
                ),
                None,
            )
            if not participant:
                raise DatabaseNotFoundError("你尚未加入当前副本")
            code = str(participant.get("binding_code") or "")
            if code:
                outcome = await self._send_card_code_private(
                    event=event,
                    session_id=str(session["id"]),
                    participant=participant,
                    resend=True,
                )
                return (
                    "【建卡入口已重新投递】\n"
                    + (
                        "请查看与 Bot 的私聊。"
                        if outcome.ok
                        else "消息已进入待投递队列；请先主动私聊 Bot，再由主持人在 WebUI 重试。"
                    )
                )
            if participant["card_status"] == "approved":
                return "【角色卡】已经审核通过，请发送 /团 准备。"
            return (
                "【角色卡】建卡流程已经绑定或等待审核；"
                "请回到与 Bot 的私聊继续。"
            )
        if command.action == "character":
            participant = await self.database.get_participant(
                session["id"],
                user_id=sender_id,
            )
            character_name = str(
                participant.get("character_name") or ""
            ).strip()
            if not character_name:
                return (
                    "【角色资料读取失败】\n"
                    "操作：查看当前角色资料。\n"
                    "原因：当前角色缺少可公开显示的名称。\n"
                    "自动处理：系统已中止展示，未输出内部标识或不完整资料。\n"
                    "下一步：请回到与 Bot 的私聊继续修正角色卡：\n\n"
                    "/团 当前"
                )
            return (
                "【我的角色】\n"
                f"角色：「{character_name}」\n"
                f"代号：{participant.get('character_code') or '尚未设置'}\n"
                f"角色卡：{participant.get('card_status')}\n"
                f"准备：{'是' if participant.get('ready') else '否'}\n"
                f"入场：{participant.get('participation_status')}"
            )
        if command.action == "supplement":
            viewer_role = "admin" if is_admin else "player"
            participant_id = ""
            if not is_admin:
                control = await self.database.get_control_state(
                    session["id"]
                )
                if (
                    str(control.get("mode") or "") == "dm"
                    and str(
                        control.get("active_dm_user_id") or ""
                    )
                    == sender_id
                ):
                    viewer_role = "dm"
                else:
                    participant = await self.database.get_participant(
                        session["id"],
                        user_id=sender_id,
                    )
                    participant_id = str(
                        participant.get("id") or ""
                    )
            offers = await self.database.list_supplement_offers(
                session["id"],
                participant_id=participant_id,
                viewer_role=viewer_role,
            )
            if not offers:
                return (
                    "【角色补充】当前没有待确认项目。\n"
                    "系统没有修改任何角色卡。\n"
                    "下一步：继续故事；新项目出现时会主动私聊对应玩家。"
                )
            lines = ["【角色补充状态】"]
            for index, offer in enumerate(offers, start=1):
                owner = (
                    f"「{offer.get('character_name') or '角色'}」 · "
                    if viewer_role in {"dm", "admin"}
                    else ""
                )
                lines.append(
                    f"{index}. {owner}"
                    + supplement_list_line(
                        stage=str(offer.get("stage") or ""),
                        field_label=str(
                            offer.get("field_label") or "角色资料"
                        ),
                        state=str(
                            offer.get("state") or "offered"
                        ),
                        expired=bool(offer.get("expired")),
                    ).removeprefix("· ")
                )
            lines.append(
                "\n玩家请在与 Bot 的私聊中发送 /团 当前，"
                "再按当前页序号确认。"
            )
            return "\n".join(lines)
        if command.action == "ready":
            participant = await self.database.set_participant_ready(
                session["id"],
                sender_id,
                True,
            )
            preflight = await self.database.opening_preflight(
                session["id"]
            )
            waiting = int(
                preflight.get("blocker_count")
                or len(preflight.get("blockers") or [])
            )
            suffix = (
                (
                    "\n【全员准备完成】主持人现在可以发送 /团 继续"
                    if preflight.get("resume_mode")
                    else "\n【全员准备完成】主持人现在可以发送 /团 开演"
                )
                if preflight["ok"]
                else f"\n当前仍有 {waiting} 项准备阻塞。"
            )
            return (
                f"【{participant.get('character_name') or participant.get('display_name')} 已准备】"
                + suffix
            )
        if command.action == "force_ready":
            if command.argument not in {"确认", "confirm", "CONFIRM"}:
                return (
                    "【确认强制全员准备】只会处理角色卡已审核通过、"
                    "且当前出场的玩家；不会绕过建卡或审核。"
                    "\n请发送：/团 强制全员准备 确认"
                )
            result = await self.database.force_all_ready(
                session["id"],
                sender_id,
            )
            lines = [
                "【强制准备完成】",
                f"已准备：{result['ready_count']} 人",
            ]
            if result["skipped"]:
                lines.append(
                    "未处理："
                    + "；".join(
                        (
                            f"{item['name']}（{item['card_status']}/"
                            f"{item['participation_status']}）"
                        )
                        for item in result["skipped"]
                    )
                )
            preflight = await self.database.opening_preflight(
                session["id"]
            )
            lines.append(
                "主持人现在可以发送 /团 继续。"
                if int(session.get("turn_no") or 0) > 0
                else "主持人现在可以发送 /团 开演。"
            )
            if not preflight["ok"]:
                lines.append(
                    "仍有阻塞："
                    + "；".join(
                        preflight.get("blocker_messages")
                        or ["准备尚未完成"]
                    )
                )
            return "\n".join(lines)
        if command.action == "roster":
            return format_roster(
                await self.database.list_roster(session["id"])
            )
        if command.action == "review":
            roster = await self.database.list_roster(session["id"])
            pending = _pending_review_cards(roster)
            argument = str(command.argument or "").strip()
            list_page = parse_instance_list_page(argument)
            if not argument:
                return format_pending_reviews_compact(pending)
            if list_page is not None:
                return format_pending_reviews(
                    pending,
                    page=list_page,
                )

            parts = argument.split(maxsplit=2)
            if parts[0] in {"查看", "详情", "view"}:
                if len(parts) < 2:
                    return (
                        "【开团】格式：/团 审核 查看 "
                        "<序号或审核号>"
                    )
                target = _resolve_pending_review(pending, parts[1])
                instance = await self.database.get_instance_config(
                    session["id"]
                )
                return format_review_card(
                    target,
                    instance["character_card_template"],
                    instance["world_snapshot"],
                )

            target = _resolve_pending_review(pending, parts[0])
            if len(parts) == 1 or parts[1] in {
                "查看",
                "详情",
                "view",
            }:
                instance = await self.database.get_instance_config(
                    session["id"]
                )
                return format_review_card(
                    target,
                    instance["character_card_template"],
                    instance["world_snapshot"],
                )

            decisions = {
                "通过": True,
                "approve": True,
                "驳回": False,
                "拒绝": False,
                "reject": False,
            }
            if parts[1] not in decisions:
                return (
                    "【开团】格式：\n"
                    "/团 审核\n"
                    "/团 审核 <序号或审核号>\n"
                    "/团 审核 <序号或审核号> "
                    "<通过|驳回> [备注]"
                )
            approved = decisions[parts[1]]
            note = parts[2] if len(parts) > 2 else ""
            expected_version = int(target.get("card_version_no") or 0)
            review_key = operation_key(
                str(session["id"]),
                "card.review",
                turn_no=int(session.get("turn_no") or 0),
                actor_id=sender_id,
                source_id=transport_event_id(event),
                payload={
                    "participant": str(target["id"]),
                    "version": expected_version,
                    "action": "approve" if approved else "reject",
                    "note": note,
                },
            )
            participant = await self.database.review_character_card(
                session["id"],
                str(target["id"]),
                approved,
                sender_id,
                note,
                expected_version,
                review_key,
            )
            remaining = len(pending) - 1
            return (
                f"【角色卡审核】{participant.get('character_name')}"
                f" · {'已通过' if approved else '已驳回'}"
                + (f"\n备注：{note}" if note else "")
                + f"\n剩余待审核：{max(0, remaining)} 人"
                + (
                    "\n发送 /团 审核，查看剩余名单。"
                    if remaining > 0
                    else ""
                )
            )
        if command.action == "perform":
            instance_config = await self.database.get_instance_config(
                session["id"]
            )
            self.engine.validate_world_runtime(
                instance_config["world_snapshot"]
            )
            result = await self.database.activate_story(
                session["id"],
                sender_id,
                resume=False,
            )
            if not result["started"]:
                return (
                    "【暂时无法开演】\n· "
                    + "\n· ".join(
                        result.get("blocker_messages")
                        or ["准备尚未完成"]
                    )
                )
            current = result["current_participant"]
            return (
                f"【故事正式开演】{session['instance_name']}\n"
                f"出场角色："
                + "、".join(
                    item.get("character_name") or item.get("display_name")
                    for item in result["participants"]
                )
                + f"\n当前行动者："
                f"{current.get('character_name') or current.get('display_name')}"
                + "\n\n"
                + format_gameplay_brief(
                    instance_config["world_snapshot"]
                )
                + (
                    f"\n\n{result['opening']}"
                    if result.get("opening")
                    else ""
                )
                + "\n\n"
                + format_choices(
                    current.get("character_name")
                    or current.get("display_name"),
                    result["choice_set"]["choices"],
                    trigger_prefix=config.trigger_prefix,
                )
            )
        return _COMMAND_UNHANDLED
