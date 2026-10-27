from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ExtensionRegistry:
    """Named extension points that keep custom code outside world JSON."""

    _KINDS = frozenset(
        {
            "dice_system",
            "check_resolver",
            "world_validator",
            "narrative_guard",
            "summary_provider",
            "admin_action",
            "element_resolver",
        }
    )

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Callable[..., Any]]] = {
            kind: {} for kind in self._KINDS
        }

    def register(self, kind: str, name: str, provider: Callable[..., Any]) -> None:
        if kind not in self._KINDS:
            raise ValueError(f"不支持的扩展类型：{kind}")
        key = str(name or "").strip().lower()
        if not key or not key.replace("-", "_").isalnum():
            raise ValueError("扩展名称只能包含字母、数字、下划线或连字符")
        if not callable(provider):
            raise TypeError("扩展实现必须可调用")
        if key in self._entries[kind]:
            raise ValueError(f"扩展已注册：{kind}/{key}")
        self._entries[kind][key] = provider

    def resolve(self, kind: str, name: str) -> Callable[..., Any] | None:
        return self._entries.get(kind, {}).get(str(name or "").strip().lower())

    def list(self, kind: str | None = None) -> dict[str, tuple[str, ...]]:
        kinds = (kind,) if kind else tuple(sorted(self._entries))
        return {
            item: tuple(sorted(self._entries.get(item, {})))
            for item in kinds
            if item in self._entries
        }

    def register_dice_system(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("dice_system", name, provider)

    def register_check_resolver(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("check_resolver", name, provider)

    def register_world_validator(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("world_validator", name, provider)

    def register_narrative_guard(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("narrative_guard", name, provider)

    def register_summary_provider(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("summary_provider", name, provider)

    def register_admin_action(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("admin_action", name, provider)

    def register_element_resolver(self, name: str, provider: Callable[..., Any]) -> None:
        self.register("element_resolver", name, provider)
