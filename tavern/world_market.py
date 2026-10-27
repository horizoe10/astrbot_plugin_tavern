"""世界包社区注册表 / 市场（v0.12.0 起；0.12.0-A3 加入远程拉取）。

本地优先的社区内容分发能力：
- 扫描插件自带的 ``worlds/`` 与 ``examples/``、``templates/`` 世界包；
- 提取规范化元数据（slug/名称/描述/协议版本/内容版本/标签）；
- 支持按关键字搜索与按包标识拉取完整内容；
- 0.12.0-A3 起支持**远程注册表拉取**（GitHub raw 等静态源）：
  - 仅 HTTPS + 主机白名单（防 SSRF）；
  - 清单与包内容按 URL 做 TTL 缓存（缓解 GitHub raw 限流）；
  - 包内容做体积上限与可选 SHA256 校验；
  - 导入始终复用控制台既有 ``worlds/import`` 通道（体检 → 冲突检查 → 落库）；
  - 远程市场默认关闭，显式配置 ``world_market.enabled`` 才生效。

对外函数：
- ``scan_entries(root)``：扫描并返回本地市场条目列表。
- ``search_entries(entries, query)``：按关键字过滤。
- ``fetch_entry(root, package_key)``：按包标识返回本地 (条目, 完整内容)。
- ``validate_remote_url(url, allowed_hosts)``：校验 HTTPS 与主机白名单。
- ``fetch_remote_manifest(client, manifest_url, allowed_hosts, max_bytes, ttl)``：
  拉取并缓存远程清单（返回条目列表）。
- ``fetch_remote_package(client, entry, allowed_hosts, max_bytes, verify_sha256)``：
  拉取远程世界包内容（体积上限 + SHA256 校验）。
- ``clear_remote_cache()``：清空远程缓存。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# 0.12.0-A3：远程市场默认主机白名单。
DEFAULT_ALLOWED_HOSTS = (
    "raw.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)

# 远程缓存：url → (fetched_at, payload)
_REMOTE_CACHE: dict[str, tuple[float, Any]] = {}

# 扫描目录（相对插件根）：内置世界 + 示例 + 官方模板。
_SCAN_GLOBS = (
    "worlds/*.json",
    "examples/world-schema-v2/*.json",
    "templates/world-package-*.json",
)

_TAG_KEYWORDS = (
    ("d20", ("d20", "检定", "dice", "掷骰", "跑团", "trpg", "d&d")),
    ("合作", ("合作", "协作", "collective", "队伍", "全队")),
    ("剧情", ("剧情", "叙事", "冒险", "rpg", "故事", "战役")),
    ("示例", ("示例", "example", "模板", "template", "最小")),
    ("新手", ("新手", "入门", "starter", "beginner")),
)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tags(name: str, description: str) -> list[str]:
    haystack = f"{name} {description}".casefold()
    return [
        tag
        for tag, keywords in _TAG_KEYWORDS
        if any(keyword in haystack for keyword in keywords)
    ]


def _schema_version(content: dict[str, Any]) -> int:
    try:
        return int(content.get("world_schema_version") or 0)
    except (TypeError, ValueError):
        return 0


def _content_version(content: dict[str, Any]) -> str:
    return _text(content.get("world_content_version") or content.get("version"))


def scan_entries(root: Path) -> list[dict[str, Any]]:
    """扫描插件根目录下的世界包，返回市场条目列表（不含完整内容）。"""
    root = Path(root)
    entries: list[dict[str, Any]] = []
    for glob in _SCAN_GLOBS:
        for path in sorted(root.glob(glob)):
            if not path.is_file():
                continue
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if not isinstance(content, dict) or not content.get("slug"):
                continue
            name = _text(content.get("name")) or _text(content.get("slug"))
            description = _text(content.get("description"))
            package_key = str(path.relative_to(root)).replace("\\", "/")
            entries.append(
                {
                    "package_key": package_key,
                    "slug": _text(content.get("slug")),
                    "name": name,
                    "description": description[:200],
                    "schema_version": _schema_version(content),
                    "content_version": _content_version(content),
                    "tags": _tags(name, description),
                    "source": "bundled" if package_key.startswith("worlds/")
                    else "example" if package_key.startswith("examples/")
                    else "template",
                    "size_bytes": path.stat().st_size,
                }
            )
    # 稳定排序：内置世界 → 示例 → 模板，同组内按名称。
    order = {"bundled": 0, "example": 1, "template": 2}
    entries.sort(
        key=lambda item: (order.get(item["source"], 9), item["name"])
    )
    return entries


def search_entries(
    entries: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """按 slug/名称/描述/标签做不区分大小写的子串过滤。"""
    query = _text(query).casefold()
    if not query:
        return list(entries)
    result: list[dict[str, Any]] = []
    for entry in entries:
        haystack = " ".join(
            (
                _text(entry.get("slug")),
                _text(entry.get("name")),
                _text(entry.get("description")),
                " ".join(_text(tag) for tag in entry.get("tags", [])),
            )
        ).casefold()
        if all(part in haystack for part in query.split()):
            result.append(entry)
    return result


def fetch_entry(
    root: Path,
    package_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按包标识返回 (条目, 完整内容)；包不存在或非法时抛出 LookupError。"""
    root = Path(root)
    candidate = (root / package_key).resolve()
    # 防目录穿越：解析后的路径必须仍位于插件根目录内。
    if not str(candidate).startswith(str(root.resolve())):
        raise LookupError("非法的世界包标识")
    if not candidate.is_file() or candidate.suffix != ".json":
        raise LookupError("世界包不存在或不是 JSON 文件")
    try:
        content = json.loads(candidate.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise LookupError(f"世界包无法解析：{exc}") from exc
    if not isinstance(content, dict) or not content.get("slug"):
        raise LookupError("世界包缺少 slug 字段")
    entry = {
        "package_key": package_key,
        "slug": _text(content.get("slug")),
        "name": _text(content.get("name")) or _text(content.get("slug")),
        "description": _text(content.get("description"))[:200],
        "schema_version": _schema_version(content),
        "content_version": _content_version(content),
        "source": "bundled" if package_key.startswith("worlds/")
        else "example" if package_key.startswith("examples/")
        else "template",
    }
    return entry, content


def validate_remote_url(url: Any, allowed_hosts: Any) -> str:
    """校验远程地址：必须 HTTPS 且主机在白名单内（0.12.0-A3，#4 防 SSRF）。"""
    text = _text(url)
    if not text:
        raise ValueError("缺少远程地址")
    parsed = urlsplit(text)
    if parsed.scheme != "https":
        raise ValueError("仅支持 HTTPS 远程地址")
    host = (parsed.hostname or "").lower()
    allowed = {
        str(item).strip().lower()
        for item in (allowed_hosts or ())
        if str(item).strip()
    } or set(DEFAULT_ALLOWED_HOSTS)
    if host not in allowed:
        raise ValueError(f"远程主机不在白名单：{host}")
    return text


def _normalize_remote_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    slug = _text(item.get("slug") or item.get("id"))
    if not slug:
        return None
    name = _text(item.get("name")) or slug
    description = _text(item.get("description"))
    return {
        "package_key": f"remote:{slug}",
        "slug": slug,
        "name": name,
        "description": description[:200],
        "schema_version": _schema_version(item),
        "content_version": _content_version(item),
        "tags": _tags(name, description),
        "source": "remote",
        "url": _text(item.get("url") or item.get("package_url")),
        "sha256": _text(item.get("sha256")),
        "size_bytes": _int(item.get("size"), 0),
    }


async def fetch_remote_manifest(
    client: Any,
    manifest_url: str,
    allowed_hosts: Any,
    max_bytes: int = 2_000_000,
    ttl_seconds: int = 600,
) -> list[dict[str, Any]]:
    """拉取并缓存远程清单（条目列表）。client 需提供 ``async get(url)``。

    - TTL 缓存缓解 GitHub raw 限流；
    - 清单必须是 JSON 数组，且总体积受 ``max_bytes`` 约束。
    """
    url = validate_remote_url(manifest_url, allowed_hosts)
    now = time.monotonic()
    cached = _REMOTE_CACHE.get(url)
    if cached is not None and now - cached[0] <= max(30, int(ttl_seconds)):
        return cached[1]
    resp = await client.get(url)
    try:
        if int(getattr(resp, "status", 500)) >= 400:
            raise LookupError(f"远程清单拉取失败：HTTP {resp.status}")
        raw = await resp.read()
        if len(raw) > int(max_bytes):
            raise LookupError("远程清单体积超限")
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    finally:
        release = getattr(resp, "release", None)
        if callable(release):
            await release()
    if not isinstance(payload, list):
        raise LookupError("远程清单必须是 JSON 数组")
    entries = [
        entry for entry in (_normalize_remote_entry(item) for item in payload)
        if entry is not None and entry.get("url")
    ]
    _REMOTE_CACHE[url] = (now, entries)
    return entries


async def fetch_remote_package(
    client: Any,
    entry: Mapping[str, Any],
    allowed_hosts: Any,
    max_bytes: int = 2_000_000,
    verify_sha256: bool = True,
) -> dict[str, Any]:
    """按远程条目拉取世界包内容（体积上限 + 可选 SHA256 校验）。"""
    url = validate_remote_url(entry.get("url"), allowed_hosts)
    resp = await client.get(url)
    try:
        if int(getattr(resp, "status", 500)) >= 400:
            raise LookupError(f"远程世界包拉取失败：HTTP {resp.status}")
        raw = await resp.read()
    finally:
        release = getattr(resp, "release", None)
        if callable(release):
            await release()
    if len(raw) > int(max_bytes):
        raise LookupError("远程世界包体积超限")
    if verify_sha256:
        expected = _text(entry.get("sha256"))
        if expected:
            actual = hashlib.sha256(raw).hexdigest()
            if actual.lower() != expected.lower():
                raise LookupError("远程世界包 SHA256 校验失败")
    try:
        content = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LookupError(f"远程世界包无法解析：{exc}") from exc
    if not isinstance(content, dict) or not content.get("slug"):
        raise LookupError("远程世界包缺少 slug 字段")
    return content


def clear_remote_cache() -> None:
    """清空远程清单/包缓存。"""
    _REMOTE_CACHE.clear()
