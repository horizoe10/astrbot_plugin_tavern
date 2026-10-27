from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INSTANCE_FORMAT = "astrbot-tavern-instance"
INSTANCE_FORMAT_VERSION = 1
TIMESTAMP_PATTERN = re.compile(r"^\d{14}$")
FILE_OPERATION_ATTEMPTS = 6
FILE_OPERATION_INITIAL_DELAY = 0.05


def _retry_file_operation(
    operation: Callable[[], Any],
    *,
    attempts: int = FILE_OPERATION_ATTEMPTS,
) -> Any:
    """Retry a short-lived Windows sharing violation without hiding failures."""

    delay = FILE_OPERATION_INITIAL_DELAY
    for attempt in range(max(1, int(attempts))):
        try:
            return operation()
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(
                exc,
                "winerror",
                None,
            ) in {5, 32, 33}
            if not retryable or attempt + 1 >= attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
    raise RuntimeError("文件操作重试状态异常")


def replace_with_retry(source: Path, destination: Path) -> None:
    _retry_file_operation(lambda: os.replace(source, destination))


def unlink_with_retry(
    path: Path,
    *,
    missing_ok: bool = True,
    suppress_errors: bool = False,
) -> None:
    try:
        _retry_file_operation(
            lambda: path.unlink(missing_ok=missing_ok)
        )
    except OSError:
        if not suppress_errors:
            raise


def timestamp14(value: str | datetime | None = None) -> str:
    """Return a sortable fourteen-digit timestamp in the server timezone."""

    moment: datetime
    if isinstance(value, datetime):
        moment = value
    elif value:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            # Corrupt imported metadata must not produce a different folder
            # name on every retry. A fixed epoch is safer than "now".
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        moment = datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y%m%d%H%M%S")


def safe_component(value: Any, fallback: str, maximum: int = 64) -> str:
    text = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        str(value or "").strip().casefold(),
    )
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return (text or fallback)[:maximum]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_group_folder(platform_id: str, group_id: str) -> str:
    platform = safe_component(platform_id, "platform", 32)
    digest = hashlib.sha256(
        f"{platform_id}\0{group_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{platform}_g_{digest}"


def stable_story_folder(
    instance_slug: str,
    created_at: str,
    session_id: str,
) -> str:
    slug = safe_component(instance_slug, "story", 52)
    stamp = timestamp14(created_at)
    token_source = str(session_id).removeprefix("session_")
    token = safe_component(token_source, "", 8)
    if len(token) < 6:
        token = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{stamp}_i-{token}"


def next_timestamped_path(
    directory: Path,
    prefix: str,
    suffix: str,
    *,
    stamp: str | None = None,
) -> Path:
    normalized_stamp = stamp or timestamp14()
    if not TIMESTAMP_PATTERN.fullmatch(normalized_stamp):
        raise ValueError("存档时间必须是 YYYYMMDDHHMMSS 十四位数字")
    base = directory / f"{prefix}_{normalized_stamp}{suffix}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = directory / (
            f"{prefix}_{normalized_stamp}_{index:02d}{suffix}"
        )
        if not candidate.exists():
            return candidate
    raise RuntimeError("同一秒内生成的同名存档过多")


class InstanceStorage:
    """Materialize a recoverable database and manifest for each story run.

    The Schema 10 catalog is the transaction coordinator. Every successful
    session mutation refreshes a self-contained, single-session SQLite
    database, so a damaged catalog can be reconstructed from manifests and
    instance databases without relying on one monolithic story file.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        catalog_path: Path,
        connect_catalog: Callable[[], sqlite3.Connection],
        schema_version: int,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.catalog_path = Path(catalog_path)
        self.groups_dir = self.data_dir / "groups"
        self.connect_catalog = connect_catalog
        self.schema_version = int(schema_version)
        self._lock = threading.RLock()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temporary, path)
        finally:
            unlink_with_retry(temporary, suppress_errors=True)

    @classmethod
    def _atomic_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
        cls._atomic_write(
            path,
            (
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        return dict(row) if row else {}

    def _ensure_index(self, session_id: str) -> dict[str, Any]:
        with self.connect_catalog() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT s.*, w.slug AS world_slug, w.name AS world_name,
                           ic.world_revision, ic.world_snapshot_json,
                           ic.phase_meta_json
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN instance_configs ic ON ic.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise LookupError("副本不存在")
                platform_id = str(session["platform_id"])
                group_id = str(session["group_id"])
                registry_id = (
                    "group_"
                    + hashlib.sha256(
                        f"{platform_id}\0{group_id}".encode("utf-8")
                    ).hexdigest()[:24]
                )
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO group_registry(
                        id, platform_id, group_id, remark, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, '', 1, ?, ?)
                    ON CONFLICT(platform_id, group_id) DO NOTHING
                    """,
                    (
                        registry_id,
                        platform_id,
                        group_id,
                        now,
                        now,
                    ),
                )
                group = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (platform_id, group_id),
                ).fetchone()
                storage = connection.execute(
                    """
                    SELECT * FROM story_storage WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                group_folder = stable_group_folder(
                    platform_id,
                    group_id,
                )
                story_folder = stable_story_folder(
                    str(session["instance_slug"]),
                    str(session["created_at"]),
                    session_id,
                )
                expected_relative_path = (
                    Path("groups")
                    / group_folder
                    / "stories"
                    / story_folder
                ).as_posix()
                if not storage:
                    playthrough_no = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM sessions previous
                            WHERE previous.platform_id = ?
                              AND previous.group_id = ?
                              AND previous.world_id = ?
                              AND (
                                previous.created_at < ?
                                OR (
                                  previous.created_at = ?
                                  AND previous.rowid <= (
                                    SELECT current.rowid
                                    FROM sessions current
                                    WHERE current.id = ?
                                  )
                                )
                              )
                            """,
                            (
                                platform_id,
                                group_id,
                                session["world_id"],
                                session["created_at"],
                                session["created_at"],
                                session_id,
                            ),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        INSERT INTO story_storage(
                            session_id, group_registry_id, relative_path,
                            playthrough_no, created_stamp,
                            last_synced_revision, last_checksum,
                            sync_status, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, '', 'pending', '', ?, ?)
                        """,
                        (
                            session_id,
                            group["id"],
                            expected_relative_path,
                            max(1, playthrough_no),
                            timestamp14(str(session["created_at"])),
                            now,
                            now,
                        ),
                    )
                    storage = connection.execute(
                        """
                        SELECT * FROM story_storage WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                elif (
                    str(storage["relative_path"]) != expected_relative_path
                    or str(storage["group_registry_id"]) != str(group["id"])
                ):
                    connection.execute(
                        """
                        UPDATE story_storage SET
                            group_registry_id = ?, relative_path = ?,
                            created_stamp = ?, sync_status = 'pending',
                            last_checksum = '', last_error = '',
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            group["id"],
                            expected_relative_path,
                            timestamp14(str(session["created_at"])),
                            now,
                            session_id,
                        ),
                    )
                    storage = connection.execute(
                        """
                        SELECT * FROM story_storage WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                connection.execute("COMMIT")
                return {
                    "session": self._row_dict(session),
                    "group": self._row_dict(group),
                    "storage": self._row_dict(storage),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _group_payload(
        self,
        group: Mapping[str, Any],
        *,
        story_count: int,
    ) -> dict[str, Any]:
        return {
            "format": "astrbot-tavern-group",
            "format_version": INSTANCE_FORMAT_VERSION,
            "platform_id": str(group.get("platform_id") or ""),
            "group_id": str(group.get("group_id") or ""),
            "remark": str(group.get("remark") or ""),
            "revision": int(group.get("revision") or 1),
            "story_count": int(story_count),
            "created_at": str(group.get("created_at") or ""),
            "updated_at": str(group.get("updated_at") or ""),
        }

    def _write_group_manifest(
        self,
        indexed: Mapping[str, Any],
    ) -> Path:
        group = indexed["group"]
        storage = indexed["storage"]
        story_dir = self.data_dir / str(storage["relative_path"])
        group_dir = story_dir.parent.parent
        with self.connect_catalog() as connection:
            story_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM story_storage
                    WHERE group_registry_id = ?
                    """,
                    (group["id"],),
                ).fetchone()[0]
            )
        group_path = group_dir / "group.json"
        self._atomic_json(
            group_path,
            self._group_payload(group, story_count=story_count),
        )
        return group_path

    @staticmethod
    def _world_snapshot(indexed: Mapping[str, Any]) -> dict[str, Any]:
        raw = indexed["session"].get("world_snapshot_json")
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _manifest_payload(
        self,
        indexed: Mapping[str, Any],
        *,
        checksum: str,
        counts: Mapping[str, int],
    ) -> dict[str, Any]:
        session = indexed["session"]
        group = indexed["group"]
        storage = indexed["storage"]
        world_snapshot = self._world_snapshot(indexed)
        try:
            phase = json.loads(session.get("phase_meta_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            phase = {}
        if not isinstance(phase, Mapping):
            phase = {}
        return {
            "format": INSTANCE_FORMAT,
            "format_version": INSTANCE_FORMAT_VERSION,
            "schema_version": self.schema_version,
            "session": {
                "id": str(session.get("id") or ""),
                "platform_id": str(session.get("platform_id") or ""),
                "group_id": str(session.get("group_id") or ""),
                "instance_slug": str(session.get("instance_slug") or ""),
                "instance_name": str(session.get("instance_name") or ""),
                "state": str(session.get("state") or ""),
                "turn_no": int(session.get("turn_no") or 0),
                "revision": int(session.get("revision") or 0),
                "selected": bool(session.get("selected")),
                "created_at": str(session.get("created_at") or ""),
                "updated_at": str(session.get("updated_at") or ""),
                "playthrough_no": int(storage.get("playthrough_no") or 1),
                "branched_from_session_id": str(
                    phase.get("branched_from_session_id") or ""
                ),
                "branched_from_snapshot_id": str(
                    phase.get("branched_from_snapshot_id") or ""
                ),
            },
            "group": {
                "registry_id": str(group.get("id") or ""),
                "remark": str(group.get("remark") or ""),
                "revision": int(group.get("revision") or 1),
            },
            "world": {
                "id": str(session.get("world_id") or ""),
                "slug": str(
                    world_snapshot.get("slug")
                    or session.get("world_slug")
                    or ""
                ),
                "name": str(
                    world_snapshot.get("name")
                    or session.get("world_name")
                    or ""
                ),
                "revision": int(
                    world_snapshot.get("revision")
                    or session.get("world_revision")
                    or 1
                ),
            },
            "storage": {
                "relative_path": str(storage.get("relative_path") or ""),
                "database": "instance.sqlite3",
                "saves_directory": "saves",
                "backups_directory": "backups",
                "database_sha256": checksum,
                "counts": dict(counts),
            },
        }

    @staticmethod
    def _database_counts(path: Path) -> dict[str, int]:
        result: dict[str, int] = {}
        with closing(sqlite3.connect(path)) as connection:
            for table in (
                "sessions",
                "participants",
                "events",
                "memories",
                "snapshots",
                "session_characters",
                "story_ledger",
                "scene_clocks",
                "rolls",
            ):
                exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table,),
                ).fetchone()
                if exists:
                    result[table] = int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    )
        return result

    @staticmethod
    def _delete_not_in(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        values: Sequence[str],
    ) -> None:
        if values:
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f'DELETE FROM "{table}" '
                f'WHERE "{column}" NOT IN ({placeholders})',
                tuple(values),
            )
        else:
            connection.execute(f'DELETE FROM "{table}"')

    def _prune_instance(
        self,
        path: Path,
        indexed: Mapping[str, Any],
    ) -> None:
        session = indexed["session"]
        session_id = str(session["id"])
        world_id = str(session["world_id"])
        group_registry_id = str(indexed["group"]["id"])
        world_snapshot = self._world_snapshot(indexed)
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM sessions WHERE id <> ?",
                    (session_id,),
                )
                tables = [
                    str(row["name"])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                ]
                columns_by_table: dict[str, set[str]] = {}
                for table in tables:
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            f'PRAGMA table_info("{table}")'
                        ).fetchall()
                    }
                    columns_by_table[table] = columns
                    if "session_id" in columns and table != "sessions":
                        connection.execute(
                            f'DELETE FROM "{table}" '
                            "WHERE session_id <> ?",
                            (session_id,),
                        )

                # 世界级子表必须先于 DELETE FROM worlds 清理：
                # world_snapshots 使用 ON DELETE RESTRICT（database.py），
                # 直接删其它世界会触发 FOREIGN KEY constraint failed，
                # 在克隆/多世界场景下表现为“副本文件同步异常”。
                for table, columns in columns_by_table.items():
                    if (
                        "world_id" in columns
                        and table not in {"worlds", "sessions"}
                    ):
                        connection.execute(
                            f'DELETE FROM "{table}" '
                            "WHERE world_id <> ?",
                            (world_id,),
                        )

                if "group_registry" in tables:
                    connection.execute(
                        "DELETE FROM group_registry WHERE id <> ?",
                        (group_registry_id,),
                    )
                if "story_storage" in tables:
                    connection.execute(
                        "DELETE FROM story_storage WHERE session_id <> ?",
                        (session_id,),
                    )

                used_card_ids = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT character_card_id FROM participants
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        UNION
                        SELECT character_card_id FROM character_runtime_states
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        UNION
                        SELECT character_card_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        """,
                        (session_id, session_id, session_id),
                    ).fetchall()
                }
                used_version_ids = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT character_version_id FROM participants
                        WHERE session_id = ?
                          AND character_version_id IS NOT NULL
                          AND character_version_id <> ''
                        UNION
                        SELECT base_version_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND base_version_id IS NOT NULL
                          AND base_version_id <> ''
                        UNION
                        SELECT candidate_version_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND candidate_version_id IS NOT NULL
                          AND candidate_version_id <> ''
                        """,
                        (session_id, session_id, session_id),
                    ).fetchall()
                }
                self._delete_not_in(
                    connection,
                    "character_card_versions",
                    "id",
                    sorted(used_version_ids),
                )
                self._delete_not_in(
                    connection,
                    "character_cards",
                    "id",
                    sorted(used_card_ids),
                )

                connection.execute(
                    "DELETE FROM worlds WHERE id <> ?",
                    (world_id,),
                )
                if world_snapshot:
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            revision = ?
                        WHERE id = ?
                        """,
                        (
                            str(
                                world_snapshot.get("slug")
                                or session.get("world_slug")
                                or "instance-world"
                            ),
                            str(
                                world_snapshot.get("name")
                                or session.get("world_name")
                                or "副本世界"
                            ),
                            str(world_snapshot.get("description") or ""),
                            str(world_snapshot.get("system_prompt") or ""),
                            json.dumps(
                                world_snapshot.get("rules") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            str(world_snapshot.get("opening_scene") or ""),
                            json.dumps(
                                world_snapshot.get("initial_state") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            int(world_snapshot.get("revision") or 1),
                            world_id,
                        ),
                    )

                for table in (
                    "provider_health",
                    "configuration_revisions",
                ):
                    if table in tables:
                        connection.execute(f'DELETE FROM "{table}"')
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('storage_kind', 'instance')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('instance_session_id', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (session_id,),
                )
                violations = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise RuntimeError("副本数据库外键校验失败")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("VACUUM")
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if quick != "ok":
                raise RuntimeError(f"副本数据库完整性校验失败：{quick}")

    def _record_sync_error(self, session_id: str, error: Exception) -> None:
        try:
            with self.connect_catalog() as connection:
                connection.execute(
                    """
                    UPDATE story_storage SET
                        sync_status = 'error', last_error = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        str(error)[:1000],
                        datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        session_id,
                    ),
                )
        except sqlite3.DatabaseError:
            return

    def sync_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            indexed = self._ensure_index(session_id)
            story_dir = (
                self.data_dir
                / str(indexed["storage"]["relative_path"])
            )
            story_dir.mkdir(parents=True, exist_ok=True)
            (story_dir / "saves").mkdir(exist_ok=True)
            (story_dir / "backups").mkdir(exist_ok=True)
            group_path = self._write_group_manifest(indexed)
            destination = story_dir / "instance.sqlite3"
            temporary = story_dir / (
                f".instance.{uuid.uuid4().hex}.sqlite3"
            )
            try:
                with closing(
                    sqlite3.connect(self.catalog_path)
                ) as source:
                    with closing(sqlite3.connect(temporary)) as target:
                        source.backup(target)
                self._prune_instance(temporary, indexed)
                for suffix in ("-wal", "-shm"):
                    unlink_with_retry(
                        destination.with_name(
                            destination.name + suffix
                        )
                    )
                replace_with_retry(temporary, destination)
                checksum = file_sha256(destination)
                counts = self._database_counts(destination)
                refreshed = self._ensure_index(session_id)
                manifest = self._manifest_payload(
                    refreshed,
                    checksum=checksum,
                    counts=counts,
                )
                manifest_path = story_dir / "manifest.json"
                self._atomic_json(manifest_path, manifest)
                now = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                with self.connect_catalog() as connection:
                    connection.execute(
                        """
                        UPDATE story_storage SET
                            last_synced_revision = ?,
                            last_checksum = ?,
                            sync_status = 'ready',
                            last_error = '',
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            int(refreshed["session"].get("revision") or 0),
                            checksum,
                            now,
                            session_id,
                        ),
                    )
                return {
                    "session_id": session_id,
                    "relative_path": str(
                        refreshed["storage"]["relative_path"]
                    ),
                    "database": destination,
                    "manifest": manifest_path,
                    "group_manifest": group_path,
                    "checksum": checksum,
                    "counts": counts,
                }
            except Exception as exc:
                unlink_with_retry(temporary, suppress_errors=True)
                for suffix in ("-wal", "-shm"):
                    unlink_with_retry(
                        temporary.with_name(temporary.name + suffix),
                        suppress_errors=True,
                    )
                self._record_sync_error(session_id, exc)
                raise

    def _archive_synced(
        self,
        session_id: str,
        *,
        kind: str,
        reason: str,
    ) -> Path:
        if kind not in {"save", "backup"}:
            raise ValueError("独立存档类型必须为 save 或 backup")
        indexed = self._ensure_index(session_id)
        session = indexed["session"]
        story_dir = (
            self.data_dir / str(indexed["storage"]["relative_path"])
        )
        destination_dir = story_dir / (
            "saves" if kind == "save" else "backups"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        slug = safe_component(
            session.get("instance_slug"),
            "story",
            maximum=52,
        )
        target = next_timestamped_path(
            destination_dir,
            f"{kind}_{slug}",
            ".zip",
        )
        database_path = story_dir / "instance.sqlite3"
        manifest_path = story_dir / "manifest.json"
        group_path = story_dir.parent.parent / "group.json"
        if not database_path.exists() or not manifest_path.exists():
            raise RuntimeError("副本实时数据库或清单不存在")
        manifest_payload = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        manifest_payload["archive"] = {
            "kind": kind,
            "reason": str(reason or ""),
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "filename": target.name,
        }
        manifest_bytes = (
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        group_bytes = group_path.read_bytes()
        checksums = (
            f"{file_sha256(database_path)}  instance.sqlite3\n"
            f"{bytes_sha256(manifest_bytes)}  manifest.json\n"
            f"{bytes_sha256(group_bytes)}  group_snapshot.json\n"
        ).encode("utf-8")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(database_path, "instance.sqlite3")
                archive.writestr("manifest.json", manifest_bytes)
                archive.writestr("group_snapshot.json", group_bytes)
                archive.writestr("checksum.sha256", checksums)
            replace_with_retry(temporary, target)
        finally:
            unlink_with_retry(temporary, suppress_errors=True)
        if kind == "backup":
            archives = sorted(
                destination_dir.glob(f"backup_{slug}_*.zip"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for stale in archives[20:]:
                stale.unlink(missing_ok=True)
        return target

    def create_archive(
        self,
        session_id: str,
        *,
        kind: str,
        reason: str,
        refresh: bool = True,
    ) -> Path:
        with self._lock:
            if refresh:
                self.sync_session(session_id)
            return self._archive_synced(
                session_id,
                kind=kind,
                reason=reason,
            )

    def bootstrap(self) -> list[dict[str, Any]]:
        with self.connect_catalog() as connection:
            session_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM sessions ORDER BY created_at, id"
                ).fetchall()
            ]
        results: list[dict[str, Any]] = []
        for session_id in session_ids:
            indexed = self._ensure_index(session_id)
            story_dir = (
                self.data_dir
                / str(indexed["storage"]["relative_path"])
            )
            database_path = story_dir / "instance.sqlite3"
            manifest_path = story_dir / "manifest.json"
            expected = str(
                indexed["storage"].get("last_checksum") or ""
            )
            # A14（审计 #14）：副本修订号未变化时跳过校验和读取（快路径）。
            last_revision = int(indexed["storage"].get("last_synced_revision") or 0)
            current_revision = int(indexed["session"].get("revision") or 0)
            if (
                last_revision >= current_revision
                and indexed["storage"].get("sync_status") == "ready"
                and expected
                and database_path.exists()
                and manifest_path.exists()
                and file_sha256(database_path) == expected
            ):
                results.append(
                    {
                        "session_id": session_id,
                        "relative_path": str(
                            indexed["storage"]["relative_path"]
                        ),
                        "database": database_path,
                        "manifest": manifest_path,
                        "checksum": expected,
                        "skipped": True,
                    }
                )
                continue
            try:
                result = self.sync_session(session_id)
                results.append(result)
            except (sqlite3.DatabaseError, OSError) as exc:
                # 单个副本的存储同步失败不应阻断插件整体加载：
                # 记录错误并继续，控制台「群会话 / 副本实时仪表盘」会展示
                # storage_sync_status=error 与 last_error，便于后续修复。
                self._record_sync_error(session_id, exc)
                results.append(
                    {
                        "session_id": session_id,
                        "error": str(exc)[:1000],
                    }
                )
        return results

    def sync_group(self, platform_id: str, group_id: str) -> Path | None:
        with self.connect_catalog() as connection:
            row = connection.execute(
                """
                SELECT ss.session_id
                FROM story_storage ss
                JOIN group_registry gr ON gr.id = ss.group_registry_id
                WHERE gr.platform_id = ? AND gr.group_id = ?
                ORDER BY ss.created_at LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        if not row:
            return None
        indexed = self._ensure_index(str(row[0]))
        return self._write_group_manifest(indexed)

    def storage_info(self, session_id: str) -> dict[str, Any]:
        indexed = self._ensure_index(session_id)
        storage = dict(indexed["storage"])
        story_dir = self.data_dir / str(storage["relative_path"])
        storage.update(
            {
                "database_exists": (story_dir / "instance.sqlite3").exists(),
                "manifest_exists": (story_dir / "manifest.json").exists(),
                "save_files": self.list_archives(session_id, kind="save"),
                "backup_files": self.list_archives(
                    session_id,
                    kind="backup",
                ),
            }
        )
        return storage

    def trash_relative_path(
        self,
        relative_path: str,
        *,
        label: str,
    ) -> Path | None:
        """Move a deleted story tree to a recoverable local trash folder."""

        relative = Path(str(relative_path or ""))
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[0] != "groups"
        ):
            raise ValueError("副本存储路径无效，已拒绝移动")
        source = self.data_dir / relative
        if not source.exists():
            return None
        trash_dir = self.data_dir / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        safe_label = safe_component(label, "story", maximum=52)
        target = next_timestamped_path(
            trash_dir,
            f"deleted_{safe_label}",
            "",
        )
        replace_with_retry(source, target)
        return target

    def trash_archive(
        self,
        session_id: str,
        *,
        kind: str,
        filename: str,
    ) -> dict[str, Any]:
        """Move one independent save archive to the story-local trash."""

        if kind != "save":
            raise ValueError("只能手动删除独立命名存档")
        safe_name = Path(str(filename or "")).name
        if (
            safe_name != str(filename or "")
            or not safe_name.startswith("save_")
            or not safe_name.endswith(".zip")
        ):
            raise ValueError("独立存档文件名无效")
        indexed = self._ensure_index(session_id)
        story_dir = self.data_dir / str(
            indexed["storage"]["relative_path"]
        )
        source = story_dir / "saves" / safe_name
        if not source.is_file():
            raise FileNotFoundError("独立存档文件不存在")
        try:
            with zipfile.ZipFile(source, "r") as archive:
                manifest = json.loads(
                    archive.read("manifest.json").decode("utf-8")
                )
        except (
            KeyError,
            OSError,
            UnicodeDecodeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise ValueError("独立存档无法验证，已拒绝删除") from exc
        reason = str(
            (manifest.get("archive") or {}).get("reason") or ""
        )
        if "最终" in reason:
            raise ValueError("最终保护存档不能手动删除")
        trash_dir = story_dir / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = next_timestamped_path(
            trash_dir,
            f"deleted_{safe_component(source.stem, 'save', maximum=80)}",
            ".zip",
        )
        replace_with_retry(source, target)
        return {
            "session_id": session_id,
            "filename": safe_name,
            "reason": reason,
            "trash_path": str(target),
        }

    def list_archives(
        self,
        session_id: str,
        *,
        kind: str,
    ) -> list[dict[str, Any]]:
        if kind not in {"save", "backup"}:
            raise ValueError("独立存档类型必须为 save 或 backup")
        indexed = self._ensure_index(session_id)
        story_dir = self.data_dir / str(
            indexed["storage"]["relative_path"]
        )
        directory = story_dir / (
            "saves" if kind == "save" else "backups"
        )
        if not directory.exists():
            return []
        items = sorted(
            directory.glob(f"{kind}_*.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "filename": item.name,
                "kind": kind,
                "size": item.stat().st_size,
                "created_at": datetime.fromtimestamp(
                    item.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(timespec="seconds"),
            }
            for item in items
        ]

    def verify_instance(self, session_id: str) -> dict[str, Any]:
        indexed = self._ensure_index(session_id)
        path = (
            self.data_dir
            / str(indexed["storage"]["relative_path"])
            / "instance.sqlite3"
        )
        if not path.exists():
            return {"ok": False, "reason": "instance.sqlite3 不存在"}
        with closing(sqlite3.connect(path)) as connection:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            sessions = connection.execute(
                "SELECT id FROM sessions"
            ).fetchall()
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        expected_checksum = str(
            indexed["storage"].get("last_checksum") or ""
        )
        checksum = file_sha256(path)
        return {
            "ok": (
                quick == "ok"
                and not foreign_keys
                and [str(row[0]) for row in sessions] == [session_id]
                and (not expected_checksum or checksum == expected_checksum)
            ),
            "quick_check": quick,
            "foreign_key_errors": len(foreign_keys),
            "sessions": [str(row[0]) for row in sessions],
            "checksum": checksum,
            "expected_checksum": expected_checksum,
        }

    def discover_manifests(self) -> list[dict[str, Any]]:
        if not self.groups_dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(
            self.groups_dir.glob("*/stories/*/manifest.json")
        ):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("format") == INSTANCE_FORMAT
            ):
                results.append(
                    {
                        "relative_path": path.relative_to(
                            self.data_dir
                        ).as_posix(),
                        "manifest": dict(payload),
                    }
                )
        return results
