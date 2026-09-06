from .common import *
from .normalization import *


def _candidate_rule_field_refs(
    raw: Any,
    *,
    include_recommendations: bool = True,
) -> set[str]:
    """Break the ``evaluation -> dependencies`` import cycle.

    ``candidate_rule_field_refs`` is defined in ``evaluation``, which
    star-imports this module, so it cannot be imported at module top-level
    here. Import it lazily at call time instead.
    """

    from .evaluation import candidate_rule_field_refs

    return candidate_rule_field_refs(
        raw,
        include_recommendations=include_recommendations,
    )


def _fields_from(value: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
    if isinstance(value, Mapping):
        fields = [
            item
            for item in _sequence(value.get("fields"))
            if isinstance(item, Mapping)
        ]
        return fields, value
    return [item for item in value if isinstance(item, Mapping)], None


def dependency_graph(
    fields: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, set[str]]:
    """Return ``field -> direct dependencies`` for every dependency source."""

    definitions, template = _fields_from(fields)
    graph: dict[str, set[str]] = {
        str(field.get("key") or ""): set()
        for field in definitions
        if str(field.get("key") or "")
    }
    for field in definitions:
        key = str(field.get("key") or "")
        if not key:
            continue
        visible_when = field.get("visible_when")
        if isinstance(visible_when, Mapping):
            graph[key].update(str(item) for item in visible_when)
        filter_by = str(field.get("filter_by") or "").strip()
        if filter_by:
            graph[key].add(filter_by)
        if field.get("constraints") not in (None, "", {}):
            graph[key].update(constraint_field_refs(field.get("constraints")))
        candidates = (
            raw_candidate_options(template, field)
            if template is not None
            else _sequence(field.get("options"))
        )
        for candidate in candidates:
            if isinstance(candidate, Mapping) and candidate.get("constraints") not in (None, "", {}):
                graph[key].update(constraint_field_refs(candidate.get("constraints")))
            if isinstance(candidate, Mapping):
                # D1 规则引用：非法结构由 validate_constraint_graph 上报，
                # 依赖图构建保持容错以免重复抛错掩盖问题清单。
                try:
                    graph[key].update(
                        _candidate_rule_field_refs(
                            candidate,
                            include_recommendations=False,
                        )
                    )
                except ValueError:
                    pass
        for target in _sequence(field.get("clear_on_change")):
            target_key = str(target or "").strip()
            if target_key:
                graph.setdefault(target_key, set()).add(key)
    return graph


def dependent_fields(
    template: Mapping[str, Any],
    field_key: str,
) -> tuple[str, ...]:
    """Return direct and transitive downstream fields in template order."""

    graph = dependency_graph(template)
    reverse: dict[str, set[str]] = {key: set() for key in graph}
    for child, parents in graph.items():
        for parent in parents:
            reverse.setdefault(parent, set()).add(child)
    pending = [str(field_key)]
    found: set[str] = set()
    while pending:
        parent = pending.pop(0)
        for child in reverse.get(parent, set()):
            if child not in found and child != field_key:
                found.add(child)
                pending.append(child)
    order = [
        str(item.get("key") or "")
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    return tuple(key for key in order if key in found)


def reachable_candidates(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return the canonical candidate DTOs reachable for current values."""

    # Lazy import avoids a module cycle: card_wizard uses candidate_matches()
    # while this public facade reuses its canonical source/DTO normalization.
    from ..card_wizard import preset_options

    return preset_options(template, field, values)


def _candidate_id(raw: Any) -> str:
    if isinstance(raw, Mapping):
        return str(raw.get("id") or raw.get("key") or "").strip()
    return str(raw or "").strip()


def _intrinsically_impossible(raw: Any) -> bool:
    constraints = normalize_candidate_constraints(raw)
    excluded: dict[str, set[str]] = {}
    for condition in constraints["excludes"]:
        excluded.setdefault(str(condition["field"]), set()).update(
            str(item) for item in condition["values"]
        )
    for condition in constraints["requires_all"]:
        allowed = {str(item) for item in condition["values"]}
        if allowed and allowed <= excluded.get(str(condition["field"]), set()):
            return True
    requires_any = constraints["requires_any"]
    if requires_any and all(
        {str(item) for item in condition["values"]}
        <= excluded.get(str(condition["field"]), set())
        for condition in requires_any
    ):
        return True
    return False


def validate_constraint_graph(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate references, stable parent values, cycles and basic reachability."""

    fields = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    by_key = {str(item.get("key") or ""): item for item in fields}
    issues: list[dict[str, Any]] = []

    known_ids: dict[str, set[str]] = {
        key: {
            candidate_id
            for candidate_id in (
                _candidate_id(item)
                for item in raw_candidate_options(template, field)
            )
            if candidate_id
        }
        for key, field in by_key.items()
    }

    def validate_constraints(raw: Any, path: str, owner_key: str) -> bool:
        try:
            constraints = normalize_candidate_constraints(raw)
        except ValueError as exc:
            issues.append(
                {
                    "level": "error",
                    "code": "constraint.invalid",
                    "message": str(exc),
                    "path": path,
                }
            )
            return False
        for group in CONSTRAINT_GROUPS:
            for index, condition in enumerate(constraints[group]):
                parent = str(condition["field"])
                condition_path = f"{path}.{group}[{index}]"
                if parent not in by_key:
                    issues.append(
                        {
                            "level": "error",
                            "code": "constraint.field_missing",
                            "message": f"引用了不存在的字段 {parent}",
                            "path": condition_path,
                        }
                    )
                    continue
                if parent == owner_key:
                    issues.append(
                        {
                            "level": "error",
                            "code": "constraint.self_dependency",
                            "message": f"字段 {owner_key} 的候选不能依赖自身",
                            "path": condition_path,
                        }
                    )
                parent_ids = known_ids.get(parent, set())
                if parent_ids:
                    for value in condition["values"]:
                        if str(value) not in parent_ids:
                            issues.append(
                                {
                                    "level": "error",
                                    "code": "constraint.value_missing",
                                    "message": f"字段 {parent} 不存在候选 ID {value}",
                                    "path": condition_path,
                                }
                            )
        return not _intrinsically_impossible(constraints)

    for field_index, field in enumerate(fields):
        key = str(field.get("key") or "")
        if field.get("constraints") not in (None, "", {}):
            validate_constraints(
                field.get("constraints"),
                f"actor.fields[{field_index}].constraints",
                key,
            )
        viable = 0
        candidates = raw_candidate_options(template, field)
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                viable += 1
                continue
            raw_constraints = candidate.get("constraints")
            if raw_constraints in (None, "", {}):
                constraint_viable = True
            else:
                constraint_viable = validate_constraints(
                    raw_constraints,
                    f"actor.fields[{field_index}].candidates[{candidate_index}].constraints",
                    key,
                )
            rule_viable = True
            if any(rule_key in candidate for rule_key in CANDIDATE_RULE_KEYS):
                candidate_path = (
                    f"actor.fields[{field_index}].candidates[{candidate_index}]"
                )
                try:
                    rules = normalize_candidate_rules(
                        _rule_keys_view(candidate)
                    )
                except ValueError as exc:
                    issues.append(
                        {
                            "level": "error",
                            "code": "candidate.rules_invalid",
                            "message": str(exc),
                            "path": candidate_path,
                        }
                    )
                    rule_viable = False
                else:
                    for rule_field in sorted(_candidate_rule_field_refs(candidate)):
                        if rule_field not in by_key:
                            issues.append(
                                {
                                    "level": "error",
                                    "code": "candidate.rule_field_missing",
                                    "message": f"候选规则引用了不存在的字段 {rule_field}",
                                    "path": f"{candidate_path}.rules",
                                }
                            )
                            rule_viable = False
                        elif rule_field == key:
                            issues.append(
                                {
                                    "level": "error",
                                    "code": "candidate.rule_self_dependency",
                                    "message": f"字段 {key} 的候选规则不能依赖自身",
                                    "path": f"{candidate_path}.rules",
                                }
                            )
                            rule_viable = False
                        else:
                            parent_ids = known_ids.get(rule_field, set())
                            if parent_ids:
                                for group in (
                                    "eligibility",
                                    "conflicts",
                                    "recommendations",
                                ):
                                    for rule_values in rules[group].get(
                                        rule_field, ()
                                    ):
                                        if str(rule_values) not in parent_ids:
                                            issues.append(
                                                {
                                                    "level": "error",
                                                    "code": "candidate.rule_value_missing",
                                                    "message": (
                                                        f"字段 {rule_field} 不存在候选 ID "
                                                        f"{rule_values}"
                                                    ),
                                                    "path": f"{candidate_path}.rules",
                                                }
                                            )
                                            rule_viable = False
            if constraint_viable and rule_viable:
                viable += 1
        is_choice = str(field.get("type") or "") in {
            "select",
            "preset_select",
            "multi_select",
        }
        if bool(field.get("required", True)) and is_choice and candidates and viable == 0:
            issues.append(
                {
                    "level": "error",
                    "code": "constraint.unreachable_required",
                    "message": f"必填字段 {key} 的所有候选都不可达",
                    "path": f"actor.fields[{field_index}]",
                }
            )

    graph = dependency_graph(template)
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            start = visiting.index(node)
            cycle = visiting[start:] + [node]
            issues.append(
                {
                    "level": "error",
                    "code": "constraint.cycle",
                    "message": "候选依赖形成循环：" + " → ".join(cycle),
                    "path": f"actor.fields.{node}",
                }
            )
            return
        if node in visited:
            return
        visiting.append(node)
        for dependency in graph.get(node, set()):
            if dependency in graph:
                visit(dependency)
        visiting.pop()
        visited.add(node)

    for key in graph:
        visit(key)

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        identity = (
            str(issue.get("code") or ""),
            str(issue.get("path") or ""),
            str(issue.get("message") or ""),
        )
        if identity not in seen:
            unique.append(issue)
            seen.add(identity)
    return unique


__all__ = [name for name in globals() if not name.startswith('__')]

