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

import logging
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


_ROLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
    if not _ROLE_KEY_RE.match(key):
        raise ValueError(
            f"Invalid internal role key {key!r}: must be lower_snake_case, "
            f"start with a letter, max 64 chars."
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
    """Reset the module-level registry. Tests only — never call from app code."""
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


def resolve_internal_roles(
    external_groups: list[dict],
    conn: duckdb.DuckDBPyConnection,
) -> list[str]:
    """Map ``session.google_groups`` to internal role keys via ``group_mappings``.

    Pure read of the mapping table — never mutates state. Returns a sorted,
    de-duplicated list of role keys. Empty list when no external groups are
    supplied or none of them are mapped.
    """
    ids = [g["id"] for g in external_groups if isinstance(g, dict) and g.get("id")]
    if not ids:
        return []
    return GroupMappingsRepository(conn).resolve_role_keys(ids)


def require_internal_role(role_key: str):
    """FastAPI dependency factory: 403 unless the user holds ``role_key``.

    Reads ``session["internal_roles"]`` populated at sign-in; no DB hit.
    The ``user`` dependency runs first so we still 401 unauthenticated
    requests with the standard message before checking role membership.
    """
    async def _check(
        request: Request,
        user: dict = Depends(get_current_user),
    ) -> dict:
        roles = []
        if hasattr(request, "session"):
            roles = request.session.get("internal_roles") or []
        if role_key not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires internal role '{role_key}'",
            )
        return user
    return _check
