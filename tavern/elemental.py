"""元素反应系统（v0.12.0-A14）。

世界包可声明一个 ``elemental`` 块，描述元素、目标亲和（抗性）与双元素反应表：

.. code-block:: json

    {
      "elemental": {
        "elements": ["火", "水", "雷", "冰", "风", "土", "光", "暗"],
        "affinities": {
          "npc:炎魔": { "水": 0.5, "冰": 1.0 },
          "item:木盾": { "火": -1.0 }
        },
        "reactions": [
          { "a": "火", "b": "冰", "result": "融化", "effect": { "op": "emit_event", "text": "冰层融化，水汽升腾。" } }
        ],
        "resolver": "my_system"
      }
    }

- ``affinities`` 取值 -2..2：负值=克制/抗性，正值=易伤/亲和；0=中性。
- ``reactions`` 为无序元素对 → 反应结果与声明式效果（复用操作引擎语义）。
- ``resolver`` 可选：指向已注册的 ``element_resolver`` 扩展点；未命中时回退声明式表。

本模块不依赖 AstrBot，可独立单测；``resolve`` 为纯函数、不落库。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

ELEMENTAL_CONTRACT_VERSION = "1.0"
MAX_ELEMENTS = 64
MAX_REACTIONS = 512
AFFINITY_MIN = -2.0
AFFINITY_MAX = 2.0


def parse(world: Mapping[str, Any] | None) -> dict[str, Any]:
    """从世界包提取并规范化 ``elemental`` 块，返回 ElementalTable dict。"""
    raw = (world or {}).get("elemental") or {}
    if not isinstance(raw, Mapping):
        raw = {}

    elements: list[str] = []
    seen: set[str] = set()
    for item in raw.get("elements", []) or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        elements.append(text)

    affinities: dict[str, dict[str, float]] = {}
    raw_affinities = raw.get("affinities") or {}
    if isinstance(raw_affinities, Mapping):
        for ref, table in raw_affinities.items():
            ref = str(ref).strip()
            if not ref or not isinstance(table, Mapping):
                continue
            parsed_table: dict[str, float] = {}
            for element, value in table.items():
                element = str(element).strip()
                if not element or not isinstance(value, (int, float)):
                    continue
                parsed_table[element] = min(
                    AFFINITY_MAX, max(AFFINITY_MIN, float(value))
                )
            if parsed_table:
                affinities[ref] = parsed_table

    reactions: list[dict[str, Any]] = []
    for item in raw.get("reactions", []) or []:
        if not isinstance(item, Mapping):
            continue
        a = str(item.get("a") or "").strip()
        b = str(item.get("b") or "").strip()
        result = str(item.get("result") or "").strip()
        if not a or not b or not result:
            continue
        effect = item.get("effect")
        if effect is not None and not isinstance(effect, (Mapping, list)):
            effect = None
        reactions.append(
            {
                "a": a,
                "b": b,
                "result": result,
                "effect": effect,
                "severity": str(item.get("severity") or "").strip() or "normal",
            }
        )
    if len(reactions) > MAX_REACTIONS:
        reactions = reactions[:MAX_REACTIONS]
    if len(elements) > MAX_ELEMENTS:
        elements = elements[:MAX_ELEMENTS]

    return {
        "version": str(raw.get("version") or ELEMENTAL_CONTRACT_VERSION),
        "elements": elements,
        "affinities": affinities,
        "reactions": reactions,
        "resolver": str(raw.get("resolver") or "").strip(),
        "raw": dict(raw),
    }


def affinity(
    table: Mapping[str, Any],
    target_ref: str,
    element: str,
) -> float:
    """目标对某元素的亲和值（默认 0 = 中性）。"""
    element = str(element or "").strip()
    target_ref = str(target_ref or "").strip()
    if not element or not target_ref:
        return 0.0
    entry = (table.get("affinities") or {}).get(target_ref)
    if not isinstance(entry, Mapping):
        return 0.0
    value = entry.get(element)
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _find_reaction(
    table: Mapping[str, Any],
    source: str,
    target_element: str,
) -> dict[str, Any] | None:
    for item in table.get("reactions", []) or []:
        pair = {str(item.get("a") or ""), str(item.get("b") or "")}
        if pair == {source, target_element}:
            return item
    return None


def _normalize_custom(
    table: Mapping[str, Any],
    source: str,
    target_ref: str,
    target_element: str | None,
    out: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if out is None:
        return None
    if not isinstance(out, Mapping):
        return None
    matched = bool(out.get("matched", True))
    return {
        "matched": matched,
        "source": str(out.get("source") or source),
        "target": str(out.get("target") or target_ref),
        "target_element": str(out.get("target_element") or target_element or ""),
        "affinity": float(out.get("affinity") or affinity(table, target_ref, source)),
        "reaction": out.get("reaction"),
        "effects": out.get("effects") or [],
        "resolver": str(out.get("resolver") or table.get("resolver") or "custom"),
    }


def resolve(
    table: Mapping[str, Any],
    source: str,
    target_ref: str,
    target_element: str | None = None,
    context: Mapping[str, Any] | None = None,
    resolver: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """解析一次「属性元素反应」。

    返回 ``None`` 表示既无亲和加成也无反应命中；否则返回确定性结果：
    ``{matched, source, target, target_element, affinity, reaction, effects}``。
    """
    source = str(source or "").strip()
    target_ref = str(target_ref or "").strip()
    target_element = str(target_element or "").strip() or None
    if not source or not target_ref:
        return None

    if resolver is not None and callable(resolver):
        try:
            out = resolver(source, target_ref, dict(context or {}), dict(table))
            normalized = _normalize_custom(
                table, source, target_ref, target_element, out
            )
            if normalized is not None:
                return normalized
        except Exception:
            # 扩展点异常不阻断，回退声明式表。
            pass

    aff = affinity(table, target_ref, source)
    reaction = _find_reaction(table, source, target_element) if target_element else None
    if aff == 0 and reaction is None:
        return None

    return {
        "matched": True,
        "source": source,
        "target": target_ref,
        "target_element": target_element or "",
        "affinity": aff,
        "reaction": reaction,
        "effects": (reaction or {}).get("effect") or [],
        "resolver": str(table.get("resolver") or "table") or "table",
    }


def validate(world: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """体检级别的元素配置校验，返回 ``[{level, path, code, message}]``。"""
    issues: list[dict[str, Any]] = []
    raw = (world or {}).get("elemental") or {}
    if not isinstance(raw, Mapping):
        return [
            {
                "level": "error",
                "path": "elemental",
                "code": "elemental.not_object",
                "message": "elemental 必须是 JSON 对象",
            }
        ]
    if not raw:
        return issues
    elements = [str(x).strip() for x in raw.get("elements", []) or [] if str(x).strip()]
    if len(elements) != len(set(elements)):
        issues.append(
            {
                "level": "error",
                "path": "elemental.elements",
                "code": "elemental.duplicate_element",
                "message": "元素名称重复",
            }
        )
    known = set(elements)
    if elements and len(elements) > MAX_ELEMENTS:
        issues.append(
            {
                "level": "error",
                "path": "elemental.elements",
                "code": "elemental.too_many",
                "message": f"元素数量超过上限 {MAX_ELEMENTS}",
            }
        )
    for ref, table in (raw.get("affinities") or {}).items() if isinstance(raw.get("affinities"), Mapping) else []:
        if not isinstance(table, Mapping):
            issues.append(
                {
                    "level": "warning",
                    "path": f"elemental.affinities.{ref}",
                    "code": "elemental.affinity.not_object",
                    "message": "亲和表必须是对象",
                }
            )
            continue
        for element, value in table.items():
            if elements and str(element) not in known:
                issues.append(
                    {
                        "level": "warning",
                        "path": f"elemental.affinities.{ref}.{element}",
                        "code": "elemental.unknown_element",
                        "message": f"亲和引用了未声明元素：{element}",
                    }
                )
            if isinstance(value, (int, float)) and not (
                AFFINITY_MIN <= float(value) <= AFFINITY_MAX
            ):
                issues.append(
                    {
                        "level": "warning",
                        "path": f"elemental.affinities.{ref}.{element}",
                        "code": "elemental.affinity.out_of_range",
                        "message": f"亲和值超出 {AFFINITY_MIN}..{AFFINITY_MAX}，已钳制",
                    }
                )
    if isinstance(raw.get("reactions"), list) and len(raw["reactions"]) > MAX_REACTIONS:
        issues.append(
            {
                "level": "warning",
                "path": "elemental.reactions",
                "code": "elemental.reactions.truncated",
                "message": f"反应表超过 {MAX_REACTIONS} 条，超出的将被忽略",
            }
        )
    return issues


def table(world: Mapping[str, Any] | None) -> dict[str, Any]:
    """世界包元素表的面向展示摘要（供 /element-table 与前端面板）。"""
    parsed = parse(world)
    return {
        "version": parsed["version"],
        "elements": parsed["elements"],
        "affinities": parsed["affinities"],
        "reactions": parsed["reactions"],
        "resolver": parsed["resolver"],
        "issues": validate(world),
    }
