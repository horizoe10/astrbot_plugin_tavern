from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .capability_service import CapabilityService
from .entity_registry import EntityRegistry, module_value
from .event_pipeline import EVENT_PHASES, EventPipeline
from .operation_engine import OperationEngine
from .resolution_receipts import ResolutionMethodEngine, new_receipt


class RuleRuntime:
    """Deterministic v5 action pipeline over a frozen world snapshot."""

    def __init__(self, world_snapshot: Mapping[str, Any]) -> None:
        self.world = deepcopy(dict(world_snapshot))
        self.registry = EntityRegistry(self.world)
        self.capabilities = CapabilityService(self.world, self.registry)
        self.pipeline = EventPipeline(self.world, self.registry)
        numeric_policies = module_value(self.world, "numeric_policies", {})
        self.operations = OperationEngine(
            self.registry,
            numeric_policies if isinstance(numeric_policies, Mapping) else {},
        )
        self.resolution = ResolutionMethodEngine(self.world, self.registry)

    def _validate_action_type(self, intent: Mapping[str, Any]) -> None:
        action_type = str(intent.get("action_type") or "freeform")
        if action_type == "freeform":
            return
        ref = action_type if ":" in action_type else f"action_type:{action_type}"
        self.registry.resolve(ref, "action_type")

    def resolve_action_intent(
        self,
        intent: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        world_snapshot_id: str = "",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        operation_id = str(operation_id or f"op_{uuid.uuid4().hex}")
        actor_ref = str(intent.get("actor_ref") or "")
        if not actor_ref:
            raise ValueError("行动意图缺少 actor_ref")
        self._validate_action_type(intent)
        definition = self.capabilities.validate_intent(intent, context)

        enriched = deepcopy(dict(context))
        enriched["action"] = {
            **dict(enriched.get("action") or {}),
            **dict(intent),
        }
        event_name = str(intent.get("event") or "action.requested")
        matched_rules: list[dict[str, Any]] = []
        condition_reads: list[dict[str, Any]] = []
        planned_operations: list[dict[str, Any]] = []

        for phase in ("before_event", "validation", "before_resolution"):
            matched, reads = self.pipeline.match(event_name, phase, enriched)
            matched_rules.extend(matched)
            condition_reads.extend(reads)
            planned_operations.extend(self.pipeline.operations(matched))

        if definition:
            costs = definition.get("costs", [])
            if isinstance(costs, Sequence) and not isinstance(costs, (str, bytes)):
                for cost in costs:
                    if not isinstance(cost, Mapping): continue
                    value = float(cost.get("value", 0) or 0)
                    operation = str(cost.get("operation") or "subtract")
                    planned_operations.append(
                        {
                            "op": "modify_value",
                            "target_ref": str(cost.get("resource_ref") or ""),
                            "value": -value if operation == "subtract" else value,
                            "persistence_scope": str(cost.get("persistence_scope") or "session"),
                            "recipient": {"scope": "actor"},
                        }
                    )
            effects = definition.get("effects", [])
            if isinstance(effects, Sequence) and not isinstance(effects, (str, bytes)):
                planned_operations.extend(dict(item) for item in effects if isinstance(item, Mapping))

        method_ref = str(intent.get("resolution_method_ref") or "")
        if not method_ref and definition:
            method_ref = str(definition.get("resolution_method_ref") or "")
        outcome_id, steps = self.resolution.resolve(method_ref, enriched)

        # The database transaction is committed only after the full pipeline
        # succeeds.  Post-resolution hooks are therefore planned here and
        # included in the same atomic operation batch; no hook can observe a
        # half-committed state.
        for phase in (
            "after_resolution",
            "before_commit",
            "after_commit",
            "before_narration",
        ):
            matched, reads = self.pipeline.match(event_name, phase, enriched)
            matched_rules.extend(matched)
            condition_reads.extend(reads)
            planned_operations.extend(self.pipeline.operations(matched))

        current_state = enriched.get("state")
        current_state = current_state if isinstance(current_state, Mapping) else {}
        committed_state, changes, narrative = self.operations.apply(
            planned_operations, current_state, dry_run=dry_run
        )
        unique_rules: list[dict[str, Any]] = []
        seen_rules: set[str] = set()
        for rule in matched_rules:
            rule_id = str(rule.get("rule_id") or "")
            if rule_id not in seen_rules:
                seen_rules.add(rule_id)
                unique_rules.append(
                    {"rule_id": rule_id, "mode": str(rule.get("mode") or "mechanical"),
                     "priority": int(rule.get("priority", 0) or 0)}
                )
        receipt = new_receipt(
            operation_id=operation_id,
            world_snapshot_id=world_snapshot_id,
            event={"name": event_name, "intent": dict(intent)},
            inputs=[{"actor_ref": actor_ref, "context": "filtered"}],
            condition_reads=condition_reads,
            matched_rules=unique_rules,
            steps=steps,
            outcome_id=outcome_id,
            committed_changes=[] if dry_run else changes,
            narrative_projection=narrative,
            status="dry_run" if dry_run else "completed",
        )
        return {
            "dry_run": dry_run,
            "operation_id": operation_id,
            "outcome_id": outcome_id,
            "planned_operations": planned_operations,
            "changes": changes,
            "state": committed_state,
            "narrative_projection": narrative,
            "receipt": receipt,
        }

    def capability_projection(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = []
        for item in self.capabilities.list_available(context):
            definition = item.get("definition", {})
            result.append(
                {
                    "capability_ref": str(item.get("capability_ref") or item.get("ref") or ""),
                    "label": str(definition.get("label") or definition.get("name") or ""),
                    "description": str(definition.get("description") or ""),
                    "usage_constraints": definition.get("usage_constraints", []),
                    "targeting": definition.get("targeting", {}),
                }
            )
        return result


def enabled_feature_versions(world: Mapping[str, Any]) -> dict[str, str]:
    protocol = module_value(world, "protocol", {})
    if not isinstance(protocol, Mapping):
        return {}
    features = protocol.get("features", {})
    return {str(key): str(value) for key, value in features.items()} if isinstance(features, Mapping) else {}


__all__ = ["RuleRuntime", "enabled_feature_versions"]
