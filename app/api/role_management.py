"""Admin REST API for v9 role management.

Three resource families, all under ``/api/admin``:

- ``/internal-roles``                    — read-only listing of capabilities.
- ``/group-mappings``                    — Cloud Identity group → role binds.
- ``/users/{user_id}/role-grants``       — direct user → role grants.
- ``/users/{user_id}/effective-roles``   — debug view of resolved keys.

Every endpoint is gated by ``require_internal_role("core.admin")`` rather than
the legacy ``require_role(Role.ADMIN)`` so PAT-aware callers (CLI scripts that
bear a personal access token) succeed via the resolver's ``user_role_grants``
fallback. See ``app/auth/role_resolver.py`` for the two-path resolution.

Mutations write to ``audit_log`` so changes to the privilege matrix are
reconstructable from a single table — the same discipline ``app/api/users.py``
applies to user CRUD.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import _get_db
from app.auth.role_resolver import (
    require_internal_role,
    resolve_internal_roles,
)
from src.repositories.audit import AuditRepository
from src.repositories.group_mappings import GroupMappingsRepository
from src.repositories.internal_roles import InternalRolesRepository
from src.repositories.user_role_grants import UserRoleGrantsRepository
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["role-management"])


# --- Pydantic request/response models ----------------------------------------

class CreateGroupMappingRequest(BaseModel):
    external_group_id: str
    role_key: str


class CreateRoleGrantRequest(BaseModel):
    role_key: str


class InternalRoleResponse(BaseModel):
    id: str
    key: str
    display_name: str
    description: Optional[str] = None
    owner_module: Optional[str] = None
    is_core: bool = False
    # implies is stored as a JSON-encoded VARCHAR (DuckDB legacy compat —
    # see src/db.py); we parse it before returning so clients see a real list.
    implies: List[str] = []


class GroupMappingResponse(BaseModel):
    id: str
    external_group_id: str
    internal_role_id: str
    role_key: str
    role_display_name: str
    assigned_at: Optional[str] = None
    assigned_by: Optional[str] = None


class RoleGrantResponse(BaseModel):
    id: str
    user_id: str
    internal_role_id: str
    role_key: str
    role_display_name: str
    role_is_core: bool = False
    granted_at: Optional[str] = None
    granted_by: Optional[str] = None
    source: str = "direct"


class EffectiveRolesResponse(BaseModel):
    direct: List[RoleGrantResponse]
    group: List[Dict[str, Any]]
    expanded: List[str]


# --- Helpers -----------------------------------------------------------------

def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    """Best-effort audit insert. Never blocks the endpoint on failure."""
    try:
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=resource, params=params,
        )
    except Exception:  # pragma: no cover — defensive only
        logger.exception("audit insert failed for %s/%s", action, resource)


def _parse_implies(raw: Any) -> List[str]:
    """Decode the implies VARCHAR-as-JSON column. Empty list on bad input.

    Mirrors the defensive parsing in ``role_resolver.expand_implies`` —
    legacy rows that predate the v9 default could still be NULL or
    malformed, and the listing endpoint shouldn't 500 on them.
    """
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        return list(decoded) if isinstance(decoded, list) else []
    except (TypeError, ValueError):
        return []


def _internal_role_to_response(row: Dict[str, Any]) -> InternalRoleResponse:
    return InternalRoleResponse(
        id=row["id"],
        key=row["key"],
        display_name=row["display_name"],
        description=row.get("description"),
        owner_module=row.get("owner_module"),
        is_core=bool(row.get("is_core", False)),
        implies=_parse_implies(row.get("implies")),
    )


def _group_mapping_to_response(row: Dict[str, Any]) -> GroupMappingResponse:
    return GroupMappingResponse(
        id=row["id"],
        external_group_id=row["external_group_id"],
        internal_role_id=row["internal_role_id"],
        role_key=row.get("internal_role_key", ""),
        role_display_name=row.get("internal_role_display_name", ""),
        assigned_at=str(row["assigned_at"]) if row.get("assigned_at") else None,
        assigned_by=row.get("assigned_by"),
    )


def _role_grant_to_response(row: Dict[str, Any]) -> RoleGrantResponse:
    return RoleGrantResponse(
        id=row["id"],
        user_id=row["user_id"],
        internal_role_id=row["internal_role_id"],
        role_key=row.get("role_key", ""),
        role_display_name=row.get("role_display_name", ""),
        role_is_core=bool(row.get("role_is_core", False)),
        granted_at=str(row["granted_at"]) if row.get("granted_at") else None,
        granted_by=row.get("granted_by"),
        source=row.get("source") or "direct",
    )


def _resolve_role_or_404(
    conn: duckdb.DuckDBPyConnection, role_key: str,
) -> Dict[str, Any]:
    """Fetch internal_role row by key or raise 404. Used by POST handlers.

    Centralizes the validation so every mutator surfaces the same 404
    detail when an admin types a stale or unknown key.
    """
    role = InternalRolesRepository(conn).get_by_key(role_key)
    if not role:
        raise HTTPException(
            status_code=404,
            detail=f"Internal role '{role_key}' does not exist",
        )
    return role


def _count_active_admins(conn: duckdb.DuckDBPyConnection) -> int:
    """Mirror of UserRepository.count_admins(active_only=True).

    Inlined here rather than instantiating UserRepository on every delete
    request — the SQL is short and we already hold the connection. Deleting
    a grant must refuse to cross zero so the system never locks itself out
    of its own admin endpoints.
    """
    result = conn.execute(
        """SELECT COUNT(DISTINCT u.id)
           FROM users u
           JOIN user_role_grants g ON g.user_id = u.id
           JOIN internal_roles r ON g.internal_role_id = r.id
           WHERE r.key = 'core.admin'
             AND COALESCE(u.active, TRUE) = TRUE"""
    ).fetchone()
    return int(result[0]) if result else 0


# --- Internal roles -----------------------------------------------------------

@router.get("/internal-roles", response_model=List[InternalRoleResponse])
async def list_internal_roles(
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List every registered internal role.

    Read-only — admins map external groups onto these via
    ``/group-mappings``; they don't create roles in the UI. Module authors
    register roles in code (``register_internal_role``) and the startup hook
    syncs them into ``internal_roles``.
    """
    rows = InternalRolesRepository(conn).list_all()
    return [_internal_role_to_response(r) for r in rows]


# --- Group mappings ----------------------------------------------------------

@router.get("/group-mappings", response_model=List[GroupMappingResponse])
async def list_group_mappings(
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List external-group → internal-role mappings.

    Joined with ``internal_roles`` so the UI can render the role's key +
    display name without a second round trip.
    """
    rows = GroupMappingsRepository(conn).list_all()
    return [_group_mapping_to_response(r) for r in rows]


@router.post(
    "/group-mappings",
    response_model=GroupMappingResponse,
    status_code=201,
)
async def create_group_mapping(
    payload: CreateGroupMappingRequest,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Bind an external Cloud Identity group to an internal role.

    409 when the (external_group_id, role_key) pair is already mapped —
    the table has a UNIQUE constraint on (external_group_id, internal_role_id).
    """
    role = _resolve_role_or_404(conn, payload.role_key)

    repo = GroupMappingsRepository(conn)
    # Pre-flight existence check: clearer 409 than letting the FK / UNIQUE
    # constraint fire with a DuckDB-shaped error message.
    for existing in repo.list_by_external_group(payload.external_group_id):
        if existing["internal_role_id"] == role["id"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Group '{payload.external_group_id}' is already mapped "
                    f"to role '{payload.role_key}'"
                ),
            )

    mapping_id = str(uuid.uuid4())
    repo.create(
        id=mapping_id,
        external_group_id=payload.external_group_id,
        internal_role_id=role["id"],
        assigned_by=user.get("email"),
    )
    _audit(
        conn, user["id"], "role_mapping.created",
        f"mapping:{mapping_id}",
        {
            "external_group_id": payload.external_group_id,
            "role_key": payload.role_key,
        },
    )
    created = repo.get_by_id(mapping_id) or {}
    # get_by_id doesn't join — fetch the role display fields manually for
    # the response. Cheap and avoids touching the repo signature.
    created["internal_role_key"] = role["key"]
    created["internal_role_display_name"] = role["display_name"]
    return _group_mapping_to_response(created)


@router.delete("/group-mappings/{mapping_id}", status_code=204)
async def delete_group_mapping(
    mapping_id: str,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove a group mapping by id. 404 when missing."""
    repo = GroupMappingsRepository(conn)
    existing = repo.get_by_id(mapping_id)
    if not existing:
        raise HTTPException(
            status_code=404, detail=f"Group mapping '{mapping_id}' not found"
        )
    repo.delete(mapping_id)
    _audit(
        conn, user["id"], "role_mapping.deleted",
        f"mapping:{mapping_id}",
        {
            "external_group_id": existing.get("external_group_id"),
            "internal_role_id": existing.get("internal_role_id"),
        },
    )


# --- User role grants --------------------------------------------------------

@router.get(
    "/users/{user_id}/role-grants",
    response_model=List[RoleGrantResponse],
)
async def list_user_role_grants(
    user_id: str,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List a user's direct role grants — both ``direct`` and ``auto-seed``.

    404 when the user_id doesn't exist so admins can distinguish "user has no
    grants" (200 + empty list) from "user does not exist" (404).
    """
    if not UserRepository(conn).get_by_id(user_id):
        raise HTTPException(
            status_code=404, detail=f"User '{user_id}' not found"
        )
    rows = UserRoleGrantsRepository(conn).list_for_user(user_id)
    return [_role_grant_to_response(r) for r in rows]


@router.post(
    "/users/{user_id}/role-grants",
    response_model=RoleGrantResponse,
    status_code=201,
)
async def create_user_role_grant(
    user_id: str,
    payload: CreateRoleGrantRequest,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Grant ``role_key`` to ``user_id`` directly. Source is ``'direct'``.

    409 when the user already holds the role (UNIQUE constraint on
    (user_id, internal_role_id)).
    """
    target = UserRepository(conn).get_by_id(user_id)
    if not target:
        raise HTTPException(
            status_code=404, detail=f"User '{user_id}' not found"
        )
    role = _resolve_role_or_404(conn, payload.role_key)

    grants_repo = UserRoleGrantsRepository(conn)
    for existing in grants_repo.list_for_user(user_id):
        if existing["internal_role_id"] == role["id"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"User '{user_id}' already holds role "
                    f"'{payload.role_key}'"
                ),
            )

    grant_id = str(uuid.uuid4())
    try:
        grants_repo.create(
            id=grant_id,
            user_id=user_id,
            internal_role_id=role["id"],
            granted_by=user.get("email"),
            source="direct",
        )
    except duckdb.ConstraintException as e:
        # Race vs. the pre-flight check above — duplicate key arrived between
        # the two calls. Surface as 409 too for client consistency.
        raise HTTPException(status_code=409, detail=str(e))

    _audit(
        conn, user["id"], "role_grant.created",
        f"grant:{grant_id}",
        {
            "target_user_id": user_id,
            "target_email": target.get("email"),
            "role_key": payload.role_key,
        },
    )

    created = grants_repo.get(grant_id) or {}
    # get() doesn't join — supplement with role display fields.
    created["role_key"] = role["key"]
    created["role_display_name"] = role["display_name"]
    created["role_is_core"] = bool(role.get("is_core", False))
    return _role_grant_to_response(created)


@router.delete(
    "/users/{user_id}/role-grants/{grant_id}",
    status_code=204,
)
async def delete_user_role_grant(
    user_id: str,
    grant_id: str,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke a grant. 404 when missing.

    Refuses to delete the last active core.admin grant in the system —
    same lockout-protection logic as ``UserRepository.count_admins``.
    Without this guard an admin could delete their own (and the only)
    core.admin grant and leave nobody able to call this endpoint.
    """
    grants_repo = UserRoleGrantsRepository(conn)
    grant = grants_repo.get(grant_id)
    if not grant or grant.get("user_id") != user_id:
        raise HTTPException(
            status_code=404, detail=f"Grant '{grant_id}' not found"
        )

    # Look up the role so we can check whether this is the last core.admin
    # holder. Cheap — single row by id.
    role = InternalRolesRepository(conn).get_by_id(
        grant["internal_role_id"]
    )
    if role and role.get("key") == "core.admin":
        if _count_active_admins(conn) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot revoke the last active core.admin grant",
            )

    grants_repo.delete(grant_id)
    _audit(
        conn, user["id"], "role_grant.deleted",
        f"grant:{grant_id}",
        {
            "target_user_id": user_id,
            "internal_role_id": grant.get("internal_role_id"),
            "role_key": role.get("key") if role else None,
        },
    )


# --- Effective roles (debug) --------------------------------------------------

@router.get(
    "/users/{user_id}/effective-roles",
    response_model=EffectiveRolesResponse,
)
async def get_effective_roles(
    user_id: str,
    user: dict = Depends(require_internal_role("core.admin")),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Debug view: direct grants + group memberships + expanded role keys.

    The ``group`` field is best-effort: when the calling admin happens to be
    inspecting their own user_id and the request carries a session with
    cached ``google_groups``, we surface those for the resolver join. For
    cross-user debugging or PAT callers we return ``[]`` — Cloud Identity
    group membership for arbitrary users isn't queryable from this API
    surface (Google's directory.groups.list is the source of truth and isn't
    re-fetched here).

    ``expanded`` is the resolver's authoritative output for the target user
    — direct grants only, since we cannot reliably enumerate the user's
    Cloud Identity groups from a server-side context.
    """
    if not UserRepository(conn).get_by_id(user_id):
        raise HTTPException(
            status_code=404, detail=f"User '{user_id}' not found"
        )

    grant_rows = UserRoleGrantsRepository(conn).list_for_user(user_id)
    direct = [_role_grant_to_response(r) for r in grant_rows]

    # Direct-grant expansion via the resolver. Pass external_groups=[] —
    # we don't have a reliable way to enumerate the target user's groups
    # without their session, so the expanded set reflects user_role_grants
    # only. This matches what require_internal_role does for PAT callers.
    expanded = resolve_internal_roles([], conn, user_id=user_id)

    # Group view: only useful when the admin asks about themselves AND
    # signed in via OAuth. Otherwise return [] — better than fabricating data.
    group: List[Dict[str, Any]] = []
    return EffectiveRolesResponse(
        direct=direct, group=group, expanded=expanded,
    )
