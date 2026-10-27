"""Current-schema derived rows; no historical migration support."""

from ..database_support import *


class CurrentStateRepositoryMixin:
    def _initialize_current_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Idempotently ensure current-schema runtime rows for sessions."""

        now = utc_now()
        sessions = connection.execute(
            """
            SELECT s.id, s.turn_no, s.state, s.world_id,
                   ic.world_snapshot_json
            FROM sessions s
            LEFT JOIN instance_configs ic ON ic.session_id = s.id
            """
        ).fetchall()
        for session in sessions:
            world = json_load(session["world_snapshot_json"], {})
            if not isinstance(world, Mapping) or not world:
                world_row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (session["world_id"],),
                ).fetchone()
                if world_row:
                    world = {
                        "id": world_row["id"],
                        "rules": json_load(world_row["rules_json"], {}),
                        "initial_state": json_load(
                            world_row["initial_state_json"],
                            {},
                        ),
                    }
                else:
                    world = {}
            modules = world_session_modules(world)
            connection.execute(
                """
                INSERT INTO session_rule_states(
                    session_id, progress_json, content_boundaries_json,
                    npc_policy_json, context_budget_json, dice_rules_json,
                    recovery_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    session["id"],
                    json_dump(modules["progress"]),
                    json_dump(modules["content_boundaries"]),
                    json_dump(modules["npc_policy"]),
                    json_dump(modules["context_budget"]),
                    json_dump(modules["dice_rules"]),
                    json_dump(modules["recovery"]),
                    now,
                    now,
                ),
            )

            preset_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order, created_at
                """,
                (session["world_id"],),
            ).fetchall()
            for preset in preset_rows:
                stable_key = f"world:{preset['id']}"
                character_id = new_id("snpc")
                connection.execute(
                    """
                    INSERT INTO session_characters(
                        id, session_id, stable_key, name, aliases_json,
                        role_type, public_profile_json, known_facts_json,
                        misconceptions_json, source, review_status,
                        lifecycle_status, persistent, first_turn, last_turn,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', ?, ?, '[]', '[]',
                              'world_preset', 'approved', 'active', 1,
                              0, ?, 1, ?, ?)
                    ON CONFLICT(session_id, stable_key) DO NOTHING
                    """,
                    (
                        character_id,
                        session["id"],
                        stable_key,
                        preset["name"],
                        preset["role"],
                        preset["profile_json"],
                        session["turn_no"],
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM session_characters
                    WHERE session_id = ? AND stable_key = ?
                    """,
                    (session["id"], stable_key),
                ).fetchone()
                if row:
                    profile = json_load(preset["profile_json"], {})
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO NOTHING
                        """,
                        (
                            row["id"],
                            json_dump(
                                {
                                    "location": profile.get("location", ""),
                                    "faction": profile.get("faction", ""),
                                    "status": "active",
                                }
                            ),
                            now,
                        ),
                    )

            if session["state"] == SESSION_FINISHED:
                archived = connection.execute(
                    "SELECT 1 FROM session_archives WHERE session_id = ?",
                    (session["id"],),
                ).fetchone()
                if not archived:
                    full_session = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (session["id"],),
                    ).fetchone()
                    latest_snapshot = connection.execute(
                        """
                        SELECT id FROM snapshots
                        WHERE session_id = ?
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        (session["id"],),
                    ).fetchone()
                    final_snapshot_id = (
                        str(latest_snapshot["id"])
                        if latest_snapshot
                        else self._insert_snapshot(
                            connection,
                            full_session,
                            f"final-migrated-{str(session['id'])[-8:]}",
                            "final",
                            "system",
                            replace=False,
                        )
                    )
                    connection.execute(
                        """
                        INSERT INTO session_archives(
                            session_id, termination_type, reason,
                            final_snapshot_id, ended_by, ended_at, readonly
                        ) VALUES (?, 'completed', ?, ?, 'system', ?, 1)
                        """,
                        (
                            session["id"],
                            "由 v0.4 finished 状态迁移为永久归档",
                            final_snapshot_id,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE sessions SET selected = 0
                        WHERE id = ?
                        """,
                        (session["id"],),
                    )

        runtime_rows = connection.execute(
            "SELECT * FROM character_runtime_states"
        ).fetchall()
        for runtime in runtime_rows:
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            defaults = initial_character_runtime_state()
            changed = False
            for key, value in defaults.items():
                if key not in state:
                    state[key] = value
                    changed = True
            if changed:
                connection.execute(
                    """
                    UPDATE character_runtime_states
                    SET state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )

        connection.execute(
            """
            INSERT INTO memory_governance(
                memory_id, visibility, locked, pinned, invalidated,
                supersedes_id, conflict_status, note, updated_by, updated_at
            )
            SELECT id,
                   CASE WHEN scope = 'player' THEN 'private' ELSE 'public' END,
                   0, 0, 0, '', 'clear', '', 'system', ?
            FROM memories
            WHERE id NOT IN (SELECT memory_id FROM memory_governance)
            """,
            (now,),
        )

