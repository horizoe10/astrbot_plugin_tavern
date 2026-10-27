from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .card_wizard import preset_options
from .lifecycle import (
    card_stat_allocation,
    card_template,
    normalize_choices,
    opening_choices,
    resolve_profession_stats,
    validate_card_template_config,
)
from .world_contract import WORLD_SCHEMA_VERSION, validate_world_contract
from .elemental import validate as validate_elemental
from .entity_registry import EntityRegistry
from .capability_service import CapabilityService
from .stat_generation import (
    calculate_preset_stack_stats,
    stat_generation_config,
    validate_stat_generation_config,
)
from .chat_experience import normalize_chat_experience, validate_chat_experience


def _issue(
    level: str,
    path: str,
    code: str,
    message: str,
    *,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "path": path,
        "code": code,
        "message": message,
        "detail": dict(detail or {}),
    }


def _duplicates(items: Sequence[Any], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get(field) or "").strip()
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _minimal_fields(template: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in template.get("fields", []):
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        if not key or field.get("type") == "derived":
            continue
        if field.get("type") == "integer":
            result[key] = int(field.get("minimum", 0) or 0)
        elif field.get("type") == "boolean":
            result[key] = False
        elif field.get("type") == "multi_select":
            result[key] = []
        else:
            result[key] = f"测试{field.get('label') or key}"
    result["name"] = "测试角色"
    result["code"] = "test-card"
    return result


def inspect_world_package(world: Mapping[str, Any]) -> dict[str, Any]:
    """Return a non-mutating, structured compatibility and dry-run report."""

    issues: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    contract: dict[str, Any] = {}
    template: dict[str, Any] = {}
    preset_stack_combinations = 0

    if not isinstance(world, Mapping):
        return {
            "compatible": False,
            "summary": {},
            "issues": [
                _issue("error", "$", "not_an_object", "世界包必须是 JSON 对象")
            ],
            "tests": [],
        }

    for key in ("slug", "name", "system_prompt"):
        if not str(world.get(key) or "").strip():
            issues.append(
                _issue("error", key, "required", f"缺少必填字段：{key}")
            )

    try:
        contract = validate_world_contract(world)
        tests.append({"name": "世界契约标准化", "status": "passed"})
    except Exception as exc:
        issues.append(_issue("error", "rules", "invalid_contract", str(exc)))
        tests.append({"name": "世界契约标准化", "status": "failed"})

    try:
        embedded_rules = world.get("rules")
        embedded_rules = embedded_rules if isinstance(embedded_rules, Mapping) else {}
        raw_version = int(
            world.get(
                "world_schema_version",
                embedded_rules.get("world_schema_version", 0),
            )
            or 0
        )
    except (TypeError, ValueError):
        raw_version = 0
        issues.append(
            _issue(
                "error",
                "world_schema_version",
                "invalid_schema_version",
                "世界包协议版本必须是整数",
            )
        )
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw_card = rules.get("character_card")
    raw_card = raw_card if isinstance(raw_card, Mapping) else {}
    try:
        template = card_template(world)
        validate_card_template_config(
            raw_card if raw_card.get("fields") else template
        )
        tests.append({"name": "建卡流程生成", "status": "passed"})
    except Exception as exc:
        issues.append(
            _issue(
                "error",
                "rules.character_card",
                "invalid_card_template",
                str(exc),
            )
        )
        tests.append({"name": "建卡流程生成", "status": "failed"})

    if template:
        fields = template.get("fields", [])
        attributes = template.get("stats", {}).get("attributes", [])
        presets = template.get("profession_presets", [])
        for path, items, field in (
            ("rules.character_card.fields", fields, "key"),
            ("rules.character_card.stats.attributes", attributes, "key"),
            ("rules.character_card.profession_presets", presets, "id"),
        ):
            for duplicate in _duplicates(items, field):
                issues.append(
                    _issue(
                        "error",
                        path,
                        f"duplicate_{field}",
                        f"存在重复 {field}：{duplicate}",
                    )
                )

        minimal = _minimal_fields(template)
        mode = str(template.get("stats", {}).get("mode") or "manual")
        if mode == "preset_stack":
            try:
                validation = validate_stat_generation_config(template)
                preset_stack_combinations = int(
                    validation.get("combination_count", 0)
                )
                generation = stat_generation_config(template)
                field_index = {
                    str(item.get("key") or ""): item
                    for item in fields
                    if isinstance(item, Mapping)
                }
                for source_id in generation.get("bonus_sources", []):
                    source_field = field_index.get(str(source_id), {})
                    options = preset_options(template, source_field, minimal)
                    if options:
                        minimal[str(source_id)] = str(
                            options[0].get("id")
                            or options[0].get("value")
                            or ""
                        )
                calculate_preset_stack_stats(
                    template,
                    minimal,
                    require_complete=True,
                )
                tests.append(
                    {
                        "name": "多预设属性全部组合",
                        "status": "passed",
                        "detail": {
                            "combination_count": validation[
                                "combination_count"
                            ]
                        },
                    }
                )
            except Exception as exc:
                issues.append(
                    _issue(
                        "error",
                        "rules.character_card.stat_generation",
                        "preset_stack_dry_run_failed",
                        str(exc),
                    )
                )
                tests.append(
                    {"name": "多预设属性全部组合", "status": "failed"}
                )
        elif mode == "preset" and presets:
            preset = next((x for x in presets if isinstance(x, Mapping)), None)
            if preset:
                selector = template.get("stats", {}).get("preset_selector", {})
                selector_field = str(selector.get("field") or "profession")
                minimal[selector_field] = str(
                    preset.get("id") or preset.get("name") or ""
                )
                attribute_defs = template.get("stats", {}).get("attributes", [])
                labels = [
                    str(x.get("key") or x.get("label") or "")
                    for x in attribute_defs
                    if isinstance(x, Mapping)
                ]
                if labels:
                    minimal["primary_attribute"] = labels[0]
                if len(labels) > 1:
                    minimal["secondary_attribute"] = labels[1]
                try:
                    resolve_profession_stats(template, minimal, require_complete=True)
                    tests.append({"name": "职业预设完整角色卡", "status": "passed"})
                except Exception as exc:
                    issues.append(
                        _issue(
                            "error",
                            "rules.character_card.profession_presets",
                            "preset_dry_run_failed",
                            str(exc),
                        )
                    )
                    tests.append({"name": "职业预设完整角色卡", "status": "failed"})
        else:
            allocation = card_stat_allocation(template, minimal)
            tests.append(
                {
                    "name": "最小角色卡数值流程",
                    "status": "passed",
                    "detail": {"mode": allocation.get("mode")},
                }
            )

    try:
        choices = opening_choices(world)
        normalize_choices(choices, world)
        tests.append({"name": "开场 A—D", "status": "passed"})
    except Exception as exc:
        issues.append(
            _issue(
                "error",
                "rules.opening_choices",
                "invalid_opening_choices",
                str(exc),
            )
        )
        tests.append({"name": "开场 A—D", "status": "failed"})

    resolution = contract.get("resolution", {}) if contract else {}
    stats = contract.get("stats", {}) if contract else {}
    capabilities = contract.get("capabilities", {}) if contract else {}
    if not world.get("description"):
        issues.append(
            _issue(
                "warning",
                "description",
                "missing_description",
                "未填写面向玩家的世界简介",
            )
        )

    entity_count = 0
    feature_versions: dict[str, str] = {}
    if contract and int(contract.get("version", 0)) >= 5:
        try:
            registry = EntityRegistry(world)
            entity_count = len(registry.items())
            feature_versions = dict(contract.get("protocol", {}).get("features", {}))
            tests.append(
                {"name": "v5 实体引用注册", "status": "passed",
                 "detail": {"entity_count": entity_count}}
            )
            cycles = CapabilityService(world, registry).progression_cycles()
            if cycles:
                raise ValueError("能力成长关系存在循环：" + " -> ".join(cycles[0]))
            tests.append({"name": "能力成长循环检查", "status": "passed"})
        except Exception as exc:
            issues.append(_issue("error", "protocol", "v5_registry_invalid", str(exc)))
            tests.append({"name": "v5 实体与能力图", "status": "failed"})
    if len(str(world.get("system_prompt") or "")) > 30000:
        issues.append(
            _issue(
                "warning",
                "system_prompt",
                "large_prompt",
                "核心世界设定超过 30000 字，可能显著增加每轮 Token 消耗",
            )
        )

    try:
        elemental_issues = validate_elemental(world)
        if elemental_issues:
            issues.extend(elemental_issues)
    except Exception:
        pass

    experience = normalize_chat_experience(world)
    if experience.get("enabled"):
        try:
            validate_chat_experience(world)
            tests.append(
                {
                    "name": "多人群聊体验策略",
                    "status": "passed",
                    "detail": {
                        "character_creation": experience["character_creation"],
                        "proactive_fallback": experience["delivery"]["proactive_fallback"],
                    },
                }
            )
        except Exception as exc:
            issues.append(
                _issue(
                    "error",
                    "rules.chat_experience",
                    "invalid_chat_experience",
                    str(exc),
                )
            )
            tests.append({"name": "多人群聊体验策略", "status": "failed"})

    # A16：可选经济块校验（仅警告，不阻断导入）
    try:
        economy = (world.get("rules") or {}).get("economy") if isinstance(world.get("rules"), dict) else None
        if isinstance(economy, dict) and economy:
            currency_ids = [
                str(c.get("currency_id") or c.get("id") or "")
                for c in economy.get("currencies") or []
                if isinstance(c, dict)
            ]
            for currency in economy.get("currencies") or []:
                if not isinstance(currency, dict):
                    issues.append(_issue("warning", "rules.economy", "economy.currency_not_object", "货币定义必须是对象"))
                    continue
                cid = str(currency.get("currency_id") or currency.get("id") or "").strip()
                if not cid:
                    issues.append(_issue("warning", "rules.economy", "economy.currency_missing_id", "货币缺少 currency_id"))
                if not str(currency.get("name") or "").strip():
                    issues.append(_issue("warning", "rules.economy", "economy.currency_missing_name", f"货币 {cid or '?'} 缺少显示名称 name"))
                try:
                    precision = int(currency.get("precision") or 0)
                    if not 0 <= precision <= 9:
                        issues.append(_issue("warning", "rules.economy", "economy.precision_range", f"货币 {cid} 的 precision 应在 0..9"))
                except (TypeError, ValueError):
                    issues.append(_issue("warning", "rules.economy", "economy.precision_invalid", f"货币 {cid} 的 precision 必须是整数"))
            if len(currency_ids) != len(set(currency_ids)):
                issues.append(_issue("warning", "rules.economy", "economy.duplicate_currency", "货币 currency_id 重复"))
            for rule in economy.get("exchange_rules") or []:
                if not isinstance(rule, dict):
                    continue
                frm = str(rule.get("from") or rule.get("from_currency") or "")
                to = str(rule.get("to") or rule.get("to_currency") or "")
                if frm and frm not in currency_ids:
                    issues.append(_issue("warning", "rules.economy", "economy.unknown_from_currency", f"兑换规则引用未声明货币：{frm}"))
                if to and to not in currency_ids:
                    issues.append(_issue("warning", "rules.economy", "economy.unknown_to_currency", f"兑换规则引用未声明货币：{to}"))
    except Exception:
        pass

    all_issues = issues
    errors = sum(item["level"] == "error" for item in all_issues)
    warnings = sum(item["level"] == "warning" for item in all_issues)
    return {
        "compatible": errors == 0,
        "summary": {
            "schema_version": contract.get("version", raw_version),
            "supported_schema_version": WORLD_SCHEMA_VERSION,
            "field_count": len(template.get("fields", [])) if template else 0,
            "stats_mode": stats.get("mode", "unknown"),
            "preset_stack_combinations": preset_stack_combinations,
            "minimum_plugin_version": contract.get(
                "minimum_plugin_version", ""
            ),
            "attribute_count": len(contract.get("attributes", [])) if contract else 0,
            "resolution_mode": resolution.get("mode", "unknown"),
            "dice_system": resolution.get("dice_system", "none"),
            "danger_levels": len(contract.get("danger_levels", [])) if contract else 0,
            "npc_count": len(world.get("characters", [])) if isinstance(world.get("characters"), list) else 0,
            "capabilities": capabilities,
            "chat_experience": experience,
            "feature_versions": feature_versions,
            "entity_count": entity_count,
            "errors": errors,
            "warnings": warnings,
            "migrations": 0,
        },
        "issues": all_issues,
        "tests": tests,
    }
