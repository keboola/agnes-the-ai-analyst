"""Resource types that can be granted to user groups.

A *resource type* identifies a class of entity admins can hand out access to
(e.g. marketplace plugins, datasets). Concrete instances live in their own
domain tables (`marketplace_plugins`, `table_registry`, …); access to a
specific instance is recorded as a row in `resource_grants` with this enum
value as ``resource_type`` and a module-defined path string as ``resource_id``.

Adding a new type — single place, no separate wiring step:

  1. Add a member to :class:`ResourceType`.
  2. Write a ``list_blocks(conn) -> list[Block]`` delegate that projects the
     domain tables into the (block → items) tree the admin /access page
     consumes. Each item must include ``resource_id`` matching the path
     string used in ``resource_grants.resource_id``.
  3. Register a :class:`ResourceTypeSpec` in :data:`RESOURCE_TYPES`. The
     dataclass requires ``list_blocks`` — the type checker forces step 2.
  4. Wire endpoints with
     ``Depends(require_resource_access(ResourceType.X, "<path>"))``.

No DB migration needed — this is application-level configuration. Membership
in the enum + registry is the source of truth; the DB just stores the string
value verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    import duckdb


class ResourceType(StrEnum):
    """Resource categories that the access-control layer understands.

    Values are persisted verbatim in ``resource_grants.resource_type``.
    Renaming a member is a breaking change — existing grants reference the
    string. Add a new member and migrate via SQL UPDATE if needed.
    """

    MARKETPLACE_PLUGIN = "marketplace_plugin"
    TABLE = "table"


# Shape returned by ``list_blocks`` delegates. Kept as plain ``dict`` to keep
# the registry decoupled from any specific ORM/repo type — UI consumes JSON.
Block = dict[str, Any]
ListBlocksFn = Callable[["duckdb.DuckDBPyConnection"], List[Block]]


@dataclass(frozen=True)
class ResourceTypeSpec:
    """Self-contained definition of a resource type.

    Bundles UI copy with the projection delegate so that adding a new type
    in :data:`RESOURCE_TYPES` is the single place that needs editing — no
    forgotten branch in ``access-overview`` or the admin UI.

    Attributes:
        key: The enum member; ``key.value`` is what gets persisted.
        display_name: Plural label rendered as a section header on the
            admin /access page.
        description: One-liner shown in the create-grant form's helper text.
        id_format: Human-readable hint for ``resource_id`` shape — e.g.
            ``"<marketplace_slug>/<plugin_name>"``. Surfaced as input
            placeholder.
        list_blocks: Delegate that takes a system DB connection and returns
            ``[{id, name, items: [{resource_id, name, ...}]}]`` — one block
            per parent entity (e.g. marketplace), one item per grantable
            resource (e.g. plugin). Items must carry ``resource_id`` that
            matches the path string written into ``resource_grants``.
    """

    key: ResourceType
    display_name: str
    description: str
    id_format: str
    list_blocks: ListBlocksFn


# ---------------------------------------------------------------------------
# Marketplace plugin projection
# ---------------------------------------------------------------------------


def _marketplace_plugin_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project marketplace_registry + marketplace_plugins into the
    hierarchical (block → items) shape the admin UI renders.

    One block per marketplace_registry row, ordered by registered_at.
    Items inside are plugins; ``resource_id`` encodes the canonical path
    ``<marketplace_slug>/<plugin_name>`` that ``resource_grants.resource_id``
    matches against.
    """
    rows = conn.execute(
        """SELECT mr.id, mr.name, mr.registered_at,
                  mp.name AS plugin_name, mp.version, mp.category,
                  mp.description, mp.source_type
           FROM marketplace_registry mr
           LEFT JOIN marketplace_plugins mp ON mp.marketplace_id = mr.id
           ORDER BY mr.registered_at, mr.id, mp.name"""
    ).fetchall()
    blocks: dict[str, Block] = {}
    for mr_id, mr_name, _, p_name, p_ver, p_cat, p_desc, p_src in rows:
        block = blocks.setdefault(mr_id, {
            "id": mr_id,
            "name": mr_name,
            "items": [],
        })
        if p_name:
            block["items"].append({
                "resource_id": f"{mr_id}/{p_name}",
                "name": p_name,
                "version": p_ver,
                "category": p_cat,
                "description": p_desc,
                "source_type": p_src,
            })
    return list(blocks.values())


# ---------------------------------------------------------------------------
# Table projection
# ---------------------------------------------------------------------------


def _table_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project table_registry into the (block → items) shape the admin UI
    renders.

    One block per ``bucket`` value, ordered by bucket then table name.
    Items inside are tables; ``resource_id`` is the ``table_registry.id``
    primary key — that is the path string that ``resource_grants.resource_id``
    matches against when enforcement lands. Bucket is purely a UI grouping
    and does not enter the resource_id (mirrors the marketplace/plugin
    pattern, where the marketplace itself is not a grantable resource).

    Tables with NULL/empty bucket fall into a synthetic ``"(no bucket)"``
    block so they are still grantable.
    """
    rows = conn.execute(
        """SELECT id, name, bucket, source_type, query_mode, description
           FROM table_registry
           ORDER BY COALESCE(bucket, ''), name"""
    ).fetchall()
    blocks: dict[str, Block] = {}
    for tbl_id, name, bucket, source_type, query_mode, description in rows:
        block_key = bucket if bucket else "(no bucket)"
        block = blocks.setdefault(block_key, {
            "id": block_key,
            "name": block_key,
            "items": [],
        })
        block["items"].append({
            "resource_id": tbl_id,
            "name": name,
            "category": query_mode,
            "source_type": source_type,
            "description": description,
        })
    return list(blocks.values())


# ---------------------------------------------------------------------------
# Registry — the one place that gets edited when adding a new resource type
# ---------------------------------------------------------------------------


RESOURCE_TYPES: dict[ResourceType, ResourceTypeSpec] = {
    ResourceType.MARKETPLACE_PLUGIN: ResourceTypeSpec(
        key=ResourceType.MARKETPLACE_PLUGIN,
        display_name="Marketplace plugins",
        description="A plugin from a registered marketplace.",
        id_format="<marketplace_slug>/<plugin_name>",
        list_blocks=_marketplace_plugin_blocks,
    ),
    ResourceType.TABLE: ResourceTypeSpec(
        key=ResourceType.TABLE,
        display_name="Tables",
        description="A registered data table.",
        id_format="<table_id>",
        list_blocks=_table_blocks,
    ),
}


def list_resource_types() -> list[dict[str, str]]:
    """Flat projection for /api/admin/resource-types.

    Shape: ``[{key, display_name, description, id_format}]``. The
    ``list_blocks`` delegate is intentionally omitted — the UI consumes
    blocks via ``/api/admin/access-overview`` instead.
    """
    return [
        {
            "key": spec.key.value,
            "display_name": spec.display_name,
            "description": spec.description,
            "id_format": spec.id_format,
        }
        for spec in RESOURCE_TYPES.values()
    ]
