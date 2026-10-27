from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


TYPED_REF_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}:[a-z0-9][a-z0-9_.-]{0,127}$")

ENTITY_TYPES = frozenset(
    {
        "actor", "character", "npc", "object", "location", "organization",
        "clue", "capability", "resource", "stat", "trait", "relationship",
        "runtime_effect", "event", "action_type", "resolution_method", "counter",
        "story_flag", "custom", "custom_tag", "location_route", "capability_type",
    }
)


def module_value(world: Mapping[str, Any], key: str, default: Any = None) -> Any:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    if key in world:
        return world[key]
    return rules.get(key, default)


def typed_ref(entity_type: object, entity_id: object) -> str:
    value = f"{str(entity_type or '').strip()}:{str(entity_id or '').strip()}"
    if not TYPED_REF_RE.fullmatch(value):
        raise ValueError(f"非法类型化引用：{value}")
    return value


def split_ref(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not TYPED_REF_RE.fullmatch(text):
        raise ValueError(f"非法类型化引用：{text or '<empty>'}")
    return tuple(text.split(":", 1))  # type: ignore[return-value]


@dataclass(frozen=True)
class EntityDefinition:
    ref: str
    entity_type: str
    entity_id: str
    label: str
    definition: dict[str, Any]


class EntityRegistry:
    """World-scoped registry for stable, typed references.

    The registry deliberately never follows arbitrary object/database paths.
    Only definitions declared by the active frozen world snapshot are visible.
    """

    def __init__(self, world: Mapping[str, Any]) -> None:
        self.world = dict(world)
        self._items: dict[str, EntityDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._register_world()

    def _register(self, entity_type: str, raw: Mapping[str, Any]) -> None:
        entity_id = str(
            raw.get(f"{entity_type}_id")
            or (raw.get("method_id") if entity_type == "resolution_method" else None)
            or raw.get("entity_id")
            or raw.get("id")
            or raw.get("slug")
            or ""
        ).strip()
        if not entity_id:
            raise ValueError(f"{entity_type} 定义缺少稳定 id")
        ref = typed_ref(entity_type, entity_id)
        if ref in self._items:
            raise ValueError(f"重复实体引用：{ref}")
        label = str(raw.get("label") or raw.get("name") or entity_id)
        self._items[ref] = EntityDefinition(
            ref=ref,
            entity_type=entity_type,
            entity_id=entity_id,
            label=label,
            definition=dict(raw),
        )

    @staticmethod
    def _iter_definitions(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            if any(key in value for key in ("id", "entity_id", "slug")):
                return [value]
            return [
                {"id": str(key), **dict(item)}
                for key, item in value.items()
                if isinstance(item, Mapping)
            ]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [item for item in value if isinstance(item, Mapping)]
        return []

    def _register_world(self) -> None:
        generic = module_value(self.world, "entity_registry", {})
        if isinstance(generic, Mapping):
            entries = generic.get("entities", [])
        else:
            entries = []
        for entry in self._iter_definitions(entries):
            entity_type = str(entry.get("entity_type") or entry.get("type") or "custom")
            self._register(entity_type, entry)

        sources = {
            "capability": ("capabilities", ("definitions", "items")),
            "resource": ("resources", ("definitions", "items")),
            "runtime_effect": ("runtime_effects", ("definitions", "items")),
            "object": ("objects", ("definitions", "items")),
            "resolution_method": ("resolution_methods", ("methods", "definitions", "items")),
            "action_type": ("action_types", ("definitions", "items")),
            "capability_type": ("capability_types", ("definitions", "items")),
        }
        for entity_type, (module_key, containers) in sources.items():
            raw_module = module_value(self.world, module_key, [])
            payload = raw_module
            if isinstance(raw_module, Mapping):
                for container in containers:
                    if container in raw_module:
                        payload = raw_module[container]
                        break
            for definition in self._iter_definitions(payload):
                self._register(entity_type, definition)

        aliases = module_value(self.world, "id_aliases", {})
        if isinstance(aliases, Mapping):
            for old_ref, new_ref in aliases.items():
                old_text, new_text = str(old_ref).strip(), str(new_ref).strip()
                split_ref(old_text)
                split_ref(new_text)
                if old_text in self._items:
                    raise ValueError(f"ID 别名来源与现有实体冲突：{old_text}")
                if new_text not in self._items:
                    raise ValueError(f"ID 别名目标不存在：{new_text}")
                self._aliases[old_text] = new_text

    def resolve(self, value: object, expected_type: str | None = None) -> EntityDefinition:
        ref = str(value or "").strip()
        split_ref(ref)
        ref = self._aliases.get(ref, ref)
        item = self._items.get(ref)
        if item is None:
            raise KeyError(f"引用不存在：{ref}")
        if expected_type and item.entity_type != expected_type:
            raise TypeError(f"引用类型不匹配：期望 {expected_type}，实际 {item.entity_type}")
        return item

    def contains(self, value: object) -> bool:
        try:
            self.resolve(value)
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def items(self, entity_type: str | None = None) -> list[EntityDefinition]:
        return [
            item for item in self._items.values()
            if entity_type is None or item.entity_type == entity_type
        ]

    def export(self) -> list[dict[str, Any]]:
        return [
            {
                "ref": item.ref,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "label": item.label,
                "definition": dict(item.definition),
            }
            for item in sorted(self._items.values(), key=lambda value: value.ref)
        ]


__all__ = [
    "ENTITY_TYPES", "EntityDefinition", "EntityRegistry", "TYPED_REF_RE",
    "module_value", "split_ref", "typed_ref",
]
