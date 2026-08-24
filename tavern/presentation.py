from __future__ import annotations

import asyncio
import html
import re
import time
from collections.abc import Sequence
from typing import Any, Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .config import TavernConfig
from .card_wizard import paged_options
from .constants import (
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
from .database import (
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from .engine import (
    TavernBusyError,
    TavernEngine,
    TavernEngineError,
    TavernPlayerDisabledError,
    TavernTurnOrderError,
)
from .events import EventBroker
from .help_topics import contextual_help
from .recaps import build_recap
from .lifecycle import (
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
from .stat_generation import (
    calculate_preset_stack_stats,
    format_preset_stack_result,
    stat_generation_config,
    uses_preset_stack_stats,
)
from .security import (
    ParsedCommand,
    parse_story_trigger,
    parse_tavern_command,
    validate_platform_id,
)
from .web_console import TavernWebConsole


INSTANCE_LIST_PAGE_SIZE = 5
INSTANCE_INTRO_MAX_CHARS = 220
REVIEW_LIST_PAGE_SIZE = 5
# 计时轮询与通知频控
TIMER_POLL_INTERVAL_SECONDS = 15
# 同一个计时器在该窗口内只允许推送一次，防止重复行造成刷屏。
TIMER_NOTICE_DEDUP_SECONDS = 25
# 相邻两条主动通知之间的最小间隔，规避 QQ 官方主动消息频控。
TIMER_NOTICE_MIN_GAP_SECONDS = 2.0
PRIVATE_CARD_ACTIONS = frozenset(
    {
        "card",
        "card_fill",
        "card_stats_reset",
        "card_timer_notice",
        "card_preview",
        "card_confirm",
        "card_cancel",
    }
)
PRIVATE_ONLY_CARD_ACTIONS = PRIVATE_CARD_ACTIONS - {"card"}
_INSTANCE_PAGE_PATTERNS = (
    re.compile(r"^第\s*(\d{1,6})\s*页$"),
    re.compile(r"^页\s*(\d{1,6})$"),
    re.compile(r"^列表\s*(\d{1,6})$"),
)


HELP_TEXT = f"""\
【AI 酒馆 v{PLUGIN_VERSION}｜多人叙事、真人 DM 与世界协议 v5】
主持：/酒馆 开启 → /酒馆 开启新副本 <序号或世界标识>｜/酒馆 开启旧副本 <序号或副本标识> → /酒馆 开演
恢复：/酒馆 暂停 → /酒馆 恢复 → 全员准备 → /酒馆 继续
玩家：/酒馆 加入｜角色｜准备｜阵容｜暂离｜返回队列｜退出
建卡：私聊 /酒馆 建卡 <验证码>｜当前步骤｜上一步｜修改 <字段>｜重填数值
回合：jg A｜/酒馆 选择 A｜/酒馆 重整选项
裁定：/酒馆 灵感｜/酒馆 灵感 A 优势｜/酒馆 灵感重投 A
集体：/酒馆 投票 A（不消耗个人行动）
记录：/酒馆 回顾｜存档列表｜存档 <名称>｜删档 <名称>｜读档｜回滚
管理：审核｜强制全员准备｜强制下一位｜倒计时｜用量｜限额｜移至｜指定
主持：/酒馆 主持 开启｜指引｜推进｜直述｜交棒｜自动｜状态｜接管
帮助：/酒馆 帮助 建卡｜回合｜投票｜回顾｜管理
安全：任一出场玩家可发送 /酒馆 安全暂停
结束：/酒馆 关闭｜/酒馆 完结 确认｜/酒馆 强制终止 确认 <原因>

普通群聊完全旁路；剧情只接受 A/B/C/D，不接受自由改写世界。"""


def parse_instance_list_page(
    argument: str,
    *,
    allow_bare_number: bool = False,
) -> int | None:
    """Parse an explicit list page without confusing it with an instance ref."""

    text = str(argument or "").strip()
    if not text:
        return 1
    if allow_bare_number and re.fullmatch(r"\d{1,6}", text):
        return max(1, int(text))
    for pattern in _INSTANCE_PAGE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return max(1, int(match.group(1)))
    return None


def _compact_instance_intro(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "暂无简介"
    if len(text) <= INSTANCE_INTRO_MAX_CHARS:
        return text
    return text[: INSTANCE_INTRO_MAX_CHARS - 1].rstrip() + "…"


def format_turn_status(turn: Mapping[str, Any]) -> str:
    order = turn.get("order")
    if not isinstance(order, list) or not order:
        return "【回合顺序】尚无玩家，请先发送 /酒馆 加入。"
    current_id = str(turn.get("current_user_id") or "")
    lines = []
    for item in order:
        marker = "▶" if str(item.get("user_id") or "") == current_id else "·"
        lines.append(
            f"{marker} {item.get('position', '?')}. "
            f"{item.get('name') or item.get('user_id')}"
        )
    return (
        f"【回合顺序】第 {turn.get('round_no', 1)} 轮\n"
        + "\n".join(lines)
    )


def _paged_items(
    source: list[Mapping[str, Any]],
    page: int,
) -> tuple[int, int, int, list[Mapping[str, Any]]]:
    total = len(source)
    pages = max(1, (total + INSTANCE_LIST_PAGE_SIZE - 1) // INSTANCE_LIST_PAGE_SIZE)
    effective_page = min(max(1, int(page or 1)), pages)
    start = (effective_page - 1) * INSTANCE_LIST_PAGE_SIZE
    page_items = source[start : start + INSTANCE_LIST_PAGE_SIZE]
    return total, pages, start, page_items


def format_existing_instance_list(
    instances: list[Mapping[str, Any]],
    *,
    page: int = 1,
) -> str:
    total, pages, start, page_items = _paged_items(instances, page)
    effective_page = min(max(1, int(page or 1)), pages)
    lines = [f"【已有副本｜第 {effective_page}/{pages} 页｜共 {total} 个】"]
    if not page_items:
        lines.extend(("本群还没有酒馆副本。", "请先发送 /酒馆 开启新副本。"))
        return "\n".join(lines)
    state_labels = {
        SESSION_CLOSED: "已关闭",
        SESSION_PREPARING: "准备中",
        SESSION_RUNNING: "运行中",
        SESSION_PAUSED: "已暂停",
        SESSION_FINISHED: "已完结",
        SESSION_MAINTENANCE: "维护中",
    }
    for index, item in enumerate(page_items, start=start + 1):
        marker = "▶" if item.get("selected") else "·"
        lines.append(
            f"{marker} {index}. {item.get('instance_name')} "
            f"（{item.get('instance_slug')}）"
            f" · {item.get('world_name')}"
            f" · {state_labels.get(item.get('state'), item.get('state'))}"
            f" · 第 {item.get('turn_no', 0)} 回合"
        )
        lines.append(
            "   简介："
            + _compact_instance_intro(
                item.get("world_description") or item.get("description")
            )
        )
    lines.extend(
        _instance_list_footer(
            effective_page,
            pages,
            selection_label=(
                "选择已有副本：/酒馆 开启旧副本 <数字序号或副本标识>"
                f"（例如：/酒馆 开启旧副本 {start + 1}）"
            ),
            page_command="/酒馆 开启旧副本",
        )
    )
    return "\n".join(lines)


def format_world_list(
    worlds: list[Mapping[str, Any]],
    *,
    page: int = 1,
    has_existing_instances: bool = False,
) -> str:
    total, pages, start, page_items = _paged_items(worlds, page)
    effective_page = min(max(1, int(page or 1)), pages)
    if has_existing_instances:
        title = f"【可用世界｜第 {effective_page}/{pages} 页｜共 {total} 个】"
    else:
        title = (
            f"【本群还没有酒馆副本｜可用世界第 {effective_page}/{pages} 页"
            f"｜共 {total} 个】"
        )
    lines = [title]
    for index, item in enumerate(page_items, start=start + 1):
        lines.append(f"· {index}. {item.get('name')}（{item.get('slug')}）")
        lines.append("   简介：" + _compact_instance_intro(item.get("description")))
    if not page_items:
        lines.append("当前没有可用世界包")
        return "\n".join(lines)
    lines.extend(
        _instance_list_footer(
            effective_page,
            pages,
            selection_label=(
                "选择世界建立新副本：/酒馆 开启新副本 <数字序号或世界标识>"
                f"（例如：/酒馆 开启新副本 {start + 1}）"
            ),
            page_command="/酒馆 开启新副本",
        )
    )
    return "\n".join(lines)


def format_opening_menu(
    instances: list[Mapping[str, Any]],
    worlds: list[Mapping[str, Any]],
) -> str:
    if not instances:
        return format_world_list(worlds, page=1)
    return "\n\n".join(
        (
            format_existing_instance_list(instances, page=1),
            format_world_list(worlds, page=1, has_existing_instances=True),
        )
    )


def format_instance_list(
    instances: list[Mapping[str, Any]],
    worlds: list[Mapping[str, Any]] | None = None,
    *,
    page: int = 1,
) -> str:
    if instances:
        return format_existing_instance_list(instances, page=page)
    return format_world_list(list(worlds or []), page=page)


def _instance_list_footer(
    page: int,
    pages: int,
    *,
    selection_label: str,
    page_command: str,
) -> list[str]:
    lines = ["", selection_label]
    navigation = []
    if page > 1:
        navigation.append(f"上一页：{page_command} 第{page - 1}页")
    if page < pages:
        navigation.append(f"下一页：{page_command} 第{page + 1}页")
    if navigation:
        lines.append("｜".join(navigation))
    return lines


def format_roster(roster: list[Mapping[str, Any]]) -> str:
    if not roster:
        return "【当前阵容】尚无玩家加入。"
    card_labels = {
        "uncreated": "未建卡",
        "draft": "建卡中",
        "pending_review": "待审核",
        "approved": "已通过",
        "rejected": "未通过",
    }
    participation_labels = {
        "reserved": "占位",
        "active": "出场",
        "standby": "候补",
        "away": "暂离",
        "retired": "已退场",
        "archived": "已归档",
    }
    lines = ["【当前阵容】"]
    for item in roster:
        name = (
            item.get("character_name")
            or item.get("display_name")
            or item.get("group_user_id")
        )
        ready = "已准备" if item.get("ready") else "未准备"
        lines.append(
            f"· {name}"
            f"（{item.get('character_code') or '无代号'}）"
            f" · {card_labels.get(item.get('card_status'), item.get('card_status'))}"
            f" · {ready}"
            f" · {participation_labels.get(item.get('participation_status'), item.get('participation_status'))}"
        )
    return "\n".join(lines)


def format_vote(vote: Mapping[str, Any]) -> str:
    lines = [
        f"【集体决策 · 第 {vote.get('stage', 1)} 轮】",
        str(vote.get("question") or ""),
    ]
    lines.extend(
        f"{item.get('key')}. {item.get('text')}"
        for item in vote.get("options", [])
    )
    lines.extend(
        [
            "",
            f"有效成员：{len(vote.get('eligible_user_ids', []))} 人",
            f"截止：{vote.get('deadline_at') or '不限时'}",
            "发送：/酒馆 投票 A",
        ]
    )
    return "\n".join(lines)


def format_recovered_timer(
    timers: Sequence[Mapping[str, Any]],
    *,
    vote_active: bool,
) -> str:
    timer_type = "vote" if vote_active else "turn"
    timer = next(
        (
            item
            for item in timers
            if item.get("timer_type") == timer_type
            and item.get("status") == "active"
        ),
        None,
    )
    if not timer:
        return "⏳ 【恢复计时】当前流程不限时或倒计时已关闭"
    try:
        remaining = max(0, int(timer.get("remaining_seconds") or 0))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    minutes, seconds = divmod(remaining, 60)
    label = "投票" if vote_active else "行动回合"
    if minutes:
        text = f"{minutes} 分 {seconds} 秒"
    else:
        text = f"{seconds} 秒"
    return f"⏳ 【恢复计时】{label}剩余 {text}"


def world_preset_brief(world: Mapping[str, Any], focus: str = "") -> str:
    """Build a compact summary of the world's preset content (professions,
    factions, regions) to show when a player starts creating a character."""
    if not isinstance(world, Mapping):
        return ""
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return ""
    professions = rules.get("professions")
    professions = professions if isinstance(professions, list) else []
    modules = rules.get("setting_modules")
    modules = modules if isinstance(modules, Mapping) else {}
    stat_rules = rules.get("character_card") or {}
    stat_defs = (stat_rules.get("stats") or {}).get("attributes") or []
    stat_labels: dict[str, str] = {}
    for _attr in stat_defs:
        if isinstance(_attr, Mapping) and _attr.get("key"):
            stat_labels[str(_attr["key"])] = str(
                _attr.get("label") or _attr["key"]
            )
    if not professions:
        _pp = stat_rules.get("profession_presets")
        if isinstance(_pp, list):
            professions = _pp
    _stats_raw = stat_rules.get("stats") or {}
    _profession_mode = bool(
        isinstance(_stats_raw, Mapping)
        and (
            _stats_raw.get("mode") == "preset"
            or _stats_raw.get("input_mode")
            == "automatic_profession_base_plus_two_fixed_bonus_choices"
            or _stats_raw.get("allocation_mode")
            == "profession_base_plus_primary7_secondary3"
        )
    )
    _bonus_note = (
        f"（基础属性合计 {_stats_raw.get('base_budget', 50)} 点已锁定，随后选主属性 +{_stats_raw.get('primary_bonus', 7)}、副属性 +{_stats_raw.get('secondary_bonus', 3)}，最终固定 {_stats_raw.get('budget', 60)} 点）"
        if _profession_mode
        else ""
    )
    lines: list[str] = []
    if professions:
        if focus == "profession":
            lines.append(
                "【可选预设职业】填写以下任一名称即可自动套用其基础数值："
                + _bonus_note
            )
        else:
            lines.append(
                "【本世界预设职业】（建卡时在「预设职业」一栏填写其一，"
                "将自动套用基础数值）"
                + _bonus_note
            )
        for item in professions:
            if not isinstance(item, Mapping):
                continue
            disp = item.get("label") or item.get("name") or item.get("key") or "?"
            base = item.get("base_stats")
            if not isinstance(base, Mapping):
                base = item.get("attributes")
            if not isinstance(base, Mapping):
                base = item.get("base_attributes")
            base = base if isinstance(base, Mapping) else {}
            if base:
                bs_text = "、".join(
                    f"{stat_labels.get(str(k), k)}{v}"
                    for k, v in base.items()
                )
            else:
                bs_text = "数值自定"
            free = item.get("free_points")
            free_text = (
                f" · 可分配 {free} 点" if isinstance(free, int) else ""
            )
            desc = item.get("description")
            desc_text = f" — {desc}" if desc else ""
            lines.append(f"· {disp}：{bs_text}{free_text}{desc_text}")
    if focus != "profession":
        factions = modules.get("factions")
        factions = factions if isinstance(factions, list) else []
        if factions:
            names = [
                f.get("name", "")
                for f in factions[:5]
                if isinstance(f, Mapping)
            ]
            if names:
                lines.append("【主要势力】" + "、".join(names))
        regions = modules.get("regions")
        regions = regions if isinstance(regions, list) else []
        if regions:
            names = [
                r.get("name", "")
                for r in regions[:5]
                if isinstance(r, Mapping)
            ]
            if names:
                lines.append("【主要地点】" + "、".join(names))
    return "\n".join(lines)


def _profession_preset_line(
    preset: Mapping[str, Any],
    key_to_label: Mapping[str, str],
) -> str:
    """Render one profession preset as a single readable line."""
    display = str(preset.get("display_text") or "").strip()
    if display:
        return display
    name = str(preset.get("name") or "").strip() or "?"
    base = preset.get("base_attributes")
    if not isinstance(base, Mapping):
        base = preset.get("attributes")
    base = base if isinstance(base, Mapping) else {}
    numbers = "｜".join(str(int(value)) for value in base.values())
    total = sum(int(value) for value in base.values()) if base else 0
    role = str(preset.get("role") or "").strip()
    text = f"{name}：{numbers}"
    if base:
        text += f"（合计{total}）"
    if role:
        text += f" — {role}"
    return text


def _format_profession_step_prompt(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    field: Mapping[str, Any],
    step: int,
    total_fields: int,
) -> str:
    """Prompts for the profession-preset stat mode (fixed 50 base +7/+3)."""
    field_key = str(field.get("key") or "")
    if field_key not in {
        "profession",
        "primary_attribute",
        "secondary_attribute",
    }:
        return ""
    _label_to_key, key_to_label = attribute_maps(template)
    attribute_options = "、".join(key_to_label.values())
    if field_key == "profession":
        lines = [
            f"【角色卡 {step + 1}/{total_fields}】选择职业",
            "选择职业后会自动载入固定 50 点基础属性。",
            "属性顺序：" + "｜".join(key_to_label.values()),
            "",
        ]
        for preset in template.get("profession_presets") or []:
            if not isinstance(preset, Mapping):
                continue
            line = _profession_preset_line(preset, key_to_label)
            if line:
                lines.append(f"· {line}")
        first_name = ""
        for preset in template.get("profession_presets") or []:
            if isinstance(preset, Mapping) and preset.get("name"):
                first_name = str(preset["name"])
                break
        example = first_name or "骑士"
        lines.extend(
            [
                "",
                f"直接回复职业名称，例如：{example}",
                f"或发送：/酒馆 填写 {example}",
            ]
        )
        return "\n".join(lines)
    try:
        resolved = resolve_profession_stats(
            template,
            values,
            require_complete=False,
        )
    except ValueError as exc:
        return f"【无法继续】{exc}\n请先重新选择职业：/酒馆 填写 <职业名称>"
    if field_key == "primary_attribute":
        lines = [
            "【选择主属性｜固定+7】",
            f"当前职业：{resolved['profession']}",
            "职业基础属性：",
        ]
        for key, value in resolved["base"].items():
            lines.append(f"· {resolved['labels'][key]}：{value}")
        lines.extend(
            [
                "",
                f"可选：{attribute_options}",
                "直接回复属性名称，例如："
                + next(iter(key_to_label.values()), "力量"),
            ]
        )
        return "\n".join(lines)
    return (
        "【选择副属性｜固定+3】\n"
        f"职业：{resolved['profession']}\n"
        f"已选主属性：{values.get('primary_attribute') or '（未选）'}（+7）\n"
        "副属性不能与主属性相同。\n"
        f"可选：{attribute_options}\n"
        "直接回复属性名称"
    )


def _format_preset_step_prompt(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    field: Mapping[str, Any],
    step: int,
    total_fields: int,
) -> str:
    page = paged_options(template, field, values)
    if not page["options"]:
        return ""
    label = str(field.get("label") or field.get("key") or "预设")
    lines = [
        f"【角色卡 {step + 1}/{total_fields}｜{label}】",
        f"预设选项 · 第 {page['page_number']}/{page['total_pages']} 页",
        "",
    ]
    _, key_to_label = attribute_maps(template)
    for index, option in enumerate(page["items"], start=1):
        source = option.get("source")
        source = source if isinstance(source, Mapping) else {}
        lines.append(f"{index}. {option['label']}")
        description = str(option.get("description") or "").strip()
        if description:
            lines.append(f"   {description}")
        base = source.get("base_attributes") or source.get("attributes")
        if isinstance(base, Mapping) and base:
            stat_text = "｜".join(
                f"{key_to_label.get(str(key), key)}{value}"
                for key, value in base.items()
            )
            lines.append(f"   基础属性：{stat_text}")
        limitation = str(source.get("limitations") or "").strip()
        if limitation:
            lines.append(f"   限制：{limitation}")
    lines.extend(
        [
            "",
            "回复本页序号、名称或稳定 ID 进行选择。",
        ]
    )
    if page["total_pages"] > 1:
        lines.append("发送“下一页”或“上一页”翻页，不会推进建卡步骤。")
    return "\n".join(lines)


def format_card_prompt(draft: Mapping[str, Any]) -> str:
    generated_notice = draft.get("stat_generation_result")
    if isinstance(generated_notice, Mapping):
        without_notice = dict(draft)
        without_notice.pop("stat_generation_result", None)
        return (
            format_preset_stack_result(generated_notice)
            + "\n\n"
            + format_card_prompt(without_notice)
        )
    template = draft.get("template") or {}
    fields = template.get("fields") or []
    values = draft.get("fields")
    values = values if isinstance(values, Mapping) else {}
    step = int(
        draft.get("current_step", draft.get("draft_step", 0)) or 0
    )
    world = draft.get("world") or {}
    preset_mode = uses_profession_preset_stats(template)
    preset_stack_mode = uses_preset_stack_stats(template)
    if step >= len(fields) and preset_stack_mode:
        try:
            resolved = calculate_preset_stack_stats(
                template,
                values,
                require_complete=True,
            )
        except ValueError as exc:
            return f"【角色卡数值尚未完成】{exc}"
        assert resolved is not None
        sources = "、".join(
            str(item) for item in stat_generation_config(template).get(
                "bonus_sources", []
            )
        )
        return "\n".join(
            [
                format_preset_stack_result(resolved),
                "",
                f"属性来源已锁定；如需调整请使用 /酒馆 修改 <字段>（{sources}）。",
                "发送 /酒馆 预览 检查内容，确认无误后发送 /酒馆 确认建卡。",
            ]
        )
    if step >= len(fields) and preset_mode:
        try:
            resolved = resolve_profession_stats(
                template,
                values,
                require_complete=True,
            )
        except ValueError as exc:
            return f"【角色卡数值尚未完成】{exc}"
        lines = [
            "【角色卡字段已填写完成】",
            f"职业：{resolved['profession']}",
            (
                f"主属性：{resolved['primary']['label']}"
                f" +{resolved['primary']['bonus']}"
            ),
            (
                f"副属性：{resolved['secondary']['label']}"
                f" +{resolved['secondary']['bonus']}"
            ),
            "最终属性：",
        ]
        for key, value in resolved["raw"].items():
            lines.append(f"· {resolved['labels'][key]}：{value}")
        lines.extend(
            [
                f"基础总和：{resolved['base_total']}",
                f"专精加成：{resolved['bonus_total']}",
                f"最终总和：{resolved['effective_total']}",
                "",
                "重新选择主副属性：/酒馆 重填数值",
                "发送 /酒馆 预览 检查内容，"
                "确认无误后发送 /酒馆 确认建卡。",
            ]
        )
        return "\n".join(lines)
    if step >= len(fields):
        allocation = card_stat_allocation(template, values)
        lines = ["【角色卡字段已填写完成】"]
        if allocation["stat_fields"]:
            lines.append(
                f"角色数值：已使用 {allocation['used']}"
                f"/{allocation['budget']} 点"
                f" · 剩余 {allocation['remaining']} 点"
            )
            lines.append(
                "只重新分配数值：/酒馆 重填数值"
            )
        if values.get("profession"):
            lines.append(
                "若已选预设职业，其基础数值已自动套用；"
                "剩余点数可经 /酒馆 重填数值 自由分配。"
            )
        lines.append(
            "发送 /酒馆 预览 检查内容，"
            "确认无误后发送 /酒馆 确认建卡。"
        )
        return "\n".join(lines)
    field = fields[step]
    if preset_mode and str(field.get("key") or "") in {
        "primary_attribute",
        "secondary_attribute",
    }:
        preset_prompt = _format_profession_step_prompt(
            template,
            values,
            field,
            step,
            len(fields),
        )
        if preset_prompt:
            return preset_prompt
    if (
        str(field.get("type") or "") in {"select", "preset_select"}
        or field.get("options")
        or field.get("options_source")
        or field.get("preset_source")
    ):
        preset_prompt = _format_preset_step_prompt(
            template,
            values,
            field,
            step,
            len(fields),
        )
        if preset_prompt:
            return preset_prompt
    allocation = card_stat_allocation(template, values, step)
    current_stat = allocation.get("current")
    if isinstance(current_stat, Mapping):
        lines = []
        if (
            int(current_stat["position"]) == 1
            and not allocation["values"]
        ):
            lines.extend(
                [
                    "【接下来开始填写角色数值】",
                    "前面的角色资料已经保存；"
                    "数值会按世界模板的总预算依次分配。",
                ]
            )
        lines.extend(
            [
                (
                    f"【角色数值 {current_stat['position']}"
                    f"/{current_stat['total']}】"
                    f"{current_stat['label']}"
                ),
                (
                    f"当前可填：{current_stat['minimum']}"
                    f"—{current_stat['effective_maximum']}"
                ),
                (
                    f"总预算：{allocation['budget']} 点"
                    f" · 已使用：{current_stat['used_before']} 点"
                    f" · 当前剩余："
                    f"{current_stat['remaining_before']} 点"
                ),
            ]
        )
        if int(current_stat["reserved_minimum"]) > 0:
            lines.append(
                f"已为后续属性预留最低 "
                f"{current_stat['reserved_minimum']} 点。"
            )
        lines.extend(
            [
                "后续属性的可填上限会随剩余预算自动递减。",
                "直接回复整数，或发送：/酒馆 填写 <整数>",
                "重新分配全部数值：/酒馆 重填数值",
            ]
        )
        return "\n".join(lines)
    required = "必填" if field.get("required") else "选填"
    return (
        f"【角色卡 {step + 1}/{len(fields)}】"
        f"{field.get('label')}（{required}，最多 {field.get('max_chars')} 字）\n"
        "字段内容不得包含空格、全角空格、换行或制表符。\n"
        "直接回复内容，或发送：/酒馆 填写 <内容>"
    )


def format_card_preview(draft: Mapping[str, Any]) -> str:
    template = draft.get("template") or {}
    fields = draft.get("fields") or {}
    lines = ["【角色卡预览】"]
    for definition in template.get("fields") or []:
        if definition.get("stat_key"):
            continue
        raw_value = fields.get(definition.get("key"), "")
        value = (
            "、".join(str(item) for item in raw_value)
            if isinstance(raw_value, list)
            else str(raw_value or "")
        )
        if definition.get("private"):
            value = "（私密字段已保存）" if value else "（未填写）"
        lines.append(f"· {definition.get('label')}：{value or '（未填写）'}")
    resolved_boundaries = fields.get("_resolved_boundaries")
    if isinstance(resolved_boundaries, Mapping):
        knowledge = resolved_boundaries.get("knowledge") or {}
        content = resolved_boundaries.get("content") or {}
        lines.extend(["", "【知识边界｜由世界包与预设生成】"])
        domains = knowledge.get("domains") or {}
        if domains:
            lines.append(
                "· 领域：" + "；".join(
                    f"{key}={value}" for key, value in domains.items()
                )
            )
        lines.append(
            "· 禁止范围："
            + "、".join(knowledge.get("forbidden_domains") or [])
            if knowledge.get("forbidden_domains")
            else "· 禁止范围：按世界通用规则"
        )
        lines.extend(["", "【内容边界｜世界包权威】"])
        lines.append(f"· 分级：{content.get('rating') or 'general'}")
        if content.get("hard_denials"):
            lines.append("· 硬边界：" + "、".join(content["hard_denials"]))
    if uses_preset_stack_stats(template):
        lines.append("")
        try:
            resolved = calculate_preset_stack_stats(
                template,
                fields,
                require_complete=True,
            )
        except ValueError as exc:
            lines.append(f"【角色数值｜多预设自动结算】尚未生成：{exc}")
        else:
            assert resolved is not None
            lines.append(format_preset_stack_result(resolved))
        lines.extend(
            [
                "",
                "确认：/酒馆 确认建卡",
                "放弃并释放席位：/酒馆 取消建卡",
            ]
        )
        return "\n".join(lines)
    if uses_profession_preset_stats(template):
        lines.append("")
        try:
            resolved = resolve_profession_stats(
                template,
                fields,
                require_complete=False,
            )
        except ValueError as exc:
            lines.append(f"【角色数值】尚未生成：{exc}")
        else:
            lines.append("【角色数值｜职业预设】")
            lines.append(f"· 职业：{resolved['profession']}")
            lines.append(
                f"· 主属性：{resolved['primary']['label'] or '（未选）'}"
                f" +{resolved['primary']['bonus']}"
            )
            lines.append(
                f"· 副属性：{resolved['secondary']['label'] or '（未选）'}"
                f" +{resolved['secondary']['bonus']}"
            )
            for key, value in resolved["raw"].items():
                base_value = resolved["base"][key]
                delta = value - base_value
                delta_text = f"（基础{base_value}{delta:+d}）" if delta else ""
                lines.append(
                    f"· {resolved['labels'][key]}：{value}{delta_text}"
                    f"（检定修正 {resolved['modifiers'][key]:+d}）"
                )
            lines.append(
                f"· 总和：基础 {resolved['base_total']}"
                f" + 加成 {resolved['bonus_total']}"
                f" = {resolved['effective_total']}"
            )
        lines.extend(
            [
                "",
                "重新选择主副属性：/酒馆 重填数值",
                "确认：/酒馆 确认建卡",
                "放弃并释放席位：/酒馆 取消建卡",
            ]
        )
        return "\n".join(lines)
    allocation = card_stat_allocation(template, fields)
    if allocation["stat_fields"]:
        modifier_table = (template.get("stats") or {}).get(
            "modifier_table"
        ) or {}
        lines.append("")
        lines.append("【角色数值】")
        for item in allocation["stat_fields"]:
            value = allocation["values"].get(item["field_key"])
            if value is None:
                lines.append(f"· {item['label']}：（未填写）")
                continue
            modifier = int(modifier_table.get(str(value), 0))
            lines.append(
                f"· {item['label']}：{value}"
                f"（检定修正 {modifier:+d}）"
            )
        lines.append(
            f"· 预算：已使用 {allocation['used']}"
            f"/{allocation['budget']} 点"
            f" · 剩余 {allocation['remaining']} 点"
        )
    lines.extend(
        [
            "",
            "只重新分配数值：/酒馆 重填数值",
            "确认：/酒馆 确认建卡",
            "放弃并释放席位：/酒馆 取消建卡",
        ]
    )
    return "\n".join(lines)


def _review_reference(participant: Mapping[str, Any]) -> str:
    raw = str(participant.get("id") or "").split("_", 1)[-1]
    token = re.sub(r"[^a-zA-Z0-9]", "", raw).upper()
    return f"R-{(token or 'UNKNOWN')[:8]}"


def _pending_review_cards(
    roster: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in roster
        if item.get("card_status") == "pending_review"
        and item.get("character_version_id")
    ]


def _resolve_pending_review(
    pending: list[Mapping[str, Any]],
    reference: str,
) -> Mapping[str, Any]:
    normalized = str(reference or "").strip()
    ordinal = normalized.removeprefix("#")
    if ordinal.isdigit():
        index = int(ordinal)
        if 1 <= index <= len(pending):
            return pending[index - 1]
        raise DatabaseNotFoundError(
            "待审核序号不存在，请发送 /酒馆 审核 刷新名单"
        )

    lowered = normalized.casefold()
    matches = []
    for item in pending:
        aliases = item.get("aliases")
        aliases = aliases if isinstance(aliases, list) else []
        candidates = {
            str(item.get("id") or ""),
            _review_reference(item),
            str(item.get("character_name") or ""),
            str(item.get("character_code") or ""),
            str(item.get("display_name") or ""),
            *(str(value) for value in aliases),
        }
        if any(
            candidate and candidate.casefold() == lowered
            for candidate in candidates
        ):
            matches.append(item)
    if not matches:
        raise DatabaseNotFoundError(
            "未找到对应的待审核角色，请发送 /酒馆 审核 刷新名单"
        )
    if len(matches) > 1:
        raise ValueError("角色标识不唯一，请改用名单中的审核号")
    return matches[0]


def format_pending_reviews(
    pending: list[Mapping[str, Any]],
    *,
    page: int = 1,
) -> str:
    if not pending:
        return "【待审核角色卡】当前没有待审核玩家。"
    pages = max(
        1,
        (len(pending) + REVIEW_LIST_PAGE_SIZE - 1)
        // REVIEW_LIST_PAGE_SIZE,
    )
    effective_page = min(max(1, int(page or 1)), pages)
    start = (effective_page - 1) * REVIEW_LIST_PAGE_SIZE
    items = pending[start : start + REVIEW_LIST_PAGE_SIZE]
    lines = [
        f"【待审核角色卡｜第 {effective_page}/{pages} 页"
        f"｜共 {len(pending)} 人】"
    ]
    for index, item in enumerate(items, start=start + 1):
        lines.append(
            f"{index}. "
            f"{item.get('character_name') or item.get('display_name')}"
            f"（{item.get('character_code') or '无代号'}）"
            f" · 玩家：{item.get('display_name')}"
            f" · 审核号：{_review_reference(item)}"
        )
    lines.extend(
        [
            "",
            "查看角色卡：/酒馆 审核 <序号或审核号>",
            "通过：/酒馆 审核 <序号或审核号> 通过 [备注]",
            "驳回：/酒馆 审核 <序号或审核号> 驳回 [原因]",
        ]
    )
    navigation = []
    if effective_page > 1:
        navigation.append(
            f"上一页：/酒馆 审核 第{effective_page - 1}页"
        )
    if effective_page < pages:
        navigation.append(
            f"下一页：/酒馆 审核 第{effective_page + 1}页"
        )
    if navigation:
        lines.append("｜".join(navigation))
    return "\n".join(lines)


def format_review_card(
    participant: Mapping[str, Any],
    template: Mapping[str, Any],
) -> str:
    profile = participant.get("card_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    stats = participant.get("card_stats")
    stats = stats if isinstance(stats, Mapping) else {}
    lines = [
        f"【角色卡审核详情｜{_review_reference(participant)}】",
        (
            f"玩家：{participant.get('display_name')}"
            f" · 角色："
            f"{participant.get('character_name') or '未命名'}"
            f"（{participant.get('character_code') or '无代号'}）"
        ),
        (
            f"角色卡版本：{participant.get('card_version_no') or 1}"
            f" · 模板版本："
            f"{participant.get('card_template_version') or 1}"
        ),
        "",
        "【角色资料】",
    ]
    for definition in template.get("fields") or []:
        if definition.get("stat_key"):
            continue
        value = profile.get(definition.get("key"), "")
        if definition.get("private"):
            value_text = (
                "（已填写，群聊中隐藏）"
                if str(value or "").strip()
                else "（未填写）"
            )
        else:
            value_text = str(value or "").strip() or "（未填写）"
        lines.append(f"· {definition.get('label')}：{value_text}")

    if uses_preset_stack_stats(template):
        lines.extend(["", "【角色数值｜多预设自动结算】"])
        try:
            resolved = calculate_preset_stack_stats(
                template,
                profile,
                require_complete=True,
            )
        except ValueError as exc:
            lines.append(f"· 校验结果：不通过（{exc}）")
        else:
            assert resolved is not None
            lines.append(
                "· 基础属性："
                + "、".join(
                    f"{resolved['labels'][key]}{value}"
                    for key, value in resolved["base"].items()
                )
            )
            for source in resolved["sources"]:
                bonus = "、".join(
                    f"{resolved['labels'][key]}{value:+d}"
                    for key, value in source["stat_bonus"].items()
                )
                lines.append(f"· {source['option_label']}：{bonus}")
            lines.append(
                "· 最终属性："
                + "、".join(
                    f"{resolved['labels'][key]}{value}"
                    f"({resolved['modifiers'][key]:+d})"
                    for key, value in resolved["raw"].items()
                )
            )
            lines.append(f"· 最终总和：{resolved['effective_total']}")
            stored_raw = stats.get("raw")
            stored_raw = stored_raw if isinstance(stored_raw, Mapping) else {}
            tampered = [
                resolved["labels"][key]
                for key, value in resolved["raw"].items()
                if key not in stored_raw or int(stored_raw[key]) != value
            ]
            stored_snapshot = stats.get("stat_generation_snapshot")
            if tampered:
                lines.append("· 校验结果：不通过（存档与来源不符：" + "、".join(tampered) + "）")
            elif not isinstance(stored_snapshot, Mapping):
                lines.append("· 校验结果：数值正确，但旧卡缺少来源快照，需管理员确认补写")
            else:
                lines.append("· 校验结果：通过")
        lines.extend(
            [
                "",
                "标为私密的字段不会在群聊展开；完整内容仍可在后台“准备与角色”查看。",
                f"通过：/酒馆 审核 {_review_reference(participant)} 通过 [备注]",
                f"驳回：/酒馆 审核 {_review_reference(participant)} 驳回 [原因]",
            ]
        )
        return "\n".join(lines)
    if uses_profession_preset_stats(template):
        lines.extend(["", "【角色数值｜职业预设】"])
        try:
            resolved = resolve_profession_stats(
                template,
                profile,
                require_complete=True,
            )
        except ValueError as exc:
            stored_raw = stats.get("raw")
            stored_raw = (
                stored_raw if isinstance(stored_raw, Mapping) else {}
            )
            lines.append(f"· 校验结果：不通过（{exc}）")
            if stored_raw:
                lines.append(
                    "· 存档数值："
                    + "、".join(
                        f"{key}{value}"
                        for key, value in stored_raw.items()
                    )
                )
            lines.append("· 建议驳回并让玩家重新使用「/酒馆 重填数值」。")
        else:
            lines.append(f"· 职业：{resolved['profession']}")
            lines.append(
                "· 基础属性："
                + "、".join(
                    f"{resolved['labels'][key]}{value}"
                    for key, value in resolved["base"].items()
                )
            )
            lines.append(
                f"· 主属性：{resolved['primary']['label']}"
                f" +{resolved['primary']['bonus']}"
            )
            lines.append(
                f"· 副属性：{resolved['secondary']['label']}"
                f" +{resolved['secondary']['bonus']}"
            )
            lines.append(
                "· 最终属性："
                + "、".join(
                    f"{resolved['labels'][key]}{value}"
                    f"({resolved['modifiers'][key]:+d})"
                    for key, value in resolved["raw"].items()
                )
            )
            lines.append(f"· 基础总和：{resolved['base_total']}")
            lines.append(f"· 加成总和：{resolved['bonus_total']}")
            lines.append(f"· 最终总和：{resolved['effective_total']}")
            stored_raw = stats.get("raw")
            stored_raw = (
                stored_raw if isinstance(stored_raw, Mapping) else {}
            )
            tampered = [
                resolved["labels"][key]
                for key, value in resolved["raw"].items()
                if key in stored_raw and int(stored_raw[key]) != value
            ]
            if tampered:
                lines.append(
                    "· 校验结果：不通过（存档与公式不符："
                    + "、".join(tampered)
                    + "）"
                )
            else:
                lines.append("· 校验结果：通过")
        lines.extend(
            [
                "",
                "标为私密的字段不会在群聊展开；"
                "完整内容仍可在后台“准备与角色”查看。",
                (
                    f"通过：/酒馆 审核 {_review_reference(participant)}"
                    " 通过 [备注]"
                ),
                (
                    f"驳回：/酒馆 审核 {_review_reference(participant)}"
                    " 驳回 [原因]"
                ),
            ]
        )
        return "\n".join(lines)

    attributes = (template.get("stats") or {}).get("attributes") or []
    raw_stats = stats.get("raw")
    raw_stats = raw_stats if isinstance(raw_stats, Mapping) else {}
    modifiers = stats.get("modifiers")
    modifiers = modifiers if isinstance(modifiers, Mapping) else {}
    budget = int(
        stats.get("budget")
        or (template.get("stats") or {}).get("budget")
        or 0
    )
    used = 0
    lines.extend(["", "【角色数值】"])
    for attribute in attributes:
        key = str(attribute.get("key") or "")
        fallback = profile.get(f"stat_{key}", attribute.get("default", 0))
        value = int(raw_stats.get(key, fallback))
        modifier = int(modifiers.get(key, 0))
        used += value
        lines.append(
            f"· {attribute.get('label') or key}：{value}"
            f"（检定修正 {modifier:+d}）"
        )
    lines.extend(
        [
            f"· 预算：已使用 {used}/{budget} 点 · 剩余 {budget - used} 点",
            "",
            "标为私密的字段不会在群聊展开；"
            "完整内容仍可在后台“准备与角色”查看。",
            (
                f"通过：/酒馆 审核 {_review_reference(participant)}"
                " 通过 [备注]"
            ),
            (
                f"驳回：/酒馆 审核 {_review_reference(participant)}"
                " 驳回 [原因]"
            ),
        ]
    )
    return "\n".join(lines)


def _format_remaining_time(value: Any) -> str:
    try:
        remaining = max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    days, remaining = divmod(remaining, 24 * 60 * 60)
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def _story_reply_parts(value: str) -> list[str]:
    text = str(value or "").strip()
    marker = "【回合秩序】"
    idx = text.find(marker)
    if idx == -1:
        return [text] if text else []
    # 兼容标记前可能带有的 emoji 前缀（如 ⚔️ ），整段作为回合内容保留
    prefix_start = idx
    while prefix_start > 0 and text[prefix_start - 1] not in "\n\r":
        prefix_start -= 1
    story = text[:prefix_start].strip()
    turn = text[prefix_start:].strip()
    if not story:
        return [turn]
    return [story, turn]



__all__ = [
    "parse_instance_list_page",
    "_compact_instance_intro",
    "format_turn_status",
    "format_existing_instance_list",
    "format_world_list",
    "format_opening_menu",
    "format_instance_list",
    "_instance_list_footer",
    "format_roster",
    "format_vote",
    "format_recovered_timer",
    "world_preset_brief",
    "_profession_preset_line",
    "_format_profession_step_prompt",
    "format_card_prompt",
    "format_card_preview",
    "_review_reference",
    "_pending_review_cards",
    "_resolve_pending_review",
    "format_pending_reviews",
    "format_review_card",
    "_format_remaining_time",
    "_story_reply_parts",
]
