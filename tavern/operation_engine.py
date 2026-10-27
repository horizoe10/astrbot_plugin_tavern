from __future__ import annotations

import math
from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
from typing import Any

from .entity_registry import EntityRegistry, split_ref


OPERATION_TYPES = frozenset(
    {
        "modify_value", "set_value", "grant_reference", "revoke_reference",
        "add_tag", "remove_tag", "create_instance", "end_instance",
        "set_visibility", "set_availability", "advance_counter",
        "modify_relationship", "emit_event", "request_resolution",
        "add_narrative_constraint",
    }
)
PERSISTENCE_SCOPES = frozenset(
    {"global_character", "world_character", "campaign", "session", "scene", "temporary"}
)


class OperationEngine:
    MAX_OPERATIONS = 128

    def __init__(
        self,
        registry: EntityRegistry,
        numeric_policies: Mapping[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.numeric_policies = dict(numeric_policies or {})

    def _numeric_policy(
        self, item: Mapping[str, Any], target_ref: str
    ) -> Mapping[str, Any] | None:
        requested = item.get("numeric_policy")
        if isinstance(requested, Mapping):
            return requested
        if isinstance(requested, str):
            candidate = self.numeric_policies.get(requested)
            if isinstance(candidate, Mapping):
                return candidate
        for key in (target_ref, target_ref.split(":", 1)[-1]):
            candidate = self.numeric_policies.get(key)
            if isinstance(candidate, Mapping):
                return candidate
        if self.registry.contains(target_ref):
            definition = self.registry.resolve(target_ref).definition
            candidate = definition.get("range")
            if isinstance(candidate, Mapping):
                return {**candidate, "overflow": candidate.get("overflow", "reject")}
        return None

    def validate(self, operations: Any) -> list[dict[str, Any]]:
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise TypeError("操作批次必须是数组")
        if len(operations) > self.MAX_OPERATIONS:
            raise ValueError("单次操作数量超过技术安全上限")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(operations):
            if not isinstance(raw, Mapping):
                raise TypeError(f"操作 #{index + 1} 必须是对象")
            item = dict(raw)
            op = str(item.get("op") or "")
            if op not in OPERATION_TYPES:
                raise ValueError(f"不支持的操作：{op or '<empty>'}")
            scope = str(item.get("persistence_scope") or "session")
            if scope not in PERSISTENCE_SCOPES:
                raise ValueError(f"不支持的持久化作用域：{scope}")
            target_ref = str(item.get("target_ref") or item.get("ref") or "")
            if target_ref:
                split_ref(target_ref)
                runtime_ok = op in {"create_instance", "emit_event", "add_tag", "remove_tag"}
                if not runtime_ok and not self.registry.contains(target_ref):
                    raise ValueError(f"操作引用未注册：{target_ref}")
            value = item.get("value")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("操作数值不得为 NaN 或无穷值")
            result.append(item)
        return result

    def apply(
        self,
        operations: Any,
        state: Mapping[str, Any] | None,
        *,
        dry_run: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        batch = self.validate(operations)
        working: dict[str, Any] = deepcopy(dict(state or {}))
        scoped_state = any(
            key in working
            for key in ("world", "actor", "target", "scene", "session")
        )
        narrative: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []

        for item in batch:
            op = str(item["op"])
            ref = str(item.get("target_ref") or item.get("ref") or "")
            recipient = item.get("recipient")
            recipient = recipient if isinstance(recipient, Mapping) else {}
            state_scope = str(
                recipient.get("scope") or item.get("state_scope") or "world"
            )
            bucket: MutableMapping[str, Any]
            if scoped_state:
                raw_bucket = working.setdefault(state_scope, {})
                if not isinstance(raw_bucket, MutableMapping):
                    raise TypeError(f"状态作用域不是对象：{state_scope}")
                bucket = raw_bucket
            else:
                bucket = working
            refs: MutableMapping[str, Any] = bucket.setdefault("refs", {})
            tags: list[str] = bucket.setdefault("tags", [])
            owned: list[str] = bucket.setdefault("references", [])
            instances: MutableMapping[str, Any] = bucket.setdefault("instances", {})
            before: Any = None
            after: Any = None
            if op in {"modify_value", "advance_counter", "modify_relationship"}:
                aggregate = str(item.get("aggregation_strategy") or "sum")
                default_before = 1 if aggregate == "multiply" else 0
                before = refs.get(ref, default_before)
                operand = float(item.get("value", item.get("delta", 0)) or 0)
                after = (
                    float(before if before is not None else default_before) * operand
                    if aggregate == "multiply"
                    else float(before or 0) + operand
                )
                policy = self._numeric_policy(item, ref)
                if isinstance(policy, Mapping):
                    minimum, maximum = policy.get("min"), policy.get("max")
                    if minimum is not None and after < float(minimum):
                        if policy.get("overflow", "reject") == "clamp": after = float(minimum)
                        else: raise ValueError(f"{ref} 低于世界规则下限")
                    if maximum is not None and after > float(maximum):
                        if policy.get("overflow", "reject") == "clamp": after = float(maximum)
                        else: raise ValueError(f"{ref} 超过世界规则上限")
                refs[ref] = int(after) if after.is_integer() else after
                after = refs[ref]
            elif op in {"set_value", "set_visibility", "set_availability"}:
                before, after = refs.get(ref), item.get("value")
                refs[ref] = after
            elif op == "grant_reference":
                before = ref in owned
                if ref not in owned: owned.append(ref)
                after = True
            elif op == "revoke_reference":
                before = ref in owned
                owned[:] = [value for value in owned if value != ref]
                after = False
            elif op == "add_tag":
                value = str(item.get("value") or ref)
                before = value in tags
                if value not in tags: tags.append(value)
                after = True
            elif op == "remove_tag":
                value = str(item.get("value") or ref)
                before = value in tags
                tags[:] = [tag for tag in tags if tag != value]
                after = False
            elif op == "create_instance":
                instance_id = str(item.get("instance_id") or ref)
                before = instances.get(instance_id)
                if before is not None and item.get("grant_policy", "ignore") == "ignore":
                    after = before
                else:
                    after = deepcopy(item.get("value") or item.get("definition") or {})
                    instances[instance_id] = after
            elif op == "end_instance":
                before = instances.pop(ref, None)
                after = None
            elif op == "add_narrative_constraint":
                projection = {
                    "source_ref": str(item.get("source_ref") or ""),
                    "text": str(item.get("value") or item.get("text") or ""),
                    "visibility": str(item.get("visibility") or "public"),
                }
                narrative.append(projection)
                after = projection
            elif op in {"emit_event", "request_resolution"}:
                after = deepcopy(item.get("value") or item)
                bucket.setdefault("emitted", []).append(after)
            changes.append({
                "op": op,
                "state_scope": state_scope,
                "target_ref": ref,
                "before": before,
                "after": after,
            })

        return (dict(state or {}) if dry_run else working), changes, narrative


__all__ = ["OPERATION_TYPES", "OperationEngine", "PERSISTENCE_SCOPES"]
