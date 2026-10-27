from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .condition_engine import ConditionEngine
from .entity_registry import EntityRegistry, module_value, split_ref


GRANT_POLICIES = frozenset({"ignore", "refresh", "stack", "modify", "transition"})


class CapabilityService:
    def __init__(self, world: Mapping[str, Any], registry: EntityRegistry) -> None:
        self.world = dict(world)
        self.registry = registry
        self.conditions = ConditionEngine(registry)

    @staticmethod
    def _instances(context: Mapping[str, Any]) -> list[dict[str, Any]]:
        actor = context.get("actor", {})
        actor = actor if isinstance(actor, Mapping) else {}
        values = actor.get("capabilities", [])
        return [dict(item) for item in values if isinstance(item, Mapping)] if isinstance(values, Sequence) else []

    def list_available(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for instance in self._instances(context):
            ref = str(instance.get("capability_ref") or instance.get("ref") or "")
            if not self.registry.contains(ref) or not bool(instance.get("available", True)):
                continue
            definition = self.registry.resolve(ref, "capability").definition
            constraints = definition.get("usage_constraints", [])
            allowed = True
            if isinstance(constraints, Sequence) and not isinstance(constraints, (str, bytes)):
                for constraint in constraints:
                    if isinstance(constraint, Mapping):
                        condition = constraint.get("when", constraint.get("condition", constraint))
                        if not self.conditions.evaluate(condition, context).matched:
                            allowed = False
                            break
            if allowed:
                result.append({**instance, "definition": dict(definition)})
        return result

    def validate_intent(self, intent: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
        ref = str(intent.get("capability_ref") or "").strip()
        if not ref:
            return None
        available = {str(item.get("capability_ref") or item.get("ref")): item for item in self.list_available(context)}
        if ref not in available:
            raise ValueError(f"行动使用了未解锁或当前不可用的能力：{ref}")
        definition = available[ref]["definition"]
        self._validate_targets(definition, intent, context)
        resources = context.get("actor", {})
        resources = resources.get("refs", {}) if isinstance(resources, Mapping) else {}
        costs = definition.get("costs", [])
        if isinstance(costs, Sequence) and not isinstance(costs, (str, bytes)):
            for cost in costs:
                if not isinstance(cost, Mapping): continue
                resource_ref = str(cost.get("resource_ref") or "")
                required = float(cost.get("value", 0) or 0)
                if float(resources.get(resource_ref, 0) or 0) < required:
                    raise ValueError(f"资源不足：{resource_ref}")
        return dict(definition)

    def _validate_targets(
        self,
        definition: Mapping[str, Any],
        intent: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        targeting = definition.get("targeting")
        if not isinstance(targeting, Mapping):
            return
        raw_targets = intent.get("targets", [])
        if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
            raise TypeError("行动目标必须是数组")
        targets = list(raw_targets)
        minimum = int(targeting.get("min_targets", 0) or 0)
        maximum = int(targeting.get("max_targets", minimum or len(targets)) or 0)
        if len(targets) < minimum or (maximum >= 0 and len(targets) > maximum):
            raise ValueError(f"目标数量必须在 {minimum}—{maximum} 之间")
        raw_types = targeting.get("entity_types", [])
        allowed_types = {
            str(value) for value in raw_types
        } if isinstance(raw_types, Sequence) and not isinstance(raw_types, (str, bytes)) else set()
        selector = targeting.get("selector")
        target_contexts = context.get("targets", {})
        target_contexts = target_contexts if isinstance(target_contexts, Mapping) else {}
        for raw in targets:
            target_ref = str(
                raw.get("ref") if isinstance(raw, Mapping) else raw
            ).strip()
            entity_type, _ = split_ref(target_ref)
            if allowed_types and entity_type not in allowed_types:
                raise ValueError(f"目标类型不允许：{entity_type}")
            if selector:
                selected_context = target_contexts.get(target_ref, {})
                selected_context = (
                    selected_context
                    if isinstance(selected_context, Mapping)
                    else {}
                )
                evaluation_context = {
                    **context,
                    "target": selected_context,
                }
                if not self.conditions.evaluate(selector, evaluation_context).matched:
                    raise ValueError(f"目标不满足能力选择条件：{target_ref}")

    def initial_grants(self, preset_refs: Mapping[str, Any]) -> list[dict[str, Any]]:
        module = module_value(self.world, "capabilities", {})
        module = module if isinstance(module, Mapping) else {}
        grants = module.get("initial_grants", [])
        result: list[dict[str, Any]] = []
        if not isinstance(grants, Sequence) or isinstance(grants, (str, bytes)):
            return result
        context = {"actor": {"refs": dict(preset_refs)}}
        for grant in grants:
            if not isinstance(grant, Mapping): continue
            if self.conditions.evaluate(grant.get("when", {}), context).matched:
                ref = str(grant.get("capability_ref") or grant.get("target_ref") or "")
                self.registry.resolve(ref, "capability")
                result.append(dict(grant))
        return result

    def progression_cycles(self) -> list[list[str]]:
        graph: dict[str, set[str]] = {}
        module = module_value(self.world, "capabilities", {})
        module = module if isinstance(module, Mapping) else {}
        transitions = module.get("transitions", [])
        if not isinstance(transitions, Sequence) or isinstance(transitions, (str, bytes)):
            return []
        for transition in transitions:
            if not isinstance(transition, Mapping): continue
            sources = transition.get("from", [])
            operations = transition.get("operations", [])
            targets = [
                str(op.get("target_ref")) for op in operations
                if isinstance(op, Mapping) and op.get("op") == "grant_reference"
            ] if isinstance(operations, Sequence) else []
            for source in sources if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) else []:
                graph.setdefault(str(source), set()).update(targets)
        cycles: list[list[str]] = []
        visiting: list[str] = []
        visited: set[str] = set()
        def visit(node: str) -> None:
            if node in visiting:
                cycles.append(visiting[visiting.index(node):] + [node]); return
            if node in visited: return
            visiting.append(node)
            for target in graph.get(node, set()): visit(target)
            visiting.pop(); visited.add(node)
        for node in graph: visit(node)
        return cycles


__all__ = ["CapabilityService", "GRANT_POLICIES"]
