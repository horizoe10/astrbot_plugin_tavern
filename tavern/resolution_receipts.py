from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .entity_registry import EntityRegistry, module_value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_receipt(
    *, operation_id: str, world_snapshot_id: str, event: Mapping[str, Any],
    inputs: Sequence[Any], condition_reads: Sequence[Any], matched_rules: Sequence[Any],
    steps: Sequence[Any], outcome_id: str, committed_changes: Sequence[Any],
    narrative_projection: Sequence[Any], status: str = "completed",
) -> dict[str, Any]:
    payload = {
        "receipt_id": f"receipt_{uuid.uuid4().hex}",
        "operation_id": operation_id,
        "world_snapshot_id": world_snapshot_id,
        "event": dict(event),
        "inputs": list(inputs),
        "condition_reads": list(condition_reads),
        "matched_rules": list(matched_rules),
        "steps": list(steps),
        "outcome_id": str(outcome_id or "world_defined"),
        "committed_changes": list(committed_changes),
        "narrative_projection": list(narrative_projection),
        "status": status,
        "created_at": utc_now(),
    }
    payload["content_hash"] = content_hash(payload)
    return payload


class ResolutionMethodEngine:
    """Small declarative resolver; no eval, imports, formulas or model dice."""

    MAX_STEPS = 64

    def __init__(self, world: Mapping[str, Any], registry: EntityRegistry) -> None:
        self.world = dict(world)
        self.registry = registry

    def _definition(self, method_ref: str) -> Mapping[str, Any] | None:
        if not method_ref:
            return None
        return self.registry.resolve(method_ref, "resolution_method").definition

    def resolve(
        self,
        method_ref: str,
        context: Mapping[str, Any],
        *,
        rng: random.Random | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        definition = self._definition(method_ref)
        if definition is None:
            return "no_resolution", []
        raw_steps = definition.get("steps", [])
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
            raise TypeError("resolution method steps 必须是数组")
        if len(raw_steps) > self.MAX_STEPS:
            raise ValueError("裁定步骤超过技术安全上限")
        generator = rng or random.SystemRandom()
        values: dict[str, Any] = {}
        receipt_steps: list[dict[str, Any]] = []
        outcome = str(definition.get("default_outcome_id") or "world_defined")
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, Mapping):
                raise TypeError(f"裁定步骤 #{index + 1} 必须是对象")
            step = dict(raw)
            op = str(step.get("op") or "")
            step_id = str(step.get("step_id") or f"step_{index + 1}")
            if op == "read_value":
                scope = str(step.get("scope") or "action")
                ref = str(step.get("ref") or "")
                source = context.get(scope, {})
                refs = source.get("refs", {}) if isinstance(source, Mapping) else {}
                value = refs.get(ref) if isinstance(refs, Mapping) else None
            elif op == "random_integer":
                minimum, maximum = int(step.get("min", 1)), int(step.get("max", 20))
                if minimum > maximum or maximum - minimum > 1000000:
                    raise ValueError("随机整数范围无效")
                value = generator.randint(minimum, maximum)
            elif op == "sum":
                value = sum(float(values.get(str(key), 0) or 0) for key in step.get("inputs", []))
                if value.is_integer(): value = int(value)
            elif op == "compare":
                left = values.get(str(step.get("left") or ""), step.get("left_value"))
                right = values.get(str(step.get("right") or ""), step.get("right_value"))
                operator = str(step.get("operator") or ">=")
                value = {"==": left == right, "!=": left != right, ">=": left >= right,
                         ">": left > right, "<=": left <= right, "<": left < right}.get(operator)
                if value is None: raise ValueError(f"裁定比较运算符无效：{operator}")
                outcome = str(step.get("true_outcome_id") if value else step.get("false_outcome_id") or outcome)
            elif op == "set_outcome":
                value = str(step.get("outcome_id") or outcome)
                outcome = value
            else:
                raise ValueError(f"不支持的裁定步骤：{op or '<empty>'}")
            values[step_id] = value
            receipt_steps.append({"step_id": step_id, "op": op, "value": value})
        return outcome, receipt_steps


__all__ = ["ResolutionMethodEngine", "content_hash", "new_receipt", "utc_now"]
