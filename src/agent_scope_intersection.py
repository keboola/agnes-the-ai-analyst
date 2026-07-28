"""Set-intersection of an agent's owner grants x its declared scope, per
ResourceType (V1d).

Mirrors ``src/grant_intersection.py``'s shape (fail-closed, builds on
``_allowed_ids_for_user``, routed through the repo factory — never raw SQL
on ``conn``) but for a *single* owner narrowed by a *single* agent's four
``*_mode`` columns instead of N co-session participants.

Fail-closed contract (spec §2, normative):
  - Missing/empty ``owner_user_id`` or ``agent_row`` -> ``{}`` (deny
    everything).
  - Mode ``'all'`` (or a ``ResourceType`` the agent does not model at all,
    e.g. ``DATA_PACKAGE``/``RECIPE``/``COLLECTION``/...) -> the owner's set,
    unchanged. The agent narrows only what it declares; every resource type
    it stays silent on passes through as the owner's authority.
  - Mode ``'selected'`` -> ``owner_set & agent_scope_set`` for that type. A
    scope row naming a resource the owner does NOT hold is silently
    dropped, never surfaced — an agent can never widen beyond its owner.
  - An unrecognized (neither ``'all'`` nor ``'selected'``) mode value ->
    ``frozenset()`` for that type. This is the OPPOSITE of
    ``app.chat.agent_profile.compute_effective_scope``'s audit-only
    fail-open ("treat as all") — that function only *describes* scope for
    admin review; this one *enforces* it, so an unrecognized mode must
    fail closed rather than accidentally grant full access.

``connections_mode`` is deliberately absent from ``MODE_TO_RESOURCE_TYPE``:
there is no ``ResourceType.CONNECTION`` — per-user MCP connections are
authorized through a separate mechanism entirely (``tool_registry``
passthrough grants keyed on groups). Do not "fix" this by inventing a
resource type here; the axis is enforced at its own seam via
``agent_scope_filter`` below.
"""

from __future__ import annotations

from typing import Optional

import duckdb

from app.resource_types import ResourceType

# agents.<mode column> -> (agent_scope.item_type, ResourceType.value).
# Reused by the seams (e.g. the sandbox-materialization filter) that need
# the same mode->type mapping this module uses internally.
MODE_TO_RESOURCE_TYPE: dict[str, tuple[str, str]] = {
    "tables_mode": ("table", "table"),
    "plugins_mode": ("plugin", "marketplace_plugin"),
    "memory_mode": ("memory_domain", "memory_domain"),
}


def _allowed_ids_for_user(
    user_id: str,
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Module-attribute indirection onto ``app.auth.access``'s no-admin-
    short-circuit grant primitive — kept as a separate module attribute (not
    inlined at the call site) so tests can monkeypatch it, mirroring
    ``src/grant_intersection.py``."""
    from app.auth.access import _allowed_ids_for_user as _impl

    return _impl(user_id, resource_type, conn)


def _agent_scope_ids(
    agent_id: str,
    item_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Set of ``item_id`` the agent's ``agent_scope`` declares for
    ``item_type``, read through the repo factory. ``conn`` is accepted for
    signature symmetry with ``_allowed_ids_for_user`` (and to leave room for
    a future DuckDB-direct fast path) but is currently unused — ``get_scope``
    is factory-routed, backend-agnostic."""
    from src.repositories import agents_repo

    items = agents_repo().get_scope(agent_id)
    return frozenset(item["item_id"] for item in items if item.get("item_type") == item_type)


def agent_scope_filter(
    agent_id: Optional[str],
    mode_field: str,
    item_type: str,
) -> Optional[frozenset[str]]:
    """Live allow-set for ONE scope axis, or ``None`` when the agent does not
    narrow that axis.

    The intersection map can only carry resources authorized through
    ``resource_grants``. Two axes are authorized elsewhere and therefore need
    a filter at their own seam, reading the agent's ``agent_scope`` rows
    directly:

    - ``connections_mode`` / ``connection`` — per-user MCP connections
      (``tool_registry`` grants keyed on groups); item_id is an
      ``mcp_sources.id``.
    - ``plugins_mode`` / ``plugin`` for **Store installs** — personal
      flea-market installs live in ``user_store_installs``, never in
      ``resource_grants``, so they survive the intersection untouched. (The
      admin-curated half of the same axis IS in the intersection and is
      filtered there; this helper covers only the store union.)

    Return contract, deliberately three-valued:

    - ``None`` — mode is ``'all'``: the agent does not narrow this axis, so
      the caller must apply **no** filter. Keying off this (rather than off
      "the caller is an ``AgentPrincipal``") is the whole point: the broker
      mints an agent-session token as soon as an agent narrows *anything*, so
      a tables-narrowed agent legitimately keeps every connection and store
      install its owner has.
    - a ``frozenset`` — mode is ``'selected'``: exactly the declared
      ``item_id`` set, possibly empty (an empty allowlist is a real answer,
      never a pass-through).
    - ``frozenset()`` — fail closed for a missing / soft-deleted agent row or
      an unrecognized mode value, mirroring ``compute_agent_intersection``.
    """
    from src.repositories import agents_repo

    if not agent_id:
        return frozenset()
    repo = agents_repo()
    agent = repo.get_by_id(agent_id)
    if not agent or agent.get("deleted_at") is not None:
        return frozenset()
    mode = agent.get(mode_field)
    if mode == "all":
        return None
    if mode != "selected":
        return frozenset()
    return frozenset(item["item_id"] for item in repo.get_scope(agent_id) if item.get("item_type") == item_type)


def agent_narrows(agent_row: dict) -> bool:
    """True when ANY of the four mode columns is ``'selected'`` — i.e. this
    agent actually restricts its owner rather than passing everything
    through unchanged. Used by the broker for the default-agent carve-out
    (an all-'all' agent needs no intersection work)."""
    for mode_field in ("tables_mode", "plugins_mode", "connections_mode", "memory_mode"):
        if agent_row.get(mode_field) == "selected":
            return True
    return False


def compute_agent_intersection(
    owner_user_id: str,
    agent_row: Optional[dict],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    """Owner's grants narrowed by the agent's declared scope, per
    ``ResourceType`` — see module docstring for the full fail-closed
    contract."""
    if not owner_user_id or not agent_row:
        return {}

    agent_id = agent_row.get("id")

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        owner_set = _allowed_ids_for_user(owner_user_id, rt.value, conn)

        mode_field = next(
            (mf for mf, (_item_type, rt_value) in MODE_TO_RESOURCE_TYPE.items() if rt_value == rt.value),
            None,
        )
        if mode_field is None:
            # Resource type the agent does not model at all -> pass through
            # the owner's set verbatim (narrows only what it declares).
            if owner_set:
                result[rt.value] = owner_set
            continue

        item_type, _rt_value = MODE_TO_RESOURCE_TYPE[mode_field]
        mode = agent_row.get(mode_field)
        if mode == "all":
            if owner_set:
                result[rt.value] = owner_set
        elif mode == "selected":
            agent_set = _agent_scope_ids(agent_id, item_type, conn)
            narrowed = owner_set & agent_set
            if narrowed:
                result[rt.value] = narrowed
        else:
            # Unrecognized mode -> fail closed. Only record an empty set
            # when the type would otherwise have appeared, to mirror the
            # "if owner_set" omission pattern above; either way the
            # resulting membership test is empty/deny.
            result[rt.value] = frozenset()

    return result
