from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .presets import validate_preset_dimensions
from .entity_registry import EntityRegistry, module_value
from .event_pipeline import EVENT_PHASES, STACKING_STRATEGIES
from .operation_engine import OPERATION_TYPES, PERSISTENCE_SCOPES
from .operation_engine import OperationEngine
from .capability_service import CapabilityService
from .chat_experience import validate_chat_experience
from .constants import PLUGIN_VERSION


WORLD_SCHEMA_VERSION = 5
SUPPORTED_WORLD_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5})
FEATURE_VERSIONS = {
    "entity_registry": "1.0",
    "condition_engine": "1.0",
    "operation_engine": "1.0",
    "event_pipeline": "1.0",
    "resolution_receipt": "1.0",
    "capabilities": "1.0",
    "resources": "1.0",
    "runtime_effects": "1.0",
    "objects": "1.0",
    "resolution_methods": "1.0",
    "interaction_rules": "1.0",
    "action_intents": "1.0",
    "chat_experience": "1.0",
}
STAT_MODES = {"none", "manual", "preset", "preset_stack"}
RESOLUTION_MODES = {"none", "narrative", "dice_only", "attribute"}

DEFAULT_DANGER_LEVELS = (
    {"id": "safe", "label": "安全"},
    {"id": "controlled", "label": "可控"},
    {"id": "dangerous", "label": "危险"},
    {"id": "desperate", "label": "绝境"},
    {
        "id": "lethal",
        "label": "致命",
        "requires_visible_consequence": True,
    },
)

DEFAULT_DIFFICULTY_POLICY = {
    "safe": None,
    "controlled": 9,
    "dangerous": 13,
    "desperate": 17,
    "lethal": 21,
}

DEFAULT_OUTCOME_POLICY = {
    "natural_20_critical": True,
    "natural_1_critical": True,
    "critical_success_margin": 10,
    "cost_success_min_margin": -4,
    "failure_min_margin": -9,
}


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lower().removeprefix("v")
    # B1-era templates briefly used a suffix-only identifier.  Continue to
    # accept it, while current releases use the full numeric + suffix form.
    if text in {"b1", "b2"}:
        return (0, 12, 0)
    numeric = text.split("-", 1)[0]
    parts = numeric.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple((int(parts[index]) if index < len(parts) else 0) for index in range(3))


def _feature_requirement(value: object) -> tuple[str, tuple[int, int, int]]:
    text = str(value or "").strip()
    if "@>=" not in text:
        raise ValueError(f"required_features 格式无效：{text}")
    name, raw_version = text.split("@>=", 1)
    parsed = _version_tuple(raw_version)
    if not name or parsed is None:
        raise ValueError(f"required_features 格式无效：{text}")
    return name, parsed


def stats_mode(stats: Mapping[str, Any] | None) -> str:
    stats = stats if isinstance(stats, Mapping) else {}
    mode = str(stats.get("mode") or "").strip().lower()
    if mode in STAT_MODES:
        return mode
    if (
        stats.get("input_mode")
        == "automatic_profession_base_plus_two_fixed_bonus_choices"
        or stats.get("allocation_mode")
        == "profession_base_plus_primary7_secondary3"
    ):
        return "preset"
    return "manual"


def world_contract(world: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the runtime contract for the current world protocol."""

    source = world if isinstance(world, Mapping) else {}
    rules = source.get("rules")
    rules = rules if isinstance(rules, Mapping) else source
    card = rules.get("character_card")
    card = card if isinstance(card, Mapping) else {}
    stats = card.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    generation = card.get("stat_generation")
    generation = generation if isinstance(generation, Mapping) else {}
    if str(generation.get("mode") or "").lower() == "preset_stack":
        stats = {
            **dict(stats),
            "mode": "preset_stack",
            "stat_generation": dict(generation),
        }
    try:
        version = int(
            source.get(
                "world_schema_version",
                rules.get("world_schema_version", 0),
            )
        )
    except (TypeError, ValueError):
        version = 0

    protocol = module_value(source, "protocol", {})
    protocol = protocol if isinstance(protocol, Mapping) else {}
    features = protocol.get("features", {})
    features = (
        {str(key): str(value) for key, value in features.items()}
        if isinstance(features, Mapping)
        else {}
    )
    required_features = module_value(source, "required_features", [])
    if not isinstance(required_features, Sequence) or isinstance(required_features, (str, bytes)):
        required_features = []

    resolution = rules.get("resolution")
    if isinstance(resolution, Mapping):
        resolution_raw = dict(resolution)
        resolution_mode = str(
            resolution_raw.get("mode") or "none"
        ).lower()
    else:
        resolution_raw = {"dice_system": str(resolution or "d20")}
        resolution_mode = (
            "attribute" if stats_mode(stats) != "none" else "dice_only"
        )
    if resolution_mode not in RESOLUTION_MODES:
        resolution_mode = "none"

    attributes = [
        dict(item)
        for item in stats.get("attributes", [])
        if isinstance(item, Mapping) and item.get("key")
    ]
    allowed = resolution_raw.get("allowed_attributes")
    if not isinstance(allowed, Sequence) or isinstance(
        allowed, (str, bytes)
    ):
        allowed = [str(item["key"]) for item in attributes]
    danger_levels = rules.get("danger_levels")
    if not isinstance(danger_levels, Sequence) or isinstance(
        danger_levels, (str, bytes)
    ):
        danger_levels = DEFAULT_DANGER_LEVELS
    presentation = rules.get("option_presentation")
    presentation = (
        presentation if isinstance(presentation, Mapping) else {}
    )
    annotation = presentation.get("annotation")
    annotation = annotation if isinstance(annotation, Mapping) else {}
    difficulty_policy = dict(DEFAULT_DIFFICULTY_POLICY)
    if isinstance(resolution_raw.get("difficulty_policy"), Mapping):
        difficulty_policy.update(
            dict(resolution_raw.get("difficulty_policy") or {})
        )
    outcome_policy = dict(DEFAULT_OUTCOME_POLICY)
    if isinstance(resolution_raw.get("outcome_policy"), Mapping):
        outcome_policy.update(dict(resolution_raw.get("outcome_policy") or {}))
    return {
        "version": version,
        "minimum_plugin_version": str(
            source.get("minimum_plugin_version")
            or source.get("min_plugin_version")
            or ""
        ),
        "protocol": {
            "core_version": int(protocol.get("core_version", version) or version),
            "features": features,
        },
        "required_features": [str(item) for item in required_features],
        "stats": {**dict(stats), "mode": stats_mode(stats)},
        "attributes": attributes,
        "resolution": {
            "mode": resolution_mode,
            "dice_system": str(
                resolution_raw.get("dice_system") or "d20"
            ),
            "unknown_attribute": str(
                resolution_raw.get("unknown_attribute") or "reject"
            ),
            "generic_check": dict(
                resolution_raw.get("generic_check") or {}
            ),
            "allowed_attributes": [str(item) for item in allowed],
            "difficulty_policy": difficulty_policy,
            "outcome_policy": outcome_policy,
        },
        "danger_levels": [
            dict(item)
            for item in danger_levels
            if isinstance(item, Mapping)
        ],
        "annotation": {
            "enabled": bool(annotation.get("enabled", True)),
            "show_danger": bool(annotation.get("show_danger", True)),
            "show_check_attribute": bool(
                annotation.get("show_check_attribute", True)
            ),
            "show_failure_consequence": bool(
                annotation.get("show_failure_consequence", False)
            ),
        },
        "capabilities": dict(
            source.get("capabilities")
            or rules.get("capabilities")
            or {}
        ),
        "economy": _economy_contract(rules),
        "preset_dimensions": list(card.get("preset_dimensions") or []),
        "knowledge_boundary": dict(
            rules.get("knowledge_boundary") or {}
        ),
        "content_boundary": dict(rules.get("content_boundary") or {}),
        "chat_experience": validate_chat_experience(source),
    }


def _economy_contract(rules: Mapping[str, Any]) -> dict[str, Any]:
    """A16：解析可选经济块（不写死任何货币/汇率语义；未声明即空）。"""
    raw = rules.get("economy")
    if not isinstance(raw, Mapping):
        return {"available": False, "policy": {}}
    currencies = raw.get("currencies") or []
    if not isinstance(currencies, list):
        currencies = []
    initial_wallets = raw.get("initial_wallets") or []
    if not isinstance(initial_wallets, list):
        initial_wallets = []
    exchange_rules = raw.get("exchange_rules") or []
    if not isinstance(exchange_rules, list):
        exchange_rules = []
    policy = raw.get("policy")
    if not isinstance(policy, Mapping):
        policy = {}
    return {
        "available": True,
        "currencies": [dict(item) for item in currencies if isinstance(item, Mapping)],
        "initial_wallets": [
            dict(item) for item in initial_wallets if isinstance(item, Mapping)
        ],
        "exchange_rules": [
            dict(item) for item in exchange_rules if isinstance(item, Mapping)
        ],
        "policy": dict(policy),
    }


def validate_world_contract(world: Mapping[str, Any]) -> dict[str, Any]:
    contract = world_contract(world)
    if contract["version"] not in SUPPORTED_WORLD_SCHEMA_VERSIONS:
        raise ValueError(
            f"{PLUGIN_VERSION} 仅接受世界包协议 v2、v3、v4 或 v5；"
            f"当前为 v{contract['version']}"
        )
    stats = contract["stats"]
    mode = stats["mode"]
    if mode == "preset_stack":
        if contract["version"] < 3:
            raise ValueError("preset_stack 必须使用世界包协议 v3")
        minimum = _version_tuple(contract.get("minimum_plugin_version"))
        if minimum is None or minimum < (0, 9, 3):
            raise ValueError(
                "preset_stack 世界包必须声明 minimum_plugin_version >= 0.9.3"
            )
    if contract["version"] >= 4:
        # 0.11.1：v5 世界缺 minimum_plugin_version 时此前会误报
        # “v4 必须声明 >= 0.10.0”；按实际协议版本给出准确要求。
        required = (0, 10, 0) if contract["version"] == 4 else (0, 11, 0)
        label = "v4" if contract["version"] == 4 else "v5"
        minimum = _version_tuple(contract.get("minimum_plugin_version"))
        if minimum is None or minimum < required:
            raise ValueError(
                f"世界包协议 {label} 必须声明 "
                f"minimum_plugin_version >= "
                f"{'.'.join(str(part) for part in required)}"
            )
        if contract["version"] == 4:
            validate_preset_dimensions(
                {"preset_dimensions": contract["preset_dimensions"]}
            )
            knowledge = contract["knowledge_boundary"]
            content = contract["content_boundary"]
            if str(knowledge.get("policy") or "strict") not in {
                "strict", "guided", "open"
            }:
                raise ValueError("knowledge_boundary.policy 必须是 strict/guided/open")
            if bool(content.get("player_may_relax", False)):
                raise ValueError("世界包内容硬边界不得允许玩家放宽")
    if contract["version"] >= 5:
        minimum = _version_tuple(contract.get("minimum_plugin_version"))
        if minimum is None or minimum < (0, 11, 0):
            raise ValueError(
                "世界包协议 v5 必须声明 minimum_plugin_version >= 0.11.0"
            )
        protocol = contract["protocol"]
        if int(protocol.get("core_version", 0)) != 5:
            raise ValueError("世界包协议 v5 必须声明 protocol.core_version = 5")
        declared = protocol.get("features", {})
        for feature, raw_version in declared.items():
            supported = FEATURE_VERSIONS.get(str(feature))
            if supported is None:
                raise ValueError(f"插件不支持功能协议：{feature}")
            parsed = _version_tuple(raw_version)
            if parsed is None or parsed > (_version_tuple(supported) or (0, 0, 0)):
                raise ValueError(f"插件不支持 {feature}@{raw_version}")
        for requirement in contract["required_features"]:
            feature, minimum_feature = _feature_requirement(requirement)
            if feature not in declared:
                raise ValueError(f"缺少必需功能声明：{feature}")
            actual = _version_tuple(declared[feature])
            if actual is None or actual < minimum_feature:
                raise ValueError(f"功能版本不足：{requirement}")
        registry = EntityRegistry(world)
        for module_name in (
            "resources",
            "runtime_effects",
            "objects",
            "resolution_methods",
            "action_types",
        ):
            module_data = module_value(world, module_name, None)
            feature_name = "action_intents" if module_name == "action_types" else module_name
            if module_data not in (None, {}, []) and feature_name not in declared:
                raise ValueError(
                    f"声明 {module_name} 数据时必须启用 {feature_name} 功能版本"
                )
        resources_module = module_value(world, "resources", {})
        if isinstance(resources_module, Mapping):
            definitions = resources_module.get("definitions", resources_module.get("items", []))
            if isinstance(definitions, Sequence) and not isinstance(definitions, (str, bytes)):
                for definition in definitions:
                    if not isinstance(definition, Mapping):
                        raise ValueError("resources.definitions 每项必须是对象")
                    scope = str(definition.get("persistence_scope") or "session")
                    if scope not in PERSISTENCE_SCOPES:
                        raise ValueError(f"资源持久化作用域无效：{scope}")
                    if "initial_value" in definition:
                        try:
                            initial = float(definition["initial_value"])
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ValueError("资源 initial_value 必须是数值") from exc
                        value_range = definition.get("range")
                        if isinstance(value_range, Mapping):
                            minimum = value_range.get("min")
                            maximum = value_range.get("max")
                            if minimum is not None and initial < float(minimum):
                                raise ValueError("资源 initial_value 低于 range.min")
                            if maximum is not None and initial > float(maximum):
                                raise ValueError("资源 initial_value 超过 range.max")
        interaction = module_value(world, "interaction_rules", {})
        if isinstance(interaction, Mapping) and interaction.get("enabled"):
            if "interaction_rules" not in declared:
                raise ValueError("启用 interaction_rules 时必须声明对应功能版本")
            rules = interaction.get("rules", [])
            if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
                raise ValueError("interaction_rules.rules 必须是数组")
            ids: set[str] = set()
            for item in rules:
                if not isinstance(item, Mapping):
                    raise ValueError("interaction_rules.rules 每项必须是对象")
                rule_id = str(item.get("rule_id") or "")
                if not rule_id or rule_id in ids:
                    raise ValueError(f"交互规则 ID 缺失或重复：{rule_id or '<empty>'}")
                ids.add(rule_id)
                mode_value = str(item.get("mode") or "mechanical")
                if mode_value not in {"mechanical", "narrative", "hybrid"}:
                    raise ValueError(f"交互规则模式无效：{mode_value}")
                stacking = item.get("stacking")
                if isinstance(stacking, Mapping):
                    strategy = str(stacking.get("strategy") or "priority_only")
                    if strategy not in STACKING_STRATEGIES:
                        raise ValueError(f"交互规则叠加策略无效：{strategy}")
                for trigger in item.get("triggers", []):
                    if isinstance(trigger, Mapping) and str(trigger.get("phase") or "before_resolution") not in EVENT_PHASES:
                        raise ValueError(f"交互规则事件阶段无效：{trigger.get('phase')}")
                for effect in item.get("effects", []):
                    if isinstance(effect, Mapping) and str(effect.get("op") or "") not in OPERATION_TYPES:
                        raise ValueError(f"交互规则操作无效：{effect.get('op')}")
        capabilities_module = module_value(world, "capabilities", {})
        if isinstance(capabilities_module, Mapping) and capabilities_module:
            if "capabilities" not in declared:
                raise ValueError("声明 capabilities 数据时必须启用对应功能版本")
            cycles = CapabilityService(world, registry).progression_cycles()
            if cycles:
                raise ValueError("能力成长关系存在循环：" + " -> ".join(cycles[0]))
            operation_engine = OperationEngine(registry)
            transitions = capabilities_module.get("transitions", [])
            if isinstance(transitions, Sequence) and not isinstance(transitions, (str, bytes)):
                transition_ids: set[str] = set()
                for transition in transitions:
                    if not isinstance(transition, Mapping):
                        raise ValueError("capabilities.transitions 每项必须是对象")
                    transition_id = str(transition.get("transition_id") or "")
                    if not transition_id or transition_id in transition_ids:
                        raise ValueError(f"能力转换 ID 缺失或重复：{transition_id or '<empty>'}")
                    transition_ids.add(transition_id)
                    for source_ref in transition.get("from", []):
                        registry.resolve(source_ref, "capability")
                    operation_engine.validate(transition.get("operations", []))
        experience_module = module_value(world, "chat_experience", {})
        if isinstance(experience_module, Mapping) and experience_module:
            if "chat_experience" not in declared:
                raise ValueError(
                    "声明 chat_experience 数据时必须启用对应功能版本"
                )
            validate_chat_experience(world)
    resolution_mode = contract["resolution"]["mode"]
    if mode == "none" and resolution_mode == "attribute":
        raise ValueError("无数值世界不能启用 attribute 属性检定")
    if mode == "none" and contract["attributes"]:
        raise ValueError("stats.mode=none 时不得声明角色属性")
    for risk, raw_dc in contract["resolution"]["difficulty_policy"].items():
        if raw_dc is None and risk == "safe":
            continue
        try:
            dc = int(raw_dc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"difficulty_policy.{risk} 必须是整数") from exc
        if contract["version"] <= 4 and not 5 <= dc <= 25:
            raise ValueError(f"difficulty_policy.{risk} 必须介于 5—25")
    outcome_policy = contract["resolution"]["outcome_policy"]
    try:
        critical_margin = int(outcome_policy["critical_success_margin"])
        cost_floor = int(outcome_policy["cost_success_min_margin"])
        failure_floor = int(outcome_policy["failure_min_margin"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("outcome_policy 的结果档位必须是整数") from exc
    if critical_margin < 1 or not failure_floor < cost_floor < 0:
        raise ValueError("outcome_policy 的结果档位顺序无效")
    capabilities = contract["capabilities"]
    expected = {
        "character_stats": mode != "none",
        "attribute_checks": resolution_mode == "attribute",
        "dice_resolution": resolution_mode in {"dice_only", "attribute"},
    }
    for key, value in expected.items():
        if key in capabilities and bool(capabilities[key]) != value:
            raise ValueError(f"capabilities.{key} 与实际规则冲突")
    return contract


def attribute_lookup(
    contract: Mapping[str, Any],
    reference: str,
) -> tuple[str, str] | None:
    value = str(reference or "").strip().casefold()
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        label = str(item.get("label") or key)
        if value in {key.casefold(), label.casefold(), f"{label}检定".casefold()}:
            return key, label
    return None


__all__ = [
    "FEATURE_VERSIONS",
    "SUPPORTED_WORLD_SCHEMA_VERSIONS",
    "WORLD_SCHEMA_VERSION",
    "attribute_lookup",
    "stats_mode",
    "validate_world_contract",
    "world_contract",
]
