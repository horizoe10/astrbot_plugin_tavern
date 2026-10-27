from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .condition_engine import ConditionEngine
from .entity_registry import EntityRegistry, module_value


EVENT_PHASES = (
    "before_event", "validation", "before_resolution", "resolution",
    "after_resolution", "before_commit", "after_commit", "before_narration",
)
STACKING_STRATEGIES = frozenset(
    {
        "first", "last", "highest", "lowest", "sum", "multiply", "replace",
        "unique", "max_count", "priority_only", "deny_on_conflict",
    }
)


class EventPipeline:
    MAX_MATCHES = 64

    def __init__(self, world: Mapping[str, Any], registry: EntityRegistry) -> None:
        self.world = dict(world)
        self.registry = registry
        self.conditions = ConditionEngine(registry)

    def _rules(self) -> tuple[list[dict[str, Any]], int]:
        module = module_value(self.world, "interaction_rules", {})
        if not isinstance(module, Mapping) or not bool(module.get("enabled", False)):
            return [], 0
        settings = module.get("settings")
        settings = settings if isinstance(settings, Mapping) else {}
        limit = int(settings.get("max_matches_per_event", 10) or 10)
        limit = max(1, min(self.MAX_MATCHES, limit))
        values = module.get("rules", [])
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TypeError("interaction_rules.rules 必须是数组")
        return [dict(item) for item in values if isinstance(item, Mapping)], limit

    @staticmethod
    def _triggered(rule: Mapping[str, Any], event_name: str, phase: str) -> bool:
        triggers = rule.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        if not isinstance(triggers, Sequence):
            return False
        for trigger in triggers:
            if isinstance(trigger, str) and trigger == event_name:
                return phase == "before_resolution"
            if isinstance(trigger, Mapping):
                if str(trigger.get("event") or "") == event_name and str(
                    trigger.get("phase") or "before_resolution"
                ) == phase:
                    return True
        return False

    def match(
        self,
        event_name: str,
        phase: str,
        context: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if phase not in EVENT_PHASES:
            raise ValueError(f"不支持的事件阶段：{phase}")
        rules, limit = self._rules()
        matched: list[dict[str, Any]] = []
        reads: list[dict[str, Any]] = []
        for rule in rules:
            if not bool(rule.get("enabled", True)) or not self._triggered(rule, event_name, phase):
                continue
            result = self.conditions.evaluate(rule.get("when", {}), context)
            reads.extend(result.reads)
            if result.matched:
                matched.append(rule)
        matched.sort(key=lambda item: (-int(item.get("priority", 0) or 0), str(item.get("rule_id") or "")))
        if len(matched) > limit:
            matched = matched[:limit]
        return self._aggregate(matched), reads

    def _aggregate(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ungrouped: list[dict[str, Any]] = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            stacking = rule.get("stacking")
            stacking = stacking if isinstance(stacking, Mapping) else {}
            group = str(stacking.get("group") or "")
            if group:
                groups.setdefault(group, []).append(rule)
            else:
                ungrouped.append(rule)
        selected = list(ungrouped)
        for group, values in groups.items():
            stacking = values[0].get("stacking") or {}
            strategy = str(stacking.get("strategy") or "priority_only")
            if strategy not in STACKING_STRATEGIES:
                raise ValueError(f"不支持的叠加策略：{strategy}")
            if strategy == "deny_on_conflict" and len(values) > 1:
                raise ValueError(f"交互规则组发生禁止冲突：{group}")
            limit = max(1, int(stacking.get("limit", len(values)) or len(values)))
            if strategy in {"first", "priority_only", "highest"}:
                chosen = values[:1]
            elif strategy in {"last", "lowest", "replace"}:
                chosen = values[-1:]
            elif strategy == "unique":
                seen: set[str] = set()
                chosen = []
                for value in values:
                    fingerprint = repr(value.get("effects", []))
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        chosen.append(value)
            else:
                chosen = values[:limit]
            selected.extend(deepcopy(chosen[:limit]))
        selected.sort(key=lambda item: (-int(item.get("priority", 0) or 0), str(item.get("rule_id") or "")))
        return selected

    @staticmethod
    def operations(rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        aggregates: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for rule in rules:
            mode = str(rule.get("mode") or "mechanical")
            stacking = rule.get("stacking")
            stacking = stacking if isinstance(stacking, Mapping) else {}
            group = str(stacking.get("group") or "")
            strategy = str(stacking.get("strategy") or "priority_only")
            effects = rule.get("effects", [])
            if not isinstance(effects, Sequence) or isinstance(effects, (str, bytes)):
                raise TypeError("交互规则 effects 必须是数组")
            for effect in effects:
                if not isinstance(effect, Mapping):
                    continue
                item = dict(effect)
                item.setdefault("source_ref", f"interaction_rule:{rule.get('rule_id', '')}")
                if mode == "narrative" and item.get("op") != "add_narrative_constraint":
                    continue
                if (
                    group
                    and strategy in {"sum", "multiply"}
                    and isinstance(item.get("value"), (int, float))
                ):
                    key = (
                        group,
                        strategy,
                        str(item.get("op") or ""),
                        str(item.get("target_ref") or ""),
                    )
                    current = aggregates.get(key)
                    if current is None:
                        item["aggregation_strategy"] = strategy
                        aggregates[key] = item
                    elif strategy == "sum":
                        current["value"] = float(current.get("value", 0)) + float(item["value"])
                    else:
                        current["value"] = float(current.get("value", 1)) * float(item["value"])
                    continue
                result.append(item)
        result.extend(aggregates.values())
        return result


__all__ = ["EVENT_PHASES", "EventPipeline", "STACKING_STRATEGIES"]
