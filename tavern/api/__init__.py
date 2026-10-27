"""Stable, permission-preserving extension surface for AI Tavern."""

from .hooks import HookRegistry
from .public import TavernPublicAPI
from .registry import ExtensionRegistry
from ..stat_generation import (
    assess_preset_stack_migration,
    calculate_preset_stack_stats,
    validate_stat_generation_config,
)

__all__ = [
    "ExtensionRegistry",
    "HookRegistry",
    "TavernPublicAPI",
    "assess_preset_stack_migration",
    "calculate_preset_stack_stats",
    "validate_stat_generation_config",
]
