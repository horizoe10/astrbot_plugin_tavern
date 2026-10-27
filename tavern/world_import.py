"""World-package import and editor payload helpers.

The admin console exposes a richer runtime view than the persistent world
package.  Keep the two shapes separate: package extensions must survive an
import/edit/save round trip, while computed console fields must never be
written back as extensions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_RUNTIME_ONLY_FIELDS = {
    "card_template",
    "character_count",
    "characters",
    "check_density",
    "choice_mode",
    "player_limits",
    "time_rules",
}
_DATABASE_MANAGED_FIELDS = {
    "created_at",
    "updated_at",
    # These values belong to the local catalog.  They are intentionally not
    # part of a portable package hash: importing a package must never steal a
    # destination catalog's stable display number or administrator ordering.
    "display_no",
    "sort_order",
}


def _world_package_fields(
    payload: Mapping[str, Any],
    *,
    include_identity: bool,
) -> dict[str, Any]:
    """Return persistent package fields without lossy allow-listing."""

    blocked = set(_RUNTIME_ONLY_FIELDS) | set(_DATABASE_MANAGED_FIELDS)
    if not include_identity:
        blocked.update({"id", "revision", "archived"})
    result = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in blocked
    }
    rules = result.get("rules")
    initial_state = result.get("initial_state")
    result["rules"] = dict(rules) if isinstance(rules, Mapping) else {}
    result["initial_state"] = (
        dict(initial_state) if isinstance(initial_state, Mapping) else {}
    )
    return result


def world_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an import payload without dropping future package extensions."""

    result = _world_package_fields(payload, include_identity=False)
    # Keep explicit defaults for the stable database contract.
    result.setdefault("description", "")
    result.setdefault("opening_scene", "")
    result.setdefault("world_schema_version", 0)
    rules = result.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    # The database exposes this legacy/top-level mirror for every world.  Use
    # the declared rules module as the canonical default so content hashes are
    # stable without ever replacing a real module with an empty object.
    result.setdefault("capabilities", dict(rules.get("capabilities") or {}))
    result.setdefault("minimum_plugin_version", "")
    return result


def world_edit_payload(
    payload: Mapping[str, Any],
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a console edit with its stored package envelope.

    The visual editor intentionally submits only editable fields.  Contract
    metadata such as ``minimum_plugin_version`` and author-defined extension
    fields therefore come from the current stored world unless explicitly
    replaced by the request.
    """

    result = (
        _world_package_fields(current, include_identity=True)
        if isinstance(current, Mapping)
        else {}
    )
    result.update(_world_package_fields(payload, include_identity=True))
    return result


__all__ = ["world_edit_payload", "world_import_payload"]
