"""网页建卡（/cw）在草稿上的内部状态读写。

网页建卡的令牌校验依据、AI 配额与"网页激活中"标记都以内部键形式存放在
``character_card_drafts.fields_json``，与建卡数据同事务持久化；本模块提供
受限键白的合并写入与活跃草稿计数，供独立面板的 /cw 接口与聊天侧静默判断
共同使用。键白名单在本模块内收口，其他内部键不允许经此通道改写。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .characters_support import *

# 网页建卡专用的草稿内部键（与 card_web_wizard 模块共用常量值）。
WEB_LINK_KEY = "_web_link"
WEB_SESSION_KEY = "_web_session"
WEB_ACTIVE_KEY = "_web_active_until"
AI_QUOTA_KEY = "_ai_quota"

_WEB_STATE_KEYS = frozenset(
    {WEB_LINK_KEY, WEB_SESSION_KEY, WEB_ACTIVE_KEY, AI_QUOTA_KEY}
)


class CharacterWebStateRepositoryMixin:
    """网页建卡内部键的受限读写（不改数据库结构）。"""

    async def set_card_web_state(
        self,
        private_origin: str,
        patch: Mapping[str, Any],
    ) -> None:
        return await self._run(
            self._set_card_web_state,
            private_origin,
            dict(patch or {}),
        )

    def _set_card_web_state(
        self,
        private_origin: str,
        patch: dict[str, Any],
    ) -> None:
        unknown = sorted(str(key) for key in patch if key not in _WEB_STATE_KEYS)
        if unknown:
            raise ValueError(f"网页建卡状态键不受支持：{'、'.join(unknown)}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT d.id, d.fields_json
                    FROM character_card_drafts d
                    JOIN participants pt ON pt.id = d.participant_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
                fields = json_load(row["fields_json"], {})
                fields = dict(fields) if isinstance(fields, Mapping) else {}
                for key, value in patch.items():
                    if value is None:
                        fields.pop(key, None)
                    else:
                        fields[key] = value
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET fields_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(fields), utc_now(), row["id"]),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def count_active_card_drafts(self) -> int:
        return await self._run(self._count_active_card_drafts)

    def _count_active_card_drafts(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM character_card_drafts
                WHERE status = 'active'
                """
            ).fetchone()
            return max(0, int(row["total"] if row else 0))
