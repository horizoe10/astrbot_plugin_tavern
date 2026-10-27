"""副本实时仪表盘聚合（v0.12.0）。

对单个副本做只读聚合，供控制台「副本实时仪表盘」与「时间线回放」视图
消费。全部数据来自既有仓库只读接口，不产生写入；字段提取保持防御式
（``.get`` + 类型归一），避免把底层表结构的偶发变化泄漏给前端。

对外函数：
- ``dashboard_sessions(database)``：副本概览列表（含状态/世界/当前行动者）。
- ``session_dashboard(database, session_id)``：单个副本的实时聚合。
- ``session_timeline(database, session_id, limit)``：事件时间线（回放视图）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .constants import SESSION_RUNNING


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def _options(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, Mapping):
        items = payload.get("options") or payload.get("choices") or []
    else:
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            result.append(
                {
                    "key": _text(item.get("key"), _text(item.get("id"))),
                    "text": _text(item.get("text"), _text(item.get("label"))),
                    "risk": _text(item.get("risk")),
                    "collective": _bool(item.get("collective")),
                    "requires_check": _bool(
                        item.get("requires_check") or item.get("check")
                    ),
                }
            )
    return result


def _normalize_timer(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(item.get("id")),
        "timer_type": _text(item.get("timer_type")),
        "status": _text(item.get("status"), "active"),
        "participant": _text(
            item.get("character_name") or item.get("display_name")
        ),
        "deadline_at": _text(item.get("deadline_at")),
        "remaining_seconds": _int(item.get("remaining_seconds"), -1),
        "reminder_sent": _bool(item.get("reminder_sent")),
        "created_at": _text(item.get("created_at")),
    }


def _timer_sort_key(timer: Mapping[str, Any]) -> str:
    """倒序依据：优先 created_at（最新创建置顶），缺失时退化为 deadline。"""
    return _text(timer.get("created_at")) or _text(timer.get("deadline_at")) or ""


async def session_timers(
    database: Any,
    session_id: str,
    order: str = "desc",
) -> list[dict[str, Any]]:
    """副本的倒计时列表（0.12.0-A3，#3）。

    - 默认 ``desc``：最新内容置顶（created_at 倒序）；
    - ``asc``：最紧迫的（deadline 最早）置顶，适合倒计时场景；
    - 只返回活跃/暂停中的计时器，已结束的不出现在小窗口。
    """
    timers = await database.list_timers(session_id)
    normalized = [_normalize_timer(item) for item in timers]
    normalized = [
        item for item in normalized if item["status"] in {"active", "paused"}
    ]
    normalized.sort(
        key=_timer_sort_key,
        reverse=(str(order).strip().lower() != "asc"),
    )
    return normalized


def _normalize_vote(vote: Any) -> dict[str, Any] | None:
    if not vote or not isinstance(vote, Mapping):
        return None
    ballots = vote.get("ballots") or vote.get("votes") or []
    tally: dict[str, int] = {}
    ballot_rows: list[dict[str, str]] = []
    voted_ids: set[str] = set()
    for ballot in ballots if isinstance(ballots, list) else []:
        if not isinstance(ballot, Mapping):
            continue
        bid = _text(ballot.get("user_id"))
        bkey = _text(ballot.get("choice_key") or ballot.get("key"))
        if bid:
            voted_ids.add(bid)
        if bkey:
            tally[bkey] = tally.get(bkey, 0) + 1
        ballot_rows.append({"user_id": bid, "choice_key": bkey})
    eligible = [_text(x) for x in vote.get("eligible_user_ids") or []]
    unvoted = [u for u in eligible if u and u not in voted_ids]
    options = _options(vote.get("options"))
    for option in options:
        option["votes"] = tally.get(option["key"], 0)
    remaining_seconds = 0
    if vote.get("deadline_at"):
        try:
            from datetime import datetime, timezone as _tz

            raw = str(vote["deadline_at"]).replace("Z", "+00:00")
            deadline = datetime.fromisoformat(raw)
            remaining_seconds = max(
                0, int((deadline - datetime.now(_tz.utc)).total_seconds())
            )
        except Exception:
            remaining_seconds = 0
    return {
        "id": _text(vote.get("id")),
        "title": _text(vote.get("title"), _text(vote.get("topic"))),
        "status": _text(vote.get("status"), "open"),
        "winner_key": _text(vote.get("winner_key")),
        "deadline_at": _text(vote.get("deadline_at")),
        "remaining_seconds": remaining_seconds,
        "majority": _int(vote.get("majority"), 0),
        "voters": _int(vote.get("voter_count") or len(ballots), 0),
        "eligible_user_ids": eligible,
        "unvoted_user_ids": unvoted,
        "ballots": ballot_rows,
        "options": options,
    }


async def dashboard_sessions(database: Any) -> list[dict[str, Any]]:
    """副本概览列表：状态、世界、当前行动者与活跃计时器数量。

    v0.12.0（性能优化）：活跃计时器计数直接使用 ``list_sessions``
    已聚合的 ``active_timer_count`` 列，不再对每个副本单独查询计时器。
    """
    sessions = await database.list_sessions()
    result: list[dict[str, Any]] = []
    for session in sessions:
        session_id = _text(session.get("id"))
        if not session_id:
            continue
        turn: Mapping[str, Any] = {}
        try:
            turn = await database.get_turn_status(session_id)
        except Exception:
            turn = {}
        result.append(
            {
                "id": session_id,
                "name": _text(
                    session.get("instance_name") or session.get("name")
                ),
                "world": _text(
                    session.get("world_name") or session.get("world_slug")
                ),
                "state": _text(session.get("state"), "closed"),
                "round_no": _int(
                    turn.get("round_no"), _int(session.get("round_no"))
                ),
                "current_name": _text(turn.get("current_name")),
                "active_timers": _int(session.get("active_timer_count")),
                "selected": _bool(session.get("selected")),
            }
        )
    # 正在运行的排前面，其余按更新时间倒序。
    result.sort(
        key=lambda item: (
            item["state"] != SESSION_RUNNING,
            -item["round_no"],
        )
    )
    return result


async def _waiting_for(
    database: Any,
    session_id: str,
    state: str,
) -> str:
    """副本当前等待流程（vote / choice / preparation / admin），0.12.0-A3。"""
    if state == "closed":
        return ""
    row = await database.execute_read(
        """
        SELECT
            EXISTS(
                SELECT 1 FROM group_votes
                WHERE session_id = ? AND status = 'open'
            ) AS has_vote,
            EXISTS(
                SELECT 1 FROM choice_sets
                WHERE session_id = ? AND status = 'active'
            ) AS has_choice
        """,
        (session_id, session_id),
    )
    item = row[0] if row else {}
    if bool(item.get("has_vote")):
        return "vote"
    if bool(item.get("has_choice")):
        return "choice"
    if state == "preparing":
        return "preparation"
    if state == "paused":
        return "admin"
    return ""


async def _session_id_labels(
    database: Any,
    session_id: str,
) -> dict[str, str]:
    """A16：副本内实体 id → 显示名映射（统一实体解析器）。

    覆盖参与者（id / uuid 后缀 / 群用户 ID / 名称）、玩家（user_id）、
    NPC（session_characters id / stable_key / uuid 后缀 / 名称）与队伍。
    前端在此基础上做后缀匹配与降级显示，不再把完整内部 ID 当普通名称展示。
    """
    from .entity_resolver import strip_prefix

    labels: dict[str, str] = {}
    party_labels = {"队伍": "队伍", "party": "队伍", "team": "队伍"}

    def add(key: Any, name: str) -> None:
        key = _text(key)
        name = _text(name)
        if key and name and key not in labels:
            labels[key] = name

    try:
        rows = await database.execute_read(
            """
            SELECT id, character_name, display_name, group_user_id
            FROM participants WHERE session_id = ?
            """,
            (session_id,),
        )
        for row in rows or []:
            pid = _text(row.get("id"))
            name = _text(row.get("character_name")) or _text(
                row.get("display_name")
            )
            if not name:
                continue
            add(pid, name)
            add(strip_prefix(pid), name)
            add(row.get("group_user_id"), name)
            add(name, name)
    except Exception:
        pass
    try:
        rows = await database.execute_read(
            """
            SELECT id, stable_key, name FROM session_characters
            WHERE session_id = ?
            """,
            (session_id,),
        )
        for row in rows or []:
            name = _text(row.get("name"))
            if not name:
                continue
            add(row.get("id"), name)
            add(row.get("stable_key"), name)
            add(strip_prefix(_text(row.get("id"))), name)
            add(name, name)
    except Exception:
        pass
    try:
        rows = await database.execute_read(
            """
            SELECT user_id, display_name, character_name FROM players
            WHERE session_id = ? AND enabled = 1
            """,
            (session_id,),
        )
        for row in rows or []:
            name = _text(row.get("display_name")) or _text(
                row.get("character_name")
            )
            add(row.get("user_id"), name)
    except Exception:
        pass
    # A16.3：世界包常驻角色名称也纳入解析（关系键可能直接用 NPC 名称）。
    try:
        snap = await database.execute_read(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        )
        if snap:
            import json as _json

            snapshot = _json.loads(snap[0]["world_snapshot_json"] or "{}")
            for character in snapshot.get("characters") or []:
                if isinstance(character, dict):
                    add(character.get("name"), _text(character.get("name")))
                    add(f"npc:{character.get('name')}", _text(character.get("name")))
    except Exception:
        pass
    try:
        from .constants import DEFAULT_CHARACTERS

        for character in DEFAULT_CHARACTERS:
            name = _text(character.get("name"))
            add(name, name)
            add(f"npc:{name}", name)
    except Exception:
        pass
    labels.update(party_labels)
    return labels


def _party_like(key: str) -> bool:
    k = _text(key).strip().lower()
    return k in {"队伍", "party", "team", "小队", "团队"}


def _party_relations(world_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A23: 队伍/阵营级关系（含组织/势力关系）。

    - ``kind="info"``：字符串值的关系条目（描述性文本，即世界状态里的
      「组织 / 势力关系」），并入队伍板块展示；
    - ``kind="party"``：数值/字段型队伍级关系（键 ``来源→目标`` 且 source/target
      为队伍，或整键为队伍），展示双向好感度条。
    """
    relationships = world_state.get("relationships") or {}
    if not isinstance(relationships, Mapping):
        return []
    result: list[dict[str, Any]] = []
    for raw_key, value in relationships.items():
        raw_key = _text(raw_key)
        if not raw_key:
            continue
        # 组织/势力关系：字符串值条目（描述性文本）
        if isinstance(value, str):
            result.append(
                {
                    "kind": "info",
                    "target": raw_key,
                    "label": raw_key,
                    "summary": value,
                }
            )
            continue
        source = ""
        target = ""
        if "→" in raw_key:
            parts = raw_key.split("→", 1)
            source = parts[0].strip()
            target = parts[1].strip() if len(parts) > 1 else ""
        party_sourced = _party_like(source)
        party_targeted = _party_like(target)
        whole_key_party = (not source and not target) and _party_like(raw_key)
        if not (party_sourced or party_targeted or whole_key_party):
            continue
        label = target or raw_key
        entry: dict[str, Any] = {
            "kind": "party",
            "target": label,
            "label": label,
            "direction": "party→target" if party_sourced else (
                "target→party" if party_targeted else "party"
            ),
        }
        if isinstance(value, Mapping):
            rows = {
                _text(k): v
                for k, v in value.items()
                if isinstance(v, (int, float, str)) and v is not None
            }
            entry["fields"] = rows
            favor = value.get("好感")
            if not isinstance(favor, (int, float)):
                favor = value.get("信任")
            if not isinstance(favor, (int, float)):
                favor = next(
                    (v for v in value.values() if isinstance(v, (int, float))),
                    None,
                )
            entry["favor"] = favor
        elif isinstance(value, (int, float)):
            entry["favor"] = value
        result.append(entry)
    return result


def _member_keys(item: Mapping[str, Any]) -> set[str]:
    """A19: 角色在 world_state 关系/背包中可能使用的 owner/source 候选键。"""
    from .entity_resolver import strip_prefix

    keys: set[str] = set()
    for key in (
        item.get("id"),
        item.get("group_user_id"),
        item.get("user_id"),
        item.get("character_name"),
        item.get("character_code"),
        item.get("display_name"),
    ):
        key = _text(key)
        if key:
            keys.add(key)
            keys.add(strip_prefix(key))
    return keys


def _member_relationships(
    world_state: Mapping[str, Any],
    member_keys: set[str],
) -> list[dict[str, Any]]:
    """A19: 从 world_state.relationships（键为 ``来源→目标``）提取该角色的关系。

    同时保留 runtime_state.npc_relationships 作为补充来源（部分世界包/旧数据
    可能写入该字段），避免两种数据形态都丢失。
    """
    rows: list[dict[str, Any]] = []
    relationships = world_state.get("relationships") or {}
    if isinstance(relationships, Mapping):
        for raw_key, value in relationships.items():
            raw_key = _text(raw_key)
            if "→" not in raw_key:
                continue
            source, target = raw_key.split("→", 1)
            source = source.strip()
            target = _text(target).strip()
            if not source or source not in member_keys:
                continue
            if isinstance(value, Mapping):
                fields = {
                    _text(k): v
                    for k, v in value.items()
                    if isinstance(v, (int, float, str)) and v is not None
                }
                favor = value.get("好感")
                if not isinstance(favor, (int, float)):
                    favor = value.get("信任")
                if not isinstance(favor, (int, float)):
                    favor = next(
                        (v for v in value.values() if isinstance(v, (int, float))),
                        None,
                    )
                rows.append(
                    {
                        "target": target,
                        "favor": favor,
                        "stage": _text(
                            value.get("stage") or value.get("phase")
                        ),
                        "summary": _text(
                            value.get("summary") or value.get("note")
                        ),
                        "fields": fields,
                    }
                )
            elif isinstance(value, (int, float)):
                rows.append({"target": target, "favor": value, "fields": {}})
            elif isinstance(value, str):
                rows.append({"target": target, "summary": value, "fields": {}})
    return rows[:16]


def _inventory_items(payload: Any, limit: int = 24) -> list[dict[str, Any]]:
    """A20: 把 world_state.inventory 某 owner 的物品（Mapping 或 list）归一为列表。"""
    items: list[dict[str, Any]] = []
    if isinstance(payload, Mapping):
        for name, raw in list(payload.items())[:limit]:
            if isinstance(raw, Mapping):
                items.append(
                    {
                        "name": _text(name),
                        "count": raw.get("count")
                        or raw.get("quantity")
                        or raw.get("qty")
                        or raw.get("amount")
                        or 1,
                        "category": _text(raw.get("category")),
                        "description": _text(raw.get("description")),
                    }
                )
            else:
                items.append(
                    {
                        "name": _text(name),
                        "count": raw,
                        "category": "",
                        "description": "",
                    }
                )
    elif isinstance(payload, list):
        for name in list(payload)[:limit]:
            items.append(
                {
                    "name": _text(name),
                    "count": 1,
                    "category": "",
                    "description": "",
                }
            )
    return items


def _owner_inventory(world_state: Mapping[str, Any], owner_keys: list[str]) -> list[dict[str, Any]]:
    """A20: 按 owner 候选键从 world_state.inventory 取物品（队伍物资 / 任务物品）。"""
    inventory = world_state.get("inventory") or {}
    if not isinstance(inventory, Mapping):
        return []
    for owner in owner_keys:
        if owner in inventory:
            return _inventory_items(inventory[owner])
    return []


def _member_inventory(
    world_state: Mapping[str, Any],
    member_keys: set[str],
) -> list[dict[str, Any]]:
    """A19: 从 world_state.inventory（owner → {物品: 数量/详情}）提取角色随身物品。

    同时兼容 runtime_state.equipment 的 dict / list 两种形态作为补充。
    """
    items: list[dict[str, Any]] = []
    inventory = world_state.get("inventory") or {}
    if isinstance(inventory, Mapping):
        for owner, payload in inventory.items():
            if _text(owner) not in member_keys:
                continue
            if isinstance(payload, Mapping):
                for name, raw in payload.items():
                    if isinstance(raw, Mapping):
                        items.append(
                            {
                                "name": _text(name),
                                "count": raw.get("count")
                                or raw.get("quantity")
                                or raw.get("qty")
                                or raw.get("amount")
                                or 1,
                                "category": _text(raw.get("category")),
                                "description": _text(raw.get("description")),
                            }
                        )
                    else:
                        items.append(
                            {
                                "name": _text(name),
                                "count": raw,
                                "category": "",
                                "description": "",
                            }
                        )
            elif isinstance(payload, list):
                for name in payload:
                    items.append(
                        {
                            "name": _text(name),
                            "count": 1,
                            "category": "",
                            "description": "",
                        }
                    )
    return items[:24]


def _normalize_equipment(runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A19: 归一化 runtime_state.equipment（dict 或 list）为物品列表。"""
    equipment = runtime.get("equipment")
    if isinstance(equipment, Mapping):
        return [
            {
                "name": _text(name),
                "count": raw if isinstance(raw, (int, float, str)) else 1,
                "category": "",
                "description": "",
            }
            for name, raw in list(equipment.items())[:24]
        ]
    if isinstance(equipment, list):
        return [
            {
                "name": _text(item),
                "count": 1,
                "category": "",
                "description": "",
            }
            for item in list(equipment)[:24]
        ]
    return []


def _normalize_reputation(value: Any) -> str:
    """A19: reputation 可能是标量或 dict，统一为可读文本。"""
    if isinstance(value, Mapping):
        parts = [
            f"{_text(k)} {v}"
            for k, v in value.items()
            if isinstance(v, (int, float, str)) and v is not None
        ]
        return " · ".join(parts) if parts else ""
    return _text(value)


def _squad(
    roster_rows: list[Mapping[str, Any]],
    turn: Mapping[str, Any],
    world_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A19: 小队列表——角色卡数值 / 背包 / 关系 / 状态 / 行动状态整合进每张角色卡。

    关系与背包以 ``world_state``（引擎权威数据）为准，runtime_state 作为补充，
    避免 A18 读取从不写入的 runtime 字段导致恒空。
    """
    world_state = world_state if isinstance(world_state, Mapping) else {}
    order = [
        item for item in (turn.get("order") or []) if isinstance(item, Mapping)
    ]
    position_by_user: dict[str, int] = {}
    for item in order:
        uid = _text(item.get("user_id"))
        if uid:
            position_by_user[uid] = _int(item.get("position"))
    current_user_id = _text(turn.get("current_user_id"))
    result: list[dict[str, Any]] = []
    for item in roster_rows:
        if not isinstance(item, Mapping):
            continue
        user_id = _text(item.get("group_user_id") or item.get("user_id"))
        runtime = item.get("runtime_state") or {}
        if not isinstance(runtime, Mapping):
            runtime = {}
        profile = item.get("card_profile") or {}
        if not isinstance(profile, Mapping):
            profile = {}
        stats = item.get("card_stats") or {}
        if not isinstance(stats, Mapping):
            stats = {}
        member_keys = _member_keys(item)
        rel_rows = _member_relationships(world_state, member_keys)
        if not rel_rows:
            # 补充：runtime_state.npc_relationships（旧数据/世界包自定义形态）
            npc_rel = runtime.get("npc_relationships") or {}
            if isinstance(npc_rel, Mapping):
                for target, value in list(npc_rel.items())[:16]:
                    if isinstance(value, Mapping):
                        rel_rows.append(
                            {
                                "target": _text(target),
                                "favor": value.get("favor")
                                if isinstance(value.get("favor"), (int, float))
                                else None,
                                "stage": _text(
                                    value.get("stage") or value.get("phase")
                                ),
                                "summary": _text(
                                    value.get("summary") or value.get("note")
                                ),
                                "fields": {},
                            }
                        )
                    elif isinstance(value, (int, float)):
                        rel_rows.append(
                            {"target": _text(target), "favor": value, "fields": {}}
                        )
                    elif isinstance(value, str):
                        rel_rows.append(
                            {"target": _text(target), "summary": value, "fields": {}}
                        )
        inventory = _member_inventory(world_state, member_keys)
        if not inventory:
            inventory = _normalize_equipment(runtime)
        statuses = runtime.get("statuses") or []
        if not isinstance(statuses, list):
            statuses = []
        result.append(
            {
                "id": _text(item.get("id")),
                "group_user_id": user_id,
                "character_name": _text(item.get("character_name")),
                "character_code": _text(item.get("character_code")),
                "display_name": _text(item.get("display_name")),
                "participation_status": _text(item.get("participation_status")),
                "card_status": _text(item.get("card_status")),
                "ready": _bool(item.get("ready")),
                "role": _text(
                    profile.get("class")
                    or profile.get("occupation")
                    or profile.get("identity")
                ),
                "resources": (
                    stats.get("raw")
                    if isinstance(stats.get("raw"), Mapping)
                    else stats
                ),
                "resource_labels": (
                    stats.get("labels")
                    if isinstance(stats.get("labels"), Mapping)
                    else {}
                ),
                "runtime": runtime,
                "statuses": statuses,
                "inventory": inventory,
                "relationships": rel_rows,
                "turn_position": position_by_user.get(user_id, 0),
                "is_current": bool(user_id and user_id == current_user_id),
                "current_location": _text(runtime.get("current_location")),
                "reputation": _normalize_reputation(runtime.get("reputation")),
            }
        )
    return result



async def _session_npcs(database: Any, session_id: str) -> list[dict[str, Any]]:
    """A18: 副本 NPC 卡片数据（世界包常驻 + 动态 NPC）。"""
    try:
        rows = await database.list_session_characters(session_id)
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, Mapping):
            continue
        profile = item.get("public_profile") or {}
        if not isinstance(profile, Mapping):
            profile = {}
        state = item.get("state") or {}
        if not isinstance(state, Mapping):
            state = {}
        result.append(
            {
                "id": _text(item.get("id")),
                "stable_key": _text(item.get("stable_key")),
                "name": _text(item.get("name")),
                "role_type": _text(item.get("role_type")),
                "identity": _text(
                    profile.get("identity")
                    or profile.get("occupation")
                    or profile.get("class")
                ),
                "organization": _text(
                    profile.get("organization")
                    or profile.get("faction")
                    or profile.get("affiliation")
                ),
                "lifecycle_status": _text(item.get("lifecycle_status")),
                "source": _text(item.get("source")),
                "location": _text(
                    state.get("current_location") or state.get("location")
                ),
                "status": _text(state.get("status") or state.get("state")),
                "public_profile": profile,
                "state": state,
                "last_turn": _int(item.get("last_turn")),
            }
        )
    return result


async def session_dashboard(
    database: Any,
    session_id: str,
) -> dict[str, Any]:
    """单个副本的实时聚合（状态机 / 行动者 / 计时器 / 选项 / 投票 / 事件）。"""
    session = await database.get_session(session_id)
    session = dict(session)
    world_state = session.get("world_state") or {}
    turn = await database.get_turn_status(session_id)
    timers = await session_timers(database, session_id, order="desc")
    choice_set = await database.active_choice_set(session_id)
    vote = await database.active_vote(session_id)
    events = await database.recent_events(session_id, 12)
    providers = await database.list_provider_health()
    # A17：LIVE 仪表盘所需的控制权 / 托管 / 待处理任务 / 阵容轻量投影。
    control = await database.get_control_state(session_id)
    delegations = await database.list_delegations(session_id)
    pending_ops = await database.pending_operations(session_id)
    try:
        roster_rows = await database.list_roster(session_id)
    except Exception:
        roster_rows = []
    roster = [
        {
            "id": _text(item.get("id")),
            "group_user_id": _text(item.get("group_user_id") or item.get("user_id")),
            "character_name": _text(item.get("character_name")),
            "display_name": _text(item.get("display_name")),
            "participation_status": _text(item.get("participation_status")),
        }
        for item in roster_rows
        if isinstance(item, Mapping)
    ]
    active_choice_list = []
    if choice_set and isinstance(choice_set, Mapping):
        # A20: active_choice_set 返回的键是 choices（已归一化），
        # 兼容旧数据仍以 choices_json 为键的形态。
        active_choice_list = _options(
            choice_set.get("choices")
            if isinstance(choice_set.get("choices"), (list, Mapping))
            else choice_set.get("choices_json")
        )
    # A18: 小队 / NPC / 剧情账本 / 场景时钟 / 队伍关系。
    squad = _squad(roster_rows, turn, world_state)
    try:
        return_requests = await database.list_return_requests(session_id)
    except Exception:
        return_requests = []
    try:
        economy = await database.economy_summary(session_id)
    except Exception:
        economy = {"enabled": False, "currencies": [], "wallets": [], "recent": []}
    try:
        ledger = await database.list_story_ledger(session_id)
    except Exception:
        ledger = []
    try:
        clocks = await database.list_scene_clocks(session_id)
    except Exception:
        clocks = []
    npcs = await _session_npcs(database, session_id)
    party_relations = _party_relations(world_state)
    party_inventory = _owner_inventory(
        world_state, ["队伍", "party", "party_supplies", "party_inventory", "team"]
    )
    quest_items = _owner_inventory(world_state, ["quest_items"])
    current_choice = None
    if choice_set and isinstance(choice_set, Mapping):
        participant = choice_set.get("participant") or {}
        current_choice = {
            "id": _text(choice_set.get("id")),
            "participant_id": _text(choice_set.get("participant_id")),
            "round_no": _int(choice_set.get("round_no")),
            "status": _text(choice_set.get("status")),
            "reroll_count": _int(choice_set.get("reroll_count")),
            "selected_key": _text(choice_set.get("selected_key")),
            "flavor_text": _text(choice_set.get("flavor_text")),
            "choices": active_choice_list,
            "participant": {
                "id": _text(participant.get("id")),
                "group_user_id": _text(
                    participant.get("group_user_id")
                    or participant.get("user_id")
                ),
                "character_name": _text(participant.get("character_name")),
                "display_name": _text(participant.get("display_name")),
            }
            if isinstance(participant, Mapping)
            else None,
        }
    return {
        "session": {
            "id": session.get("id"),
            "name": _text(
                session.get("instance_name") or session.get("name")
            ),
            "state": _text(session.get("state"), "closed"),
            "revision": _int(session.get("revision")),
            "turn_no": _int(session.get("turn_no")),
            "input_locked": _int(session.get("input_locked"), 0),
            "group_id": _text(session.get("group_id")),
            # 0.12.0-A3：副本运行卡片信息（群 ID / 回合数 / 进度 / 等待）。
            "waiting_for": await _waiting_for(
                database, session_id, _text(session.get("state"))
            ),
            "world": {
                "name": _text(session.get("world_name")),
                "slug": _text(session.get("world_slug")),
                "revision": _int(session.get("world_revision")),
            },
            # A11：直接返回完整受控世界状态，供前端做全字段可视化；
            # 同时附带 id→名称映射以解析 participants/session_characters 裸 ID。
            "world_state": world_state,
            "id_labels": await _session_id_labels(database, session_id),
        },
        "turn": {
            "round_no": _int(turn.get("round_no")),
            "current_user_id": _text(turn.get("current_user_id")),
            "current_name": _text(turn.get("current_name")),
            "order": [
                {
                    "position": _int(item.get("position")),
                    "user_id": _text(item.get("user_id")),
                    "name": _text(
                        item.get("name")
                        or item.get("character_name")
                        or item.get("display_name")
                    ),
                }
                for item in turn.get("order", [])
                if isinstance(item, Mapping)
            ],
        },
        "timers": [_normalize_timer(item) for item in timers],
        "active_choices": active_choice_list,
        "active_vote": _normalize_vote(vote),
        "current_choice": current_choice,
        "squad": squad,
        "npcs": npcs,
        "ledger": [
            {
                "id": _text(item.get("id")),
                "kind": _text(item.get("kind")),
                "title": _text(item.get("title")),
                "description": _text(item.get("description"))[:200],
                "status": _text(item.get("status")),
                "visibility": _text(item.get("visibility")),
                "updated_at": _text(item.get("updated_at")),
            }
            for item in ledger
            if isinstance(item, Mapping)
        ],
        "clocks": [
            {
                "id": _text(item.get("id")),
                "title": _text(item.get("title")),
                "segments": _int(item.get("segments")),
                "current_value": _int(item.get("current_value")),
                "visibility": _text(item.get("visibility")),
                "status": _text(item.get("status")),
                "trigger_text": _text(item.get("trigger_text")),
            }
            for item in clocks
            if isinstance(item, Mapping)
        ],
        "party_relations": party_relations,
        "party_inventory": party_inventory,
        "quest_items": quest_items,
        "economy": economy if isinstance(economy, Mapping) else {},
        "return_requests": [
            {
                "id": _text(item.get("id")),
                "character_name": _text(item.get("character_name")),
                "display_name": _text(item.get("display_name")),
                "objective": _text(item.get("objective")),
                "status": _text(item.get("status")),
            }
            for item in return_requests
            if isinstance(item, Mapping)
        ],
        "control": control,
        "delegations": delegations,
        "pending_operations": pending_ops,
        "roster": roster,
        "recent_events": [
            {
                "role": _text(item.get("role")),
                "content": _text(item.get("content"))[:300],
                "created_at": _text(item.get("created_at")),
            }
            for item in events
            if isinstance(item, Mapping)
        ],
        "provider_health": [
            {
                "provider_id": _text(item.get("provider_id")),
                "success": _bool(item.get("success")),
                "reason": _text(item.get("reason"))[:120],
            }
            for item in providers
            if isinstance(item, Mapping)
        ],
    }


async def session_timeline(
    database: Any,
    session_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    """事件时间线（回放视图）：事件流 + 操作摘要。"""
    limit = max(5, min(int(limit), 100))
    events = await database.recent_events(session_id, limit)
    operations = await database.list_session_operations(session_id, limit)
    return {
        "session_id": session_id,
        "events": [
            {
                "role": _text(item.get("role")),
                "content": _text(item.get("content"))[:600],
                "created_at": _text(item.get("created_at")),
            }
            for item in events
            if isinstance(item, Mapping)
        ],
        "operations": [
            {
                "phase": _text(item.get("phase")),
                "status": _text(item.get("status")),
                "created_at": _text(item.get("created_at")),
            }
            for item in operations
            if isinstance(item, Mapping)
        ],
    }
