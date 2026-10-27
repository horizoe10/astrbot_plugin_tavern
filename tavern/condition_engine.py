from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .entity_registry import EntityRegistry, split_ref


OPERATORS = frozenset(
    {
        "==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains",
        "not_contains", "intersects", "exists", "not_exists", "matches_ref",
    }
)
SCOPES = frozenset(
    {
        "actor", "target", "action", "scene", "world", "session", "party",
        "location", "object", "relationship", "event", "runtime_effect", "custom",
    }
)


@dataclass
class ConditionResult:
    matched: bool
    reads: list[dict[str, Any]]


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


class ConditionEngine:
    MAX_DEPTH = 12
    MAX_NODES = 256

    def __init__(self, registry: EntityRegistry) -> None:
        self.registry = registry

    def _read(self, operand: Any, context: Mapping[str, Any], reads: list[dict[str, Any]]) -> Any:
        if not isinstance(operand, Mapping):
            return operand
        if "value" in operand and "scope" not in operand:
            return operand.get("value")
        scope = str(operand.get("scope") or "").strip()
        if scope not in SCOPES:
            raise ValueError(f"条件读取了未注册作用域：{scope or '<empty>'}")
        ref = str(operand.get("ref") or "").strip()
        if ref:
            split_ref(ref)
            if not self.registry.contains(ref) and not bool(operand.get("allow_runtime_ref", False)):
                raise ValueError(f"条件引用未注册：{ref}")
        source = context.get(scope, {})
        value = None
        found = False
        if isinstance(source, Mapping):
            refs = source.get("refs")
            if ref and isinstance(refs, Mapping) and ref in refs:
                value, found = refs[ref], True
            elif ref and ref in source:
                value, found = source[ref], True
            elif not ref:
                value, found = source, True
        reads.append({"scope": scope, "ref": ref, "found": found, "value": value})
        return value

    @staticmethod
    def _compare(left: Any, operator: str, right: Any) -> bool:
        if operator not in OPERATORS:
            raise ValueError(f"不支持的条件运算符：{operator}")
        if operator == "exists":
            return left is not None
        if operator == "not_exists":
            return left is None
        if operator == "==" or operator == "matches_ref":
            return left == right
        if operator == "!=":
            return left != right
        if operator in {">", ">=", "<", "<="}:
            if isinstance(left, bool) or isinstance(right, bool):
                raise TypeError("布尔值不能参与数值大小比较")
            try:
                a, b = float(left), float(right)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError("大小比较的双方必须是数值") from exc
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[operator]
        if operator in {"in", "not_in"}:
            result = left in right if _sequence(right) or isinstance(right, (set, Mapping)) else False
            return result if operator == "in" else not result
        if operator in {"contains", "not_contains"}:
            result = right in left if _sequence(left) or isinstance(left, (str, set, Mapping)) else False
            return result if operator == "contains" else not result
        if operator == "intersects":
            return bool(set(left or []) & set(right or []))
        return False

    def evaluate(self, condition: Any, context: Mapping[str, Any]) -> ConditionResult:
        reads: list[dict[str, Any]] = []
        nodes = 0

        def visit(node: Any, depth: int) -> bool:
            nonlocal nodes
            nodes += 1
            if nodes > self.MAX_NODES:
                raise ValueError("条件节点数量超过技术安全上限")
            if depth > self.MAX_DEPTH:
                raise ValueError("条件嵌套超过技术安全上限")
            if node is None or node == {}:
                return True
            if isinstance(node, bool):
                return node
            if not isinstance(node, Mapping):
                raise TypeError("条件必须是对象")
            if "all" in node:
                values = node["all"]
                if not _sequence(values):
                    raise TypeError("all 必须是数组")
                return all(visit(item, depth + 1) for item in values)
            if "any" in node:
                values = node["any"]
                if not _sequence(values):
                    raise TypeError("any 必须是数组")
                return any(visit(item, depth + 1) for item in values)
            if "not" in node:
                return not visit(node["not"], depth + 1)
            compare = node.get("compare") if isinstance(node.get("compare"), Mapping) else node
            left_spec = compare.get("left")
            if left_spec is None and "ref" in compare:
                left_spec = {"scope": compare.get("scope", "actor"), "ref": compare.get("ref")}
            operator = str(compare.get("operator") or "==")
            right_spec = compare.get("right", compare.get("value"))
            left = self._read(left_spec, context, reads)
            right = self._read(right_spec, context, reads)
            offset = compare.get("offset")
            if offset not in (None, 0) and right is not None:
                try:
                    right = float(right) + float(offset)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise TypeError("offset 只能用于数值比较") from exc
            return self._compare(left, operator, right)

        return ConditionResult(matched=visit(condition, 0), reads=reads)


__all__ = ["ConditionEngine", "ConditionResult", "OPERATORS", "SCOPES"]
