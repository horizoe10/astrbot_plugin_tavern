from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .constants import CORE_NARRATOR_RULES
from .world_contract import world_contract
from .chat_experience import narrator_directives


def _json(value: Any) -> str:
    # Prompt payloads are machine-readable; whitespace only consumes context.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


RESOLUTION_SCHEMA = {
    "mode": "resolve | check",
    "narrative": "最终叙事；check 阶段留空",
    "check": {
        "stat": "检定维度",
        "reason": "为什么结果存在不确定性",
        "difficulty": "5-25 的整数",
        "modifier": "填 0；插件会按角色卡与世界查表覆盖为权威加值",
        "risk": "safe | controlled | dangerous | desperate | lethal",
        "check_type": "standard | leader | group | resistance | opposed",
        "advantage_sources": ["只能引用角色卡、装备、状态、协助或场景中已存在的有利事实"],
        "disadvantage_sources": ["只能引用伤势、环境、时间压力或场景中已存在的不利事实"],
        "known_consequences": "玩家当前能够预见的失败后果；致命风险必须明确",
        "visibility": "public | immersive | hidden",
        "participant_ids": ["集体检定或独立抵抗时的参与角色 ID"],
        "opponent_modifier": "对抗检定时填 0；插件会覆盖为权威值",
    },
    "state_patch": {
        "location": "可选，新地点",
        "time": "可选，新时间",
        "scene_summary": "可选，当前场景简要状态",
        "facts_add": ["新增且已确定的事实"],
        "facts_remove": ["已不再成立的旧事实，必须精确匹配"],
        "inventory_ops": [
            {
                "owner_id": "玩家ID或角色标识",
                "item": "物品名",
                "delta": "有符号整数",
            }
        ],
        "relationship_ops": [
            {
                "source": "关系发起方（优先用插件提供的 participant_id / NPC stable_key / 名称）",
                "target": "关系对象（同上）",
                "dimension": "信任/亲近/敬畏/敌意等",
                "delta": "-20 到 20 的整数",
            }
        ],
        "economy_ops": [
            {
                "operation_id": "唯一幂等键（必填，避免重复扣款）",
                "kind": "credit | debit | transfer | reward | fine | purchase | sale | adjust",
                "currency_id": "世界包声明的货币稳定 ID",
                "amount": "主单位金额",
                "from_owner_type": "player | party | npc | shop | faction | 世界包自定义",
                "from_owner_ref": "所有者稳定 ID",
                "to_owner_type": "同上",
                "to_owner_ref": "同上",
                "reason": "变动原因（写入交易日志）",
            }
        ],
    },
    "memories": [
        {
            "scope": "world | player | npc",
            "scope_id": "对应ID；world 可留空",
            "kind": "fact | promise | relationship | discovery | injury",
            "content": "值得跨回合保留的简短事实",
            "importance": "1-5",
            "tags": ["检索标签"],
            "visibility": "public | host | private",
            "locked": "只有确定且不可被普通摘要淘汰的重要事实才为 true",
            "pinned": "需要优先进入上下文时为 true",
            "supersedes_id": "明确取代旧记忆时填写旧记忆 ID",
        }
    ],
    "next_choices": [
        {
            "key": "A | B | C | D，必须恰好四项且不重复",
            "actor_id": "必须等于插件提供的 next_actor.participant_id",
            "text": "下一位玩家可选择的行动意图，不预设结果",
            "danger_id": "世界声明的危险度 ID",
            "check": {
                "required": "布尔值",
                "attribute_id": "attribute 模式填写世界属性 ID；其他模式留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "5-25",
                "known_consequences": "玩家可预见的风险；不能泄露隐藏真相",
                "advantage_sources": ["选项生成时已经成立的优势来源"],
                "disadvantage_sources": ["选项生成时已经成立的劣势来源"],
            },
            "collective": "布尔值；影响全队时为 true",
        }
    ],
    "group_decision": {
        "question": "只有遇到影响全队的关键节点时填写",
        "options": [
            {
                "key": "A-D，2 至 4 项",
                "text": "互斥且不提前保证结果的集体方案",
            }
        ],
    },
    "return_progress": {
        "request_id": "仅在已存在返场任务且本轮产生真实推进时填写",
        "evidence": "本轮如何推进或完成返场条件",
        "completed": "只有条件已经在剧情中实际完成时才为 true",
    },
    "npc_ops": [
        {
            "op": "create | update | archive | depart | kill",
            "npc_id": "更新既有 NPC 时填写稳定 ID",
            "name": "姓名；每回合最多创建 3 名",
            "aliases": ["别名"],
            "role_type": "npc | creature | faction",
            "persistent": "需要跨回合保留时为 true",
            "registration_reasons": [
                "direct_interaction | important_clue | long_term_memory"
            ],
            "public_profile": {
                "identity": "公开身份",
                "appearance": "外貌",
                "personality": "可观察到的性格",
            },
            "runtime_state": {
                "location": "当前位置",
                "faction": "阵营",
                "status": "active | departed | dead | archived",
            },
            "known_facts": ["该 NPC 确实知道的事实"],
            "misconceptions": ["误解、谣言或错误认知"],
        }
    ],
    "clock_ops": [
        {
            "op": "create | advance | set | complete | archive",
            "clock_id": "既有时钟 ID",
            "title": "时钟名称",
            "segments": "4 | 6 | 8",
            "delta": "推进格数",
            "value": "set 时的新值",
            "visibility": "public | vague | hidden",
            "trigger": "填满时只触发一次的事件",
        }
    ],
    "ledger_ops": [
        {
            "op": "create | update | complete | fail | archive",
            "entry_id": "既有条目 ID",
            "kind": "main | side | objective | clue | milestone",
            "title": "标题",
            "description": "当前已确认的信息",
            "visibility": "public | host",
        }
    ],
    "status_ops": [
        {
            "op": "add | update | remove",
            "target_id": "角色或参与者 ID",
            "name": "伤势或状态名",
            "severity": "minor | serious | critical",
            "affects": ["受影响的具体行动"],
            "effect": "通常为相关检定劣势，不得无差别影响全部行动",
            "removal": "明确解除条件",
        }
    ],
    "assist_ops": [
        {
            "target_id": "被协助角色 ID",
            "stat": "适用检定维度",
            "method": "本回合实际采取的协助方式",
            "expires_round": "默认本轮结束失效",
        }
    ],
    "director_note": "仅供审计的简短裁定依据，不得泄露隐藏剧情",
}


_NON_NARRATIVE_RULE_KEYS = {
    "capabilities",
    "character_card",
    "context_budget",
    "danger_levels",
    "default_difficulty",
    "difficulty_max",
    "difficulty_min",
    "event_pool",
    "entity_registry",
    "opening_choices",
    "option_presentation",
    "world_schema_version",
}

_CARD_ONLY_SETTING_KEYS = {
    "attribute_progression",
    "factions",
    "origin_regions",
    "power_systems",
    "professions",
    "regions",
    "social_identities",
    "species_and_identities",
}


def compact_world_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    """Compile only rules useful to narration; omit authoring/card payloads."""
    raw = world.get("rules", {})
    rules = raw if isinstance(raw, Mapping) else {}
    result = {
        key: value
        for key, value in rules.items()
        if key not in _NON_NARRATIVE_RULE_KEYS and key != "setting_modules"
    }
    setting_modules = rules.get("setting_modules")
    if isinstance(setting_modules, Mapping):
        compact_modules = {
            key: value
            for key, value in setting_modules.items()
            if key not in _CARD_ONLY_SETTING_KEYS
        }
        if compact_modules:
            result["setting_modules"] = compact_modules
    return result


def _schema_for(*, allow_check: bool) -> dict[str, Any]:
    schema = json.loads(json.dumps(RESOLUTION_SCHEMA, ensure_ascii=False))
    if not allow_check:
        schema["mode"] = "resolve"
        schema.pop("check", None)
        schema["next_choices"][0]["check"] = (
            "null 或下一回合预先声明的检定；safe 必须为 null"
        )
    return schema


def system_prompt(
    world: Mapping[str, Any],
    *,
    allow_check: bool = True,
    capability_projection: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Purpose-built narrative system prompt without card-authoring duplication."""
    contract = world_contract(world)
    effective_allow_check = allow_check and contract["resolution"]["mode"] in {
        "dice_only",
        "attribute",
    }
    projection = [
        {
            "capability_ref": item.get("capability_ref"),
            "available": bool(item.get("available", True)),
            "state": item.get("state", {}),
        }
        for item in (capability_projection or ())
        if isinstance(item, Mapping) and item.get("capability_ref")
    ]
    capability_block = ""
    if projection:
        capability_block = (
            "<available_capabilities>\n"
            f"{_json(projection)}\n"
            "Only these projected capabilities may be narrated as currently usable. "
            "The plugin remains authoritative for costs, targets, constraints and effects.\n"
            "</available_capabilities>\n\n"
        )
    experience = narrator_directives(world)
    experience_block = (
        "<multiplayer_experience>\n"
        f"{_json(experience)}\n"
        "</multiplayer_experience>\n\n"
        if experience
        else ""
    )
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<narrative_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</narrative_world_rules>\n\n"
        f"{capability_block}"
        f"{experience_block}"
        "<required_output_schema>\n"
        f"{_json(_schema_for(allow_check=effective_allow_check))}\n"
        "</required_output_schema>\n"
    )


def _history(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(
            {
                "turn": event.get("turn_no"),
                "role": event.get("role"),
                "actor_id": event.get("actor_id"),
                "actor_name": event.get("actor_name"),
                "content": event.get("content"),
            }
        )
    return result


def _party(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in roster:
        if not isinstance(item, Mapping) or item.get(
            "participation_status"
        ) not in {"active", "standby", "away"}:
            continue
        profile = item.get("card_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        runtime = item.get("runtime_state")
        runtime = runtime if isinstance(runtime, Mapping) else {}
        # 非行动角色只暴露现场可观察信息，避免模型把其性格、秘密、
        # 专长或决定权混入下一位玩家的行动选项。
        result.append(
            {
                "participant_id": item.get("id"),
                "character_name": item.get("character_name"),
                "character_code": item.get("character_code"),
                "participation_status": item.get("participation_status"),
                "public_appearance": profile.get("appearance", ""),
                "visible_location": runtime.get("current_location", ""),
                "visible_statuses": runtime.get("statuses", []),
            }
        )
    return result


def _character_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = value.get("profile")
    if not isinstance(profile, Mapping):
        profile = value.get("card_profile")
    stats = value.get("stats")
    if not isinstance(stats, Mapping):
        stats = value.get("card_stats")
    runtime = value.get("runtime_state")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    return {
        "participant_id": value.get("participant_id") or value.get("id"),
        "character_name": value.get("character_name"),
        "character_code": value.get("character_code"),
        "display_name": value.get("display_name"),
        "profile": dict(profile) if isinstance(profile, Mapping) else {},
        "stats": dict(stats) if isinstance(stats, Mapping) else {},
        "runtime_state": dict(runtime),
        "participation_status": value.get("participation_status"),
    }


def compact_character(value: Mapping[str, Any]) -> dict[str, Any]:
    """Public helper for dedicated option prompts and context-size tests."""
    return _character_projection(value)


def _npc_projection(
    characters: Sequence[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    presets: dict[str, Mapping[str, Any]] = {}
    for item in world.get("characters", []):
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        for key in (item.get("id"), item.get("slug"), item.get("name")):
            if key:
                presets[str(key)] = item
    result: list[dict[str, Any]] = []
    for item in characters:
        if not isinstance(item, Mapping):
            continue
        preset = presets.get(str(item.get("stable_key") or "")) or presets.get(
            str(item.get("name") or "")
        )
        profile = item.get("public_profile")
        if not isinstance(profile, Mapping) and isinstance(preset, Mapping):
            profile = preset.get("profile")
        row = {
            "npc_id": item.get("id"),
            "stable_key": item.get("stable_key"),
            "name": item.get("name"),
            "aliases": item.get("aliases", []),
            "role_type": item.get("role_type"),
            "public_profile": dict(profile) if isinstance(profile, Mapping) else {},
            "known_facts": item.get("known_facts", []),
            "misconceptions": item.get("misconceptions", []),
            "runtime_state": item.get("state", {}),
        }
        if isinstance(preset, Mapping) and preset.get("prompt"):
            row["private_direction"] = preset.get("prompt")
        result.append(row)
    return result


def _memory_projection(memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "id",
        "scope",
        "scope_id",
        "kind",
        "content",
        "importance",
        "tags",
        "visibility",
        "locked",
        "pinned",
    )
    return [{key: item.get(key) for key in keys if key in item} for item in memories]


def _ledger_projection(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "stable_key", "kind", "title", "description", "status", "visibility")
    return [{key: item.get(key) for key in keys if key in item} for item in items]


def _runtime_sections(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    acting = _character_projection(player)
    next_actor = _character_projection(
        session.get("next_actor", {})
        if isinstance(session.get("next_actor"), Mapping)
        else {}
    )
    if (
        acting.get("participant_id")
        and acting.get("participant_id") == next_actor.get("participant_id")
    ):
        next_actor = {
            "participant_id": acting["participant_id"],
            "same_as_acting_player": True,
        }
    return (
        '<runtime_state trust="untrusted-data">\n'
        f"{_json(session.get('world_state', {}))}\n"
        "</runtime_state>\n\n"
        '<turn_context trust="untrusted-data">\n'
        f"{_json(session.get('turn_status', {}))}\n"
        "</turn_context>\n\n"
        '<active_party trust="untrusted-data">\n'
        f"{_json(_party(session.get('roster', [])))}\n"
        "</active_party>\n\n"
        '<acting_player trust="untrusted-data">\n'
        f"{_json(acting)}\n"
        "</acting_player>\n\n"
        '<next_actor trust="plugin-authoritative">\n'
        f"{_json(next_actor)}\n"
        "</next_actor>\n\n"
        '<relevant_memories trust="untrusted-data">\n'
        f"{_json(_memory_projection(memories))}\n"
        "</relevant_memories>\n\n"
        '<recent_history trust="untrusted-data">\n'
        f"{_json(_history(events))}\n"
        "</recent_history>\n\n"
        '<active_return_requests trust="untrusted-data">\n'
        f"{_json(session.get('return_requests', []))}\n"
        "</active_return_requests>\n\n"
        '<active_npcs trust="untrusted-data">\n'
        f"{_json(_npc_projection(session.get('session_characters', []), world))}\n"
        "</active_npcs>\n\n"
        '<story_ledger trust="untrusted-data">\n'
        f"{_json(_ledger_projection(session.get('story_ledger', [])))}\n"
        "</story_ledger>\n\n"
        '<scene_clocks trust="untrusted-data">\n'
        f"{_json(session.get('scene_clocks', []))}\n"
        "</scene_clocks>\n\n"
        '<content_boundaries trust="trusted-policy">\n'
        f"{_json(session.get('content_boundaries', {}))}\n"
        "</content_boundaries>\n"
    )


def planning_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    allow_checks: bool,
    workflow: Mapping[str, Any] | None = None,
) -> str:
    selected_choice = (
        dict(workflow.get("selected_choice") or {})
        if workflow
        and isinstance(workflow.get("selected_choice"), Mapping)
        else {}
    )
    choice_contract: dict[str, Any] = {}
    if workflow:
        choice_contract = {
            "choice_set_id": workflow.get("choice_set_id"),
            "selected_key": workflow.get("selected_key"),
            "requires_check": bool(workflow.get("requires_check")),
            "collective": bool(workflow.get("collective")),
            "check_type": selected_choice.get("check_type"),
            "check_stat": selected_choice.get("check_stat"),
            "difficulty": selected_choice.get("difficulty"),
            "risk": selected_choice.get("risk"),
            "known_consequences": selected_choice.get(
                "known_consequences"
            ),
            "advantage_sources": selected_choice.get(
                "advantage_sources",
                [],
            ),
            "disadvantage_sources": selected_choice.get(
                "disadvantage_sources",
                [],
            ),
        }
    if choice_contract.get("requires_check"):
        mode_rule = (
            "本条行动来自插件已锁定的必检选项，必须返回 mode=check；"
            "不得直接返回 resolve。check 阶段 narrative 留空、"
            "state_patch={}、memories=[]，且不要输出 next_choices "
            "与 group_decision；选项中已有的检定属性、难度、风险、"
            "类型、已知后果与优劣势不得弱化或改写。"
        )
    elif workflow:
        mode_rule = (
            "本条行动来自插件已锁定的免检选项，必须返回 mode=resolve；"
            "不得临时追加检定或隐藏加码。"
        )
    else:
        mode_rule = (
            "若行动结果存在风险、对抗或显著不确定性，返回 mode=check；"
            "check 阶段 narrative 为空，state_patch 与 memories 必须为空。"
            if allow_checks
            else (
                "本轮不启用随机检定；请依据已有事实保守裁定并"
                "返回 mode=resolve。"
            )
        )
    return (
        "裁定下面这一条玩家行动。"
        f"{mode_rule}\n"
        "若无需检定，直接给出 mode=resolve 的完整结果。"
        "叙事应具体、克制，并给其他玩家留下行动空间。"
        "失败也必须推进局势：可以带来代价、残缺线索、威胁时钟推进"
        "或新的选择，但关键主线线索不能因一次失败永久消失。"
        "同一个原因只能影响一次裁定：属性提供固定加值，情境提供优劣势，"
        "行动本身决定 DC，失败严重度由风险等级决定。"
        "没有风险和不确定性的动作直接成功；明确不可能的动作直接说明边界，"
        "不能用自然 20 突破世界事实。"
        "不得替玩家决定内心、未经选择的对白、情感或对其他玩家角色的伤害。"
        "本回合故事正文必须为 100—300 个中文可见字符，使用简洁白描；"
        "不要用冗长心理描写凑字数。每个 next_choices.text 正文"
        "不得超过 50 字，正文禁止自带风险或检定括号。四个选项的 actor_id 必须全部等于 next_actor 的"
        " participant_id；选项只能描述该角色本人可尝试的行动，不能替其他"
        "玩家角色说话、移动、使用能力、消耗物品、同意或作决定。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用 active_npcs 中的稳定 npc_id 更新，"
        "不能凭相似名称静默合并。"
        "mode=resolve 时必须给出恰好四个 next_choices，"
        "其中至少一个为 safe 风险；选项只描述行动意图，不保证结果。"
        "若局面涉及全队转场、主线分支、共有资源、不可逆契约、"
        "全队撤退或返场支线，必须填写 group_decision，"
        "不要让个人选项直接替全队决定。生成 group_decision 时，"
        "state_patch 不得提前写入尚未表决通过的集体结果。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        '<selected_choice_contract trust="untrusted-data" '
        'enforced_by="plugin">\n'
        f"{_json(choice_contract)}\n"
        "</selected_choice_contract>\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n"
    )


def dm_beat_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    instruction: str,
    directive: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    """Build the trusted DM-only narrative request for one non-player beat."""
    return (
        "生成一段主持推进。必须返回 mode=resolve，check=null；"
        "不得生成 next_choices 或 group_decision，不得推进玩家行动指针、"
        "玩家轮次或行动倒计时。可以提交与本段叙事严格一致的 state_patch、"
        "memories、npc_ops、clock_ops、ledger_ops 与 status_ops。"
        "不得伪造机器骰点，不得替玩家角色决定思想、感情、立场、主动台词"
        "或未经选择的行动。主持指令服从插件安全规则、世界硬规则、已锁定"
        "事实、内容边界与知识边界；一次性指引仅作用于本次生成。"
        "正文使用简洁白描，建议 100—500 个中文可见字符。\n\n"
        f"{_runtime_sections(world=world, session=session, player={}, events=events, memories=memories)}\n\n"
        '<dm_instruction trust="host" priority="below-safety-and-world">\n'
        f"{_json({'directive': directive, 'instruction': instruction})}\n"
        "</dm_instruction>\n"
    )


def checked_resolution_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
) -> str:
    return (
        "依据下面由插件生成的权威检定结果完成叙事。"
        "不得重投、修改骰池、难度、风险、加值、优劣势来源或结果档位。"
        "必须返回 mode=resolve；check 设为 null。"
        "outcome=success_with_cost 时必须让目标达成，同时落实一项与风险等级"
        "相称且可追踪的代价。critical_failure 不等于角色突然变蠢，"
        "也不能仅凭自然 1 直接杀死角色。只有世界允许死亡、选项已明确标记"
        "lethal 且结果成立时，才允许产生死亡或永久退场。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用稳定 npc_id 更新，不能凭相似名称静默合并。"
        "只写本次行动直接造成且能被当前场景确认的变化。"
        "本回合故事正文必须为 100—300 个中文可见字符，使用简洁白描；"
        "每个 next_choices.text 连同括号内容不得超过 50 字。"
        "四个选项的 actor_id 必须全部等于 next_actor 的 participant_id，"
        "并且只能描述该角色本人可尝试的行动，不得操控其他玩家角色。"
        "同时必须生成恰好四个合规 next_choices；"
        "关键集体节点使用 group_decision；生成表决时，state_patch "
        "不得提前写入尚未通过的集体结果。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n\n"
        "<authoritative_check>\n"
        f"{_json({'request': dict(check), 'result': dict(dice)})}\n"
        "</authoritative_check>\n"
    )


def repair_prompt(
    raw_output: str,
    error: str,
    original_prompt: str,
) -> str:
    # The system prompt already contains the trusted world context and schema.
    # Repeating the original task here used to double large prompts on repair.
    del original_prompt
    return (
        "上一份输出无法通过校验。只修复 JSON 结构、字段类型、正文长度、"
        "选项长度与行动角色归属；可以在不改变已发生事实的前提下压缩或"
        "补足叙事，并重写越权选项。不得改变检定结论、世界状态变化、代价"
        "或记忆事实，也不要解释错误。返回单个 JSON 对象。\n\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:12000]}\n"
        "</invalid_output>\n"
    )


_CHOICE_SCHEMA = {
    "choices": [
        {
            "key": "A | B | C | D",
            "actor_id": "插件提供的 participant_id",
            "text": "不预设结果的行动意图，最多 50 字",
            "danger_id": "safe | controlled | dangerous | desperate | lethal",
            "check": {
                "required": True,
                "attribute_id": "世界属性稳定 ID；纯骰或免检留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "由插件按 danger_id 覆盖",
                "known_consequences": "玩家当前可预见的失败后果",
                "advantage_sources": ["已经成立的优势来源"],
                "disadvantage_sources": ["已经成立的劣势来源"],
            },
            "collective": False,
        }
    ]
}


def choice_system_prompt(world: Mapping[str, Any]) -> str:
    """Small system prompt used only for A-D generation and repair."""
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "你的当前任务仅是生成下一位角色的 A、B、C、D 四个行动选项。"
        "不要续写故事，不要输出状态补丁、记忆或骰点结果。\n\n"
        "<required_output_schema>\n"
        f"{_json(_CHOICE_SCHEMA)}\n"
        "</required_output_schema>\n"
    )


def choice_generation_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    participant: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    avoid: Sequence[Mapping[str, Any]] = (),
    validation_error: str = "",
    story_context: str = "",
) -> str:
    contract = world_contract(world)
    resolution_mode = str(contract["resolution"]["mode"])
    risk_policy = contract["resolution"]["difficulty_policy"]
    if resolution_mode in {"none", "narrative"}:
        check_rule = "当前世界不启用检定，四项 check 必须全部为 null。"
    elif resolution_mode == "dice_only":
        check_rule = (
            "当前世界使用纯骰检定；需要检定时 attribute_id 留空。"
        )
    else:
        attributes = "、".join(
            f"{item.get('key')}={item.get('label')}"
            for item in contract["attributes"]
        )
        check_rule = f"需要检定时 attribute_id 只能使用：{attributes}。"
    return (
        "只生成当前角色在当前场景可选的四个行动意图。"
        "必须恰好包含 A、B、C、D，至少一个 safe；actor_id 必须等于"
        " acting_character.participant_id。每项正文最多 50 字且不得自带"
        "风险或检定括号；不得预设成功，不得添加角色没有的能力、物品或知识。"
        "safe 代表没有显著风险与不确定性，check 必须为 null；"
        "controlled、dangerous、desperate、lethal 仅在结果确有不确定性时"
        "才可配置 check。DC 由插件按风险映射，模型填写值不具权威性。"
        f"{check_rule}"
        "致命风险必须明确已知后果；同一原因不能同时提高 DC 和造成劣势。"
        "只能描述当前角色本人能够尝试的行为，不得替其他玩家角色行动或决定。"
        "影响全队的转场、主线分支、共有资源或不可逆决定只能标记"
        " collective=true。返回单个 JSON 对象，不要解释。\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<relevant_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</relevant_world_rules>\n\n"
        "<risk_dc_policy trust=\"plugin-authoritative\">\n"
        f"{_json(risk_policy)}\n"
        "</risk_dc_policy>\n\n"
        "<runtime_state>\n"
        f"{_json(session.get('world_state', {}))}\n"
        "</runtime_state>\n\n"
        "<acting_character>\n"
        f"{_json(_character_projection(participant))}\n"
        "</acting_character>\n\n"
        "<recent_history>\n"
        f"{_json(_history(list(events)[-8:]))}\n"
        "</recent_history>\n\n"
        "<avoid_repeating>\n"
        f"{_json([dict(item) for item in list(avoid)[-4:]])}\n"
        "</avoid_repeating>\n\n"
        "<resolved_story>\n"
        f"{str(story_context or '')[:3000]}\n"
        "</resolved_story>\n\n"
        "<previous_choice_error>\n"
        f"{str(validation_error or '')[:500]}\n"
        "</previous_choice_error>\n"
    )


def choice_repair_prompt(
    raw_output: str,
    error: str,
    *,
    world: Mapping[str, Any],
    participant: Mapping[str, Any],
) -> str:
    contract = world_contract(world)
    attributes = [
        {"id": item.get("key"), "label": item.get("label")}
        for item in contract["attributes"]
    ]
    return (
        "上一组选项未通过校验。只修复四个选项，不续写故事。"
        "必须保留 A、B、C、D；safe 的 check 必须为 null；"
        "actor_id 必须使用下面的权威 ID；属性只用稳定 ID。"
        "返回单个 JSON 对象，不要解释。\n\n"
        "<actor_id>"
        f"{_character_projection(participant).get('participant_id')}"
        "</actor_id>\n"
        "<allowed_attributes>"
        f"{_json(attributes)}"
        "</allowed_attributes>\n"
        "<risk_dc_policy>"
        f"{_json(contract['resolution']['difficulty_policy'])}"
        "</risk_dc_policy>\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:8000]}\n"
        "</invalid_output>\n"
    )
