"""Resource types that can be granted to user groups.

A *resource type* identifies a class of entity admins can hand out access to
(e.g. marketplace plugins, datasets). Concrete instances live in their own
domain tables (`marketplace_plugins`, `table_registry`, …); access to a
specific instance is recorded as a row in `resource_grants` with this enum
value as ``resource_type`` and a module-defined path string as ``resource_id``.

Adding a new type:
  1. Add a member to the ``ResourceType`` enum.
  2. Add an entry to ``RESOURCE_TYPE_META`` with display copy + id_format hint.
  3. Wire your endpoints with ``Depends(require_resource_access(ResourceType.X, "<path>"))``.

No DB migration needed — this is an application-level constant. Membership in
the enum is the source of truth; the DB just stores the string value verbatim.
"""

from __future__ import annotations

from enum import StrEnum


class ResourceType(StrEnum):
    """Resource categories that the access-control layer understands.

    Values are persisted verbatim in ``resource_grants.resource_type``.
    Renaming a member is a breaking change — existing grants reference the
    string. Add a new member and migrate via SQL UPDATE if needed.
    """

    MARKETPLACE_PLUGIN = "marketplace_plugin"


RESOURCE_TYPE_META: dict[ResourceType, dict[str, str]] = {
    ResourceType.MARKETPLACE_PLUGIN: {
        "display_name": "Marketplace Plugin",
        "description": "A plugin from a registered marketplace.",
        "id_format": "<marketplace_slug>/<plugin_name>",
    },
}


def list_resource_types() -> list[dict[str, str]]:
    """Flat list for admin UI: ``[{key, display_name, description, id_format}]``."""
    return [
        {"key": rt.value, **meta}
        for rt, meta in RESOURCE_TYPE_META.items()
    ]
