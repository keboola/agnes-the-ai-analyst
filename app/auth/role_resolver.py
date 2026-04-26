"""Internal-role registry, resolver, and FastAPI dependency factory.

## Lifecycle

1. **Module import** — each Agnes module declares its internal roles via
   ``register_internal_role(...)``. The registry is module-level state, so
   the registration happens once per process.
2. **App startup** — ``sync_registered_roles_to_db(conn)`` inserts any
   newly-registered keys into ``internal_roles`` and refreshes the metadata
   (display_name, description, owner_module) on existing rows. Idempotent.
3. **Sign-in** — ``resolve_internal_roles(external_groups, conn)`` joins
   ``session.google_groups`` against ``group_mappings`` and writes the
   resulting role-key list into ``session["internal_roles"]``.
4. **Request handling** — ``require_internal_role("context_admin")`` reads
   the cached list off the session; no DB hit per request.

## Refresh semantics

Resolution happens at sign-in, so a user with a stale session keeps stale
roles after an admin changes a mapping. ``Logout → sign in again`` is the
only refresh path today — the same semantics as Google's group cache and
the existing ``session.google_groups`` flow.
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user
from src.repositories.group_mappings import GroupMappingsRepository
from src.repositories.internal_roles import InternalRolesRepository

logger = logging.getLogger(__name__)


# v9: dot-separated namespace convention (e.g. "core.admin",
# "context_engineering.admin"). The owner_module column is expected to match
# the prefix before the first dot; module-author validation lives in
# register_internal_role. Total length capped at 64 to keep the column
# bounded and to fit comfortably in audit-log resource strings.
_ROLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_ROLE_KEY_MAX_LEN = 64


@dataclass(frozen=True)
class InternalRoleSpec:
    """Module-side declaration of an internal role.

    Mirrors the persisted shape minus ``id`` (assigned at sync time) and
    timestamps. Frozen so a stray mutation can't desync registry from DB.
    """
    key: str
    display_name: str
    description: str = ""
    owner_module: Optional[str] = None


# Module-level registry. Populated by register_internal_role() at import time;
# drained by sync_registered_roles_to_db() at app startup. Kept module-level
# (not class-state) because role registration is conceptually per-process.
_REGISTRY: dict[str, InternalRoleSpec] = {}


def register_internal_role(
    key: str,
    *,
    display_name: str,
    description: str = "",
    owner_module: Optional[str] = None,
) -> None:
    """Declare an internal role at module-import time.

    ``key`` is the immutable identifier referenced from code (e.g.
    ``"context_admin"``); must match ``[a-z][a-z0-9_]{0,63}``. Calling twice
    with the same key + same fields is a no-op (re-import safe). Calling
    twice with conflicting fields raises ``ValueError`` — that almost always
    means two modules picked the same key, which would leave admins unable
    to tell which capability they're granting in the mapping UI.
    """
    if len(key) > _ROLE_KEY_MAX_LEN or not _ROLE_KEY_RE.match(key):
        raise ValueError(
            f"Invalid internal role key {key!r}: must be lower_snake_case "
            f"with optional dot-separated namespace (e.g. 'core.admin' or "
            f"'context_engineering.admin'), max {_ROLE_KEY_MAX_LEN} chars."
        )
    spec = InternalRoleSpec(
        key=key,
        display_name=display_name,
        description=description,
        owner_module=owner_module,
    )
    existing = _REGISTRY.get(key)
    if existing is not None and existing != spec:
        raise ValueError(
            f"Internal role {key!r} already registered with different fields "
            f"(existing={existing}, new={spec}). Pick a unique key."
        )
    _REGISTRY[key] = spec


def list_registered_roles() -> list[InternalRoleSpec]:
    """Snapshot of the current registry — sorted by key for stable output."""
    return sorted(_REGISTRY.values(), key=lambda s: s.key)


def _clear_registry_for_tests() -> None:
    """Reset the module-level registry. Tests only — never call from app code.

    Refuses to run unless ``TESTING=1`` so a stray import-path in production
    can't accidentally drop the registered capabilities. Pytest sets this
    via conftest / pytest.ini; production never does.
    """
    if os.environ.get("TESTING", "").lower() not in ("1", "true"):
        raise RuntimeError(
            "_clear_registry_for_tests() called outside of TESTING — "
            "this drops every registered internal role and is never safe "
            "in app code. Set TESTING=1 if you really mean this.",
        )
    _REGISTRY.clear()


def sync_registered_roles_to_db(conn: duckdb.DuckDBPyConnection) -> None:
    """Reconcile registered roles into ``internal_roles``. Idempotent.

    Inserts new keys, updates display_name/description/owner_module for
    existing keys when they've changed. Never deletes — a role disappearing
    from code may just mean the module was unloaded; the DB row keeps the
    mappings safe until an admin explicitly removes it.
    """
    repo = InternalRolesRepository(conn)
    inserted = 0
    updated = 0
    for spec in _REGISTRY.values():
        existing = repo.get_by_key(spec.key)
        if existing is None:
            repo.create(
                id=str(uuid.uuid4()),
                key=spec.key,
                display_name=spec.display_name,
                description=spec.description,
                owner_module=spec.owner_module,
            )
            inserted += 1
        else:
            drift = (
                existing.get("display_name") != spec.display_name
                or (existing.get("description") or "") != spec.description
                or (existing.get("owner_module") or None) != spec.owner_module
            )
            if drift:
                repo.update(
                    id=existing["id"],
                    display_name=spec.display_name,
                    description=spec.description,
                    owner_module=spec.owner_module,
                )
                updated += 1
    if inserted or updated:
        logger.info(
            "internal_roles sync: %d inserted, %d updated, %d total registered",
            inserted, updated, len(_REGISTRY),
        )


def expand_implies(
    role_keys: list[str], conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """Transitively expand a role-key list along the ``implies`` JSON column.

    Example: input ``["core.admin"]`` returns
    ``["core.admin", "core.km_admin", "core.analyst", "core.viewer"]`` because
    ``core.admin.implies = ["core.km_admin"]``, ``core.km_admin.implies =
    ["core.analyst"]``, etc. BFS — visits each role at most once even if the
    graph contains a (mis-configured) cycle. Output is sorted + deduped.

    Reads the entire ``internal_roles`` row set in a single query and walks
    the graph in Python; for the expected scale (single-digit core.* + tens
    of module roles) this is cheaper than recursive CTE round-trips and
    keeps the JSON-as-text decode local. If the table grows past hundreds of
    rows, switch to a recursive CTE in DuckDB.
    """
    if not role_keys:
        return []
    rows = conn.execute(
        "SELECT key, implies FROM internal_roles"
    ).fetchall()
    implies_map: dict[str, list[str]] = {}
    for key, implies_json in rows:
        try:
            implies_map[key] = list(_json.loads(implies_json or "[]"))
        except (TypeError, ValueError):
            implies_map[key] = []
    seen: set[str] = set()
    queue: list[str] = list(role_keys)
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for implied in implies_map.get(current, []):
            if implied not in seen:
                queue.append(implied)
    return sorted(seen)


def resolve_internal_roles(
    external_groups: list[dict],
    conn: duckdb.DuckDBPyConnection,
    user_id: Optional[str] = None,
) -> list[str]:
    """Resolve a user's internal role keys from both auth paths.

    Two complementary sources are unioned, then expanded along the implies
    hierarchy:

    - **External groups** — Cloud Identity / mocked dev groups joined against
      ``group_mappings``. Drives the OAuth-callback session cache.
    - **Direct user grants** (``user_id`` supplied) — rows in
      ``user_role_grants``. Persists across sessions and works for PAT /
      headless callers, where the session cache is unreachable.

    Pure read — never mutates state. Returns a sorted, de-duplicated list of
    role keys. Empty list when neither source produces any membership.

    Callers in the OAuth callback pass ``external_groups`` only; PAT-aware
    callers (``require_internal_role``) pass ``user_id`` only; the two paths
    share the implies expansion so a user holding ``core.admin`` directly
    sees the same expanded set as one resolved through a Cloud Identity
    group mapping.
    """
    keys: set[str] = set()

    ids = [g["id"] for g in external_groups if isinstance(g, dict) and g.get("id")]
    if ids:
        keys.update(GroupMappingsRepository(conn).resolve_role_keys(ids))

    if user_id:
        rows = conn.execute(
            """SELECT r.key
               FROM user_role_grants g
               JOIN internal_roles r ON g.internal_role_id = r.id
               WHERE g.user_id = ?""",
            [user_id],
        ).fetchall()
        keys.update(row[0] for row in rows)

    if not keys:
        return []
    return expand_implies(sorted(keys), conn)


def require_internal_role(role_key: str):
    """FastAPI dependency factory: 403 unless the user holds ``role_key``.

    Two-path resolution (v9):

    1. **Session cache** (OAuth flow) — reads ``session["internal_roles"]``
       populated by ``resolve_internal_roles`` at sign-in. No DB hit; the
       fast path for browser users.
    2. **Direct grants fallback** (PAT/headless flow) — when the session
       doesn't carry the role (or no session exists), looks up
       ``user_role_grants`` for the authenticated user, expands implies,
       and re-checks. One DB query per gated request — acceptable cost for
       headless callers that lack the session cache by design.

    The two-path design lets PAT clients hit endpoints gated by
    ``require_internal_role("core.admin")`` without surrendering session
    semantics: an admin user holding ``core.admin`` via either a direct
    grant or a Cloud Identity group mapping will succeed regardless of
    whether they came in via OAuth cookie or Authorization: Bearer.

    The ``user`` dependency runs first so we still 401 unauthenticated
    requests with the standard message before checking role membership.
    """
    async def _check(
        request: Request,
        user: dict = Depends(get_current_user),
    ) -> dict:
        # Path 1: session cache — present for OAuth callers, absent for PAT.
        roles: list[str] = []
        if hasattr(request, "session"):
            roles = request.session.get("internal_roles") or []

        if role_key in roles:
            return user

        # Path 2: DB-backed direct grants. Only consulted when the session
        # cache didn't grant access — typical for PAT/headless callers, but
        # also a safety net for OAuth callers whose session was populated
        # before the admin granted them the role (no need to log out + back
        # in for direct grants to take effect, unlike group_mappings which
        # are still cached on the session).
        try:
            from src.db import get_system_db
            conn = get_system_db()
            try:
                granted = resolve_internal_roles(
                    [], conn, user_id=user.get("id"),
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(
                "require_internal_role: DB grant lookup failed for %s/%s: %s",
                user.get("email", "<unknown>"), role_key, e,
            )
            granted = []

        if role_key in granted:
            return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires internal role '{role_key}'",
        )
    return _check
