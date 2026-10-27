from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _lines(events: Sequence[Mapping[str, Any]], limit: int) -> list[str]:
    return [
        f"· {str(item.get('content') or '').strip()}"
        for item in events
        if item.get("role") in {"narrator", "system"}
        and str(item.get("content") or "").strip()
    ][-limit:]


async def build_recap(
    database: Any,
    session: Mapping[str, Any],
    user_id: str,
    scope: str = "",
) -> str:
    scope = str(scope or "").strip()
    event_limit = {
        "最近一轮": 4,
        "最近一章": 30,
        "请假摘要": 50,
    }.get(scope, 16)
    events = await database.recent_events(str(session["id"]), event_limit)
    title = f"【故事回顾 · {scope or '当前局势'}】{session['instance_name']}"
    if scope in {"任务线索", "当前任务与线索"}:
        ledger = await database.list_story_ledger(str(session["id"]))
        active = [
            item
            for item in ledger
            if str(item.get("status") or "") not in {"completed", "failed", "archived"}
        ]
        body = [
            f"· {item.get('title') or '未命名线索'}：{item.get('description') or '暂无说明'}"
            for item in active[:12]
        ]
    elif scope in {"NPC关系", "人物关系"}:
        characters = await database.list_session_characters(
            str(session["id"]), include_archived=False
        )
        body = [
            f"· {item.get('name') or item.get('stable_key')}："
            f"{(item.get('public_profile') or {}).get('summary') or item.get('role_type') or '关系待确认'}"
            for item in characters[:15]
        ]
    elif scope in {"我的经历", "我的角色经历"}:
        roster = await database.list_roster(str(session["id"]))
        me = next(
            (x for x in roster if str(x.get("group_user_id") or "") == str(user_id)),
            None,
        )
        name = (
            (me or {}).get("character_name")
            or (me or {}).get("display_name")
            or "你的角色"
        )
        related = [
            item
            for item in events
            if name in str(item.get("content") or "")
            or str(item.get("actor_id") or "") == str(user_id)
        ]
        body = _lines(related or events, 10)
    else:
        body = _lines(events, 12 if scope == "请假摘要" else 6)
    state = session.get("world_state") or {}
    header = [
        title,
        f"地点：{state.get('location', '未记录')}",
        f"局势：{state.get('scene_summary', '暂无')}",
        "",
    ]
    return "\n".join(header + (body or ["暂无对应记录。"]))

