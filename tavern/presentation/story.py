from .common import *
from .sessions import *
from .characters import *
from .reviews import *

def format_pending_reviews_compact(
    pending: list[Mapping[str, Any]],
) -> str:
    """群内待审核名单的紧凑版：一行汇总 + 私聊网页审核指引。"""

    if not pending:
        return "【待审核角色卡】当前没有待审核玩家。"
    names = "；".join(
        f"{index}. {item.get('character_name') or item.get('display_name')}"
        f"（{item.get('character_code') or '无代号'}）"
        for index, item in enumerate(pending, 1)
    )
    return (
        f"【待审核角色卡｜共 {len(pending)} 人】{names}\n"
        "📱 网页审核（推荐）：私聊 Bot 发送 /团 审核 获取链接，"
        "网页中查看详情并通过 / 驳回\n"
        "💬 群内快速审批：/团 审核 <序号或审核号> 通过|驳回 [备注]"
    )


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
            "查看角色卡：/团 审核 <序号或审核号>",
            "通过：/团 审核 <序号或审核号> 通过 [备注]",
            "驳回：/团 审核 <序号或审核号> 驳回 [原因]",
        ]
    )
    navigation = []
    if effective_page > 1:
        navigation.append(
            f"上一页：/团 审核 第{effective_page - 1}页"
        )
    if effective_page < pages:
        navigation.append(
            f"下一页：/团 审核 第{effective_page + 1}页"
        )
    if navigation:
        lines.append("｜".join(navigation))
    return "\n".join(lines)


def format_review_card(
    participant: Mapping[str, Any],
    template: Mapping[str, Any],
    world: Mapping[str, Any],
) -> str:
    profile = participant.get("card_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    stats = participant.get("card_stats")
    stats = stats if isinstance(stats, Mapping) else {}
    actor_view = project_actor_view(
        world,
        profile,
        viewer_role="player",
    )
    actor_title = str(
        actor_view.get("title")
        or participant.get("character_name")
        or ""
    ).strip()
    actor_subtitle = str(
        actor_view.get("subtitle")
        or participant.get("character_code")
        or ""
    ).strip()
    lines = [
        f"【角色卡审核详情｜{_review_reference(participant)}】",
        (
            f"玩家：{participant.get('display_name')}"
            f" · 角色：{actor_title or '角色名称数据缺失'}"
            + (f"（{actor_subtitle}）" if actor_subtitle else "")
        ),
        (
            f"角色卡版本：{participant.get('card_version_no') or 1}"
            f" · 模板版本："
            f"{participant.get('card_template_version') or 1}"
        ),
        "",
        "【角色资料】",
    ]
    for section in actor_view.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        lines.append(f"【{section.get('label') or '角色资料'}】")
        for field in section.get("items") or []:
            if not isinstance(field, Mapping):
                continue
            value_text = str(
                field.get("display_error")
                or field.get("display_value")
                or "（未填写）"
            )
            lines.append(
                f"· {field.get('label') or '字段名称解析失败'}：{value_text}"
            )
    if actor_view.get("problems"):
        lines.append("· 投影提示：角色资料有字段无法解析，请让管理员检查世界包。")

    if uses_preset_stack_stats(template):
        lines.extend(["", "【角色数值｜由角色选择自动计算】"])
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
                f"通过：/团 审核 {_review_reference(participant)} 通过 [备注]",
                f"驳回：/团 审核 {_review_reference(participant)} 驳回 [原因]",
            ]
        )
        return "\n".join(lines)
    if uses_profession_preset_stats(template):
        lines.extend(["", "【角色数值｜职业基础与主副属性】"])
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
            lines.append("· 建议驳回并让玩家重新使用「/团 重填数值」。")
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
                    f"通过：/团 审核 {_review_reference(participant)}"
                    " 通过 [备注]"
                ),
                (
                    f"驳回：/团 审核 {_review_reference(participant)}"
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
                f"通过：/团 审核 {_review_reference(participant)}"
                " 通过 [备注]"
            ),
            (
                f"驳回：/团 审核 {_review_reference(participant)}"
                " 驳回 [原因]"
            ),
        ]
    )
    return "\n".join(lines)


__all__ = [name for name in globals() if not name.startswith('__')]

