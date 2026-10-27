from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .lifecycle import card_template
from .world_contract import world_contract


def _ids(items: Any, *fields: str) -> set[str]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return set()
    result: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for field in fields:
            value = str(item.get(field) or "").strip()
            if value:
                result.add(value)
                break
    return result


def compare_world_contracts(
    frozen_world: Mapping[str, Any],
    candidate_world: Mapping[str, Any],
) -> dict[str, Any]:
    """Report whether a frozen instance may be cloned onto a newer world."""

    old_contract = world_contract(frozen_world)
    new_contract = world_contract(candidate_world)
    old_template = card_template(frozen_world)
    new_template = card_template(candidate_world)

    old_attributes = _ids(old_contract.get("attributes"), "key", "id")
    new_attributes = _ids(new_contract.get("attributes"), "key", "id")
    old_presets = _ids(old_template.get("profession_presets"), "id", "key", "name")
    new_presets = _ids(new_template.get("profession_presets"), "id", "key", "name")
    old_fields = _ids(old_template.get("fields"), "key")
    new_fields = _ids(new_template.get("fields"), "key")

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for kind, removed in (
        ("attribute", old_attributes - new_attributes),
        ("profession", old_presets - new_presets),
        ("card_field", old_fields - new_fields),
    ):
        if removed:
            blockers.append(
                {
                    "code": f"removed_{kind}",
                    "message": f"候选世界删除了正在使用的 {kind} ID",
                    "ids": sorted(removed),
                }
            )
    if old_contract["stats"]["mode"] != new_contract["stats"]["mode"]:
        blockers.append(
            {
                "code": "stats_mode_changed",
                "message": "数值模式发生变化，现有角色卡不能直接热迁移",
                "from": old_contract["stats"]["mode"],
                "to": new_contract["stats"]["mode"],
            }
        )
    if old_contract["resolution"]["mode"] != new_contract["resolution"]["mode"]:
        warnings.append(
            {
                "code": "resolution_mode_changed",
                "message": "裁定模式发生变化，建议克隆副本后试运行",
                "from": old_contract["resolution"]["mode"],
                "to": new_contract["resolution"]["mode"],
            }
        )

    return {
        "safe_for_live_replace": False,
        "safe_for_clone": not blockers,
        "recommended_action": (
            "clone_and_apply" if not blockers else "keep_frozen_contract"
        ),
        "blockers": blockers,
        "warnings": warnings,
        "added": {
            "attributes": sorted(new_attributes - old_attributes),
            "professions": sorted(new_presets - old_presets),
            "card_fields": sorted(new_fields - old_fields),
        },
        "policy": "运行中的副本永远不直接热更新世界契约",
    }

