from __future__ import annotations

from .characters_support import *


class CharacterCardsTransactionsRepositoryMixin:
    def _restart_card_draft(self, private_origin: str) -> dict[str, Any]:
        private_origin = clean_text(private_origin, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, ic.world_snapshot_json,
                           ic.time_rules_json, ic.world_revision
                    FROM participants pt
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND s.state <> 'finished'
                    ORDER BY pt.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有可重新开始的建卡席位"
                    )
                if str(row["character_card_id"] or ""):
                    raise ValueError(
                        "正式角色不能使用重新建卡；请使用角色卡修改流程"
                    )
                if row["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    raise ValueError(
                        "该席位已经放弃；请回群使用 /团 加入 重新申请"
                    )
                now = utc_now()
                world = json_load(row["world_snapshot_json"], {})
                template = card_template(world)
                time_rules = normalize_time_rules(
                    json_load(row["time_rules_json"], {})
                )
                generation = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(generation), 0) + 1
                        FROM character_card_drafts
                        WHERE participant_id = ?
                        """,
                        (row["id"],),
                    ).fetchone()[0]
                )
                draft_id = new_id("draft")
                draft_expires_at = deadline_after(
                    time_rules["card_draft_ttl_seconds"]
                )
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'superseded',
                        cancel_reason = 'player_restarted',
                        superseded_by = ?, updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (draft_id, now, row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO character_card_drafts(
                        id, participant_id, generation, template_version,
                        template_revision, world_revision, fields_json,
                        current_step, status, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '{}', 0,
                              'active', ?, ?, ?)
                    """,
                    (
                        draft_id,
                        row["id"],
                        generation,
                        template["version"],
                        f"actor@{template['version']}",
                        int(row["world_revision"] or 1),
                        draft_expires_at,
                        now,
                        now,
                    ),
                )
                # rc12 native bug：`entry_beat` 是未实现特性残留的未定义变量，
                # 整段条件体目前无法执行。临时把 `entry_beat is not None` 这一行
                # 永久置 False，等上游真正实现"重启时插入入场幕间"再启用。
                if False and entry_beat is not None:
                    append_event(
                        connection,
                        session_id=row["session_id"],
                        turn_no=(
                            row["turn_no"] if "turn_no" in row.keys() else 0
                        ),
                        role="system",
                        actor_id="system",
                        actor_name="入场幕间",
                        content=entry_beat["text"],
                        meta={
                            "kind": "entry_interlude",
                            "participant_id": row["id"],
                            "visibility": entry_beat["visibility"],
                            "text_id": entry_beat["text_id"],
                        },
                        created_at=now,
                    )
                connection.execute(
                    """
                    UPDATE participants SET
                        card_status = 'draft', ready = 0,
                        participation_status = 'reserved',
                        exit_reason = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._create_timer(
                    connection,
                    session_id=row["session_id"],
                    participant_id=row["id"],
                    timer_type="card_completion",
                    timeout_seconds=time_rules[
                        "card_completion_timeout_seconds"
                    ],
                    reminder_seconds=None,
                    action={
                        "timeout_action": time_rules["card_timeout_action"],
                        "draft_generation": generation,
                    },
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.restart",
                    row["id"],
                    {"generation": generation},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": {},
                    "template": template,
                    "current_step": 0,
                    "complete": False,
                    "draft_generation": generation,
                    "world": world,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
