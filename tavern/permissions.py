"""A16：统一权限判定助手。

所有「托管 / 代选 / 管理员强制 / 人工 DM / 资产调整 / 投票修改 / 行动顺序修改 /
状态修复 / 回滚」入口都应先经过这里，避免各功能内部各写一套管理员判断。
"""
from __future__ import annotations

from typing import Any


def is_plugin_admin(config: Any, user_id: str) -> bool:
    """插件全局管理员（配置 security.admin_ids）。"""
    try:
        return str(user_id) in config.admin_ids
    except Exception:
        return False


def is_session_dm(control: Any, user_id: str) -> bool:
    """当前副本的活动人工 DM。"""
    try:
        return (
            bool(control)
            and str(control.get("mode") or "") == "dm"
            and str(control.get("active_dm_user_id") or "") == str(user_id)
        )
    except Exception:
        return False


async def can_manage_dm(
    database: Any,
    config: Any,
    session_id: str,
    control: Any,
    user_id: str,
) -> bool:
    """可管理人工 DM / 会话控制：全局管理员 或 活动 DM 或 副本 host。"""
    if is_plugin_admin(config, user_id):
        return True
    if is_session_dm(control, user_id):
        return True
    try:
        grants = await database.list_permission_grants(session_id)
    except Exception:
        grants = []
    return any(
        str(item.get("user_id") or "") == str(user_id)
        and str(item.get("role") or "") == "host"
        for item in (grants or [])
    )


async def can_adjust_economy(
    database: Any,
    config: Any,
    session_id: str,
    control: Any,
    user_id: str,
) -> bool:
    """资产调整权限：管理员或活动 DM 或副本 host/mod 均可。"""
    if await can_manage_dm(database, config, session_id, control, user_id):
        return True
    try:
        grants = await database.list_permission_grants(session_id)
    except Exception:
        grants = []
    return any(
        str(item.get("user_id") or "") == str(user_id)
        and str(item.get("role") or "") in {"host", "moderator"}
        for item in (grants or [])
    )
