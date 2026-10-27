"""A15：完整备份导出与自动备份调度（复用控制台导出逻辑）。

``build_backup_archive`` 生成与控制台「备份导出」完全一致的 ZIP：
``bundle.json``（Schema 数据）+ ``catalog.sqlite3``（物理库）+ 各群独立存档
目录 + ``checksum.sha256``。``prune_backups`` 保留最近 N 份自动备份。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .storage import (
    file_sha256,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)

BACKUP_PREFIX = "backup_tavern"


def _write_archive(
    *,
    data_dir: Path,
    database_path: Path,
    bundle: dict[str, Any],
    export_dir: Path,
    prefix: str,
) -> Path:
    """Write a full backup ZIP to ``export_dir`` and return its path."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = next_timestamped_path(export_dir, prefix, ".zip")
    temporary = path.with_name(f".{path.name}.{uuid4_hex()}.tmp")
    catalog_copy = export_dir / f".catalog.{uuid4_hex()}.sqlite3"
    bundle_bytes = (
        json.dumps(
            bundle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    checksum_lines = [
        (hashlib.sha256(bundle_bytes).hexdigest(), "bundle.json")
    ]
    try:
        with closing(sqlite3.connect(str(database_path))) as source:
            with closing(sqlite3.connect(catalog_copy)) as target:
                source.backup(target)
        checksum_lines.append(
            (file_sha256(catalog_copy), "catalog.sqlite3")
        )
        group_files: list[tuple[Path, str]] = []
        groups_dir = data_dir / "groups"
        if groups_dir.exists():
            for item in sorted(groups_dir.rglob("*")):
                if (
                    not item.is_file()
                    or item.is_symlink()
                    or item.name.endswith(("-wal", "-shm", ".tmp"))
                    or item.name.startswith(".")
                ):
                    continue
                archive_name = item.relative_to(data_dir).as_posix()
                group_files.append((item, archive_name))
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr("bundle.json", bundle_bytes)
            archive.write(catalog_copy, "catalog.sqlite3")
            for item, archive_name in group_files:
                digest = hashlib.sha256()
                with item.open("rb") as source, archive.open(
                    archive_name,
                    "w",
                    force_zip64=True,
                ) as output:
                    for chunk in iter(
                        lambda: source.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(chunk)
                        output.write(chunk)
                checksum_lines.append(
                    (digest.hexdigest(), archive_name)
                )
            archive.writestr(
                "checksum.sha256",
                "".join(
                    f"{digest}  {name}\n"
                    for digest, name in checksum_lines
                ).encode("utf-8"),
            )
        replace_with_retry(temporary, path)
    finally:
        unlink_with_retry(temporary, suppress_errors=True)
        unlink_with_retry(catalog_copy, suppress_errors=True)
    return path


async def build_backup_archive(
    *,
    data_dir: Path,
    database: Any,
    export_dir: Path | None = None,
    prefix: str = BACKUP_PREFIX,
) -> Path:
    """异步构建完整备份 ZIP（bundle 导出在事件循环内，文件写入在线程池）。"""
    bundle = await database.export_bundle()
    target_dir = Path(export_dir or Path(data_dir) / "exports")
    return await asyncio.to_thread(
        _write_archive,
        data_dir=Path(data_dir),
        database_path=Path(database.path),
        bundle=bundle,
        export_dir=target_dir,
        prefix=prefix,
    )


def prune_backups(export_dir: Path, keep_count: int) -> list[Path]:
    """删除最早的自动备份，仅保留最近 ``keep_count`` 份。"""
    export_dir = Path(export_dir)
    if not export_dir.exists():
        return []
    keep = max(1, int(keep_count or 1))
    candidates = sorted(
        (
            path
            for path in export_dir.glob(f"{BACKUP_PREFIX}_*.zip")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for stale in candidates[keep:]:
        unlink_with_retry(stale, suppress_errors=True)
        removed.append(stale)
    return removed


def uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex
