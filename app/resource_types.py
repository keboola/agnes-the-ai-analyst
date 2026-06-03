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
    DATA_PACKAGE = "data_package"
    MEMORY_DOMAIN = "memory_domain"
    MEMORY_ITEM = "memory_item"
    RECIPE = "recipe"
    CHAT = "chat"
    SLACK_CHANNEL = "slack_channel"


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
                  mp.description, mp.source_type, mp.is_system
           FROM marketplace_registry mr
           LEFT JOIN marketplace_plugins mp ON mp.marketplace_id = mr.id
           ORDER BY mr.registered_at, mr.id, mp.name"""
    ).fetchall()
    blocks: dict[str, Block] = {}
    for mr_id, mr_name, _, p_name, p_ver, p_cat, p_desc, p_src, p_sys in rows:
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
                # v39: drives the SYSTEM pill + disabled checkbox in
                # /admin/access. The grant row exists for every group on a
                # system plugin (materialized by mark_system) — we just
                # prevent admins from revoking it via the UI to keep the
                # mandatory-tier semantic honest.
                "is_system": bool(p_sys),
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
    matches against. Bucket is purely a UI grouping and does not enter the
    resource_id (mirrors the marketplace/plugin pattern).

    Tables with NULL/empty bucket fall into a synthetic ``"(no bucket)"``
    block so they are still grantable.
    """
    # Filter out source_type='internal' rows (agnes_sessions /
    # agnes_telemetry / agnes_audit). Their RBAC is row-level, enforced
    # in the query path; the table-grain `resource_grants` gate is
    # bypassed for them (see can_access). Surfacing them on
    # /admin/access would let admins assign grants that do nothing,
    # which is exactly the confusion this filter prevents.
    rows = conn.execute(
        """SELECT id, name, bucket, source_type, query_mode, description
           FROM table_registry
           WHERE source_type IS DISTINCT FROM 'internal'
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
# Data package projection
# ---------------------------------------------------------------------------


def _data_package_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project ``data_packages`` into the (block → items) shape rendered by
    the admin /access page.

    Data packages are admin-curated bundles of tables (v49) and form the
    grantable unit on the /catalog Browse + Stack views. One synthetic
    block ``"Data packages"`` holds them; ``resource_id`` is
    ``data_packages.id``.
    """
    rows = conn.execute(
        """SELECT id, slug, name, description, icon, color
           FROM data_packages
           WHERE deleted_at IS NULL
           ORDER BY name"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "data_packages",
        "name": "Data packages",
        "items": [
            {
                "resource_id": r[0],
                "name": r[2],
                "category": "data_package",
                "description": r[3],
                "icon": r[4],
                "color": r[5],
                "slug": r[1],
            }
            for r in rows
        ],
    }]


# ---------------------------------------------------------------------------
# Memory domain projection
# ---------------------------------------------------------------------------


def _memory_domain_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project ``memory_domains`` rows into the (block → items) shape the
    admin /access page renders.

    Pre-v49 the domain set was a fixed hardcoded enum mirroring
    ``VALID_DOMAINS`` in ``app/api/memory.py``. v49 replaces the scalar
    ``knowledge_items.domain`` column with a junction onto a row-backed
    ``memory_domains`` table — admins can now CRUD domains. ``resource_id``
    is the ``memory_domains.id`` (e.g. ``md_finance``), no longer the slug.
    """
    rows = conn.execute(
        """SELECT id, slug, name, description, icon, color
           FROM memory_domains
           WHERE deleted_at IS NULL
           ORDER BY name"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "memory_domains",
        "name": "Memory domains",
        "items": [
            {
                "resource_id": r[0],
                "name": r[2],
                "category": "memory_domain",
                "description": r[3],
                "icon": r[4],
                "color": r[5],
                "slug": r[1],
            }
            for r in rows
        ],
    }]


# ---------------------------------------------------------------------------
# Memory item projection — per-group item-level Required override (v49)
# ---------------------------------------------------------------------------


def _memory_item_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project ``knowledge_items`` into the (block → items) shape for the
    rare per-item grant override.

    Default Required tier for a memory item is driven by
    ``knowledge_items.is_required`` (the global flag). This resource type
    exists for the per-group override: a group can be granted MEMORY_ITEM
    on a specific item with ``requirement='required'`` (force-include) or
    ``'available'`` (force-exclude — counter-acts the global flag for that
    group). Surfaced as a flat list of approved items so admins can pick.
    """
    rows = conn.execute(
        """SELECT id, title FROM knowledge_items
           WHERE status IN ('approved', 'pending')
             AND (is_personal = FALSE OR is_personal IS NULL)
           ORDER BY title
           LIMIT 1000"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "memory_items",
        "name": "Memory items",
        "items": [
            {
                "resource_id": r[0],
                "name": r[1] or r[0],
                "category": "memory_item",
                "description": None,
            }
            for r in rows
        ],
    }]


# ---------------------------------------------------------------------------
# Recipe projection — admin-curated query templates (v53 RBAC, v55)
# ---------------------------------------------------------------------------


def _recipe_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project ``recipes`` rows into the (block → items) shape rendered by
    the admin /access page.

    Recipes are SQL templates admins curate (v53). Default tier is
    ``available``: with no grants the recipe is invisible to non-admins.
    Soft-deleted rows (``deleted_at IS NOT NULL``) are filtered out so the
    admin grant UI doesn't accidentally hand out access to rows the
    Recipes tab can no longer show. ``resource_id`` is the
    ``recipes.id`` (e.g. ``rec_top_revenue``).
    """
    rows = conn.execute(
        """SELECT id, slug, title, description, icon, color
           FROM recipes
           WHERE deleted_at IS NULL
           ORDER BY title"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "recipes",
        "name": "Recipes",
        "items": [
            {
                "resource_id": r[0],
                "name": r[2],
                "category": "recipe",
                "description": r[3],
                "icon": r[4],
                "color": r[5],
                "slug": r[1],
            }
            for r in rows
        ],
    }]


# ---------------------------------------------------------------------------
# Cloud-chat projection
# ---------------------------------------------------------------------------


def _chat_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Singleton feature resource: one grantable item gating the whole
    cloud-chat surface (web ``/chat`` + the Slack DM bot).

    No DB entity backs it — the chat feature is a single toggle — so the
    block is static. With no grant the feature is denied to everyone except
    the ``Admin`` god-mode group; admins grant ``(group, chat, chat)`` on
    /admin/access to turn it on for a group.
    """
    return [{
        "id": "cloud_chat",
        "name": "Cloud chat",
        "items": [{
            "resource_id": "chat",
            "name": "Cloud chat",
            "description": (
                "Access to the cloud-hosted Claude chat (the /chat web UI and "
                "the Slack DM bot)."
            ),
        }],
    }]


# ---------------------------------------------------------------------------
# Slack channel allowlist projection
# ---------------------------------------------------------------------------


def _slack_channel_blocks(conn: "duckdb.DuckDBPyConnection") -> List[Block]:
    """Project the per-channel mention allowlist.

    There is **no domain table** — the ``resource_grants`` rows themselves are
    the allowlist. An admin enables Agnes in a channel by pasting its channel
    id (e.g. ``C0123ABCD``) into the create-grant form on /admin/access; that
    writes ``(Everyone, slack_channel, <channel_id>)``. We project the distinct
    granted channel ids so the admin UI can list what is currently enabled.
    Empty allowlist → no block (default-deny).
    """
    rows = conn.execute(
        """SELECT DISTINCT resource_id
           FROM resource_grants
           WHERE resource_type = 'slack_channel'
           ORDER BY resource_id"""
    ).fetchall()
    if not rows:
        return []
    return [{
        "id": "slack_channels",
        "name": "Slack channels",
        "items": [
            {
                "resource_id": r[0],
                "name": r[0],
                "category": "slack_channel",
                "description": "Channel where @agnes mentions are answered.",
            }
            for r in rows
        ],
    }]


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
    ResourceType.DATA_PACKAGE: ResourceTypeSpec(
        key=ResourceType.DATA_PACKAGE,
        display_name="Data packages",
        description="An admin-curated bundle of data tables.",
        id_format="<package_id>",
        list_blocks=_data_package_blocks,
    ),
    ResourceType.MEMORY_DOMAIN: ResourceTypeSpec(
        key=ResourceType.MEMORY_DOMAIN,
        display_name="Memory domains",
        description=(
            "A corporate-memory domain — items belonging to a granted domain "
            "are visible to members of the granted group."
        ),
        id_format="<memory_domain_id>",
        list_blocks=_memory_domain_blocks,
    ),
    ResourceType.MEMORY_ITEM: ResourceTypeSpec(
        key=ResourceType.MEMORY_ITEM,
        display_name="Memory items",
        description=(
            "Per-group override of an individual knowledge item's Required "
            "flag — rare path; the global flag covers the common case."
        ),
        id_format="<knowledge_item_id>",
        list_blocks=_memory_item_blocks,
    ),
    ResourceType.RECIPE: ResourceTypeSpec(
        key=ResourceType.RECIPE,
        display_name="Recipes",
        description=(
            "An admin-curated SQL recipe analysts copy + adapt. With no "
            "grant the recipe is hidden from non-admin viewers."
        ),
        id_format="<recipe_id>",
        list_blocks=_recipe_blocks,
    ),
    ResourceType.CHAT: ResourceTypeSpec(
        key=ResourceType.CHAT,
        display_name="Cloud chat",
        description=(
            "The cloud-hosted Claude chat feature. With no grant it is "
            "denied to everyone (except admins); grant it to a group to "
            "turn it on for that group."
        ),
        id_format="chat",
        list_blocks=_chat_blocks,
    ),
    ResourceType.SLACK_CHANNEL: ResourceTypeSpec(
        key=ResourceType.SLACK_CHANNEL,
        display_name="Slack channels",
        description=(
            "A Slack channel where @agnes mentions are answered. Grant "
            "(Everyone, slack_channel, <channel_id>) to enable Agnes there; "
            "with no grant the channel is silent (default-deny)."
        ),
        id_format="<channel_id>",
        list_blocks=_slack_channel_blocks,
    ),
}


def is_resource_type_enabled(rt: ResourceType) -> bool:
    """Whether a resource type is exposed to the admin UI + grant API.

    All resource types are unconditionally enabled in v19. The
    ``AGNES_ENABLE_TABLE_GRANTS`` env-gate that previously held back
    ``ResourceType.TABLE`` was removed when ``can_access_table`` was
    rewired onto ``app.auth.access.can_access``.
    """
    return True


def enabled_resource_types() -> list[ResourceTypeSpec]:
    """The subset of RESOURCE_TYPES currently surfaced to admins."""
    return [spec for rt, spec in RESOURCE_TYPES.items() if is_resource_type_enabled(rt)]


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
        for spec in enabled_resource_types()
    ]
