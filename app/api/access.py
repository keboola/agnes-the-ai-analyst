"""Unified admin REST API for user_groups, members, and resource_grants.

Replaces ``app.api.role_management`` and ``app.api.plugin_access`` with a
single namespace under ``/api/admin``:

  - ``GET/POST/DELETE /api/admin/groups``
  - ``GET/POST/DELETE /api/admin/groups/{group_id}/members``
  - ``GET/POST/DELETE /api/admin/grants``
  - ``GET            /api/admin/resource-types``

Every endpoint is gated by ``require_admin``. Audit log entries are written
for every mutation so an admin's group/grant changes are traceable.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import RESOURCE_TYPES, ResourceType, list_resource_types
from src.repositories.audit import AuditRepository
from src.repositories.user_groups import (
    SystemGroupProtected,
    UserGroupsRepository,
)
from src.repositories.resource_grants import ResourceGrantsRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["access"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe = None
        if params:
            safe = {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in params.items()
            }
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=resource, params=safe
        )
    except Exception:
        # Audit failures must never break the mutation. Logged at WARN.
        logger.warning("audit log failed for %s/%s", action, resource)


def _validate_resource_type(value: str) -> ResourceType:
    try:
        return ResourceType(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown resource_type {value!r}. Known types: "
                f"{[rt.value for rt in ResourceType]}"
            ),
        )


# ---------------------------------------------------------------------------
# Resource types (read-only, from Python enum)
# ---------------------------------------------------------------------------


@router.get("/resource-types", response_model=List[dict])
async def get_resource_types(
    user: dict = Depends(require_admin),
):
    """List the resource types defined in app.resource_types.

    No DB call — these come from the ``ResourceType`` StrEnum. The shape
    is ``[{key, display_name, description, id_format}]`` so the admin UI
    can render the create-grant form's resource_type dropdown plus a
    placeholder hint for the ``resource_id`` input.
    """
    return list_resource_types()


@router.get("/group-suggestions", response_model=List[dict])
async def get_group_suggestions(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Suggest Google Workspace group names the calling admin belongs to that
    are *not yet* registered as ``user_groups`` rows.

    Powers the "Suggested from your Google account" picker on the
    /admin/groups create modal — click a chip → name input is pre-filled.

    Fail-soft: returns ``[]`` if the Cloud Identity call errors. Off-VM the
    call falls through to the real path and bails out empty unless
    ``GOOGLE_ADMIN_SDK_MOCK_GROUPS`` is set.
    """
    from app.auth.group_sync import fetch_user_groups

    email = user.get("email") or ""
    if not email:
        return []
    try:
        google_names = fetch_user_groups(email)
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("group-suggestions fetch failed for %s: %s", email, e)
        return []
    if not google_names:
        return []
    existing = {g["name"] for g in UserGroupsRepository(conn).list_all()}
    return [
        {"name": n, "source": "google"}
        for n in google_names
        if n and n not in existing
    ]


# ---------------------------------------------------------------------------
# Access overview — single-shot payload for the /admin/access page
# ---------------------------------------------------------------------------


@router.get("/access-overview", response_model=dict)
async def access_overview(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """One-shot snapshot for the /admin/access page.

    Returns:
      - ``groups``: every user_group with member + grant counts
      - ``grants``: every (group_id, resource_type, resource_id) row
      - ``resources``: per-resource-type hierarchical layout, where each
        type has a list of *blocks* (parent entities, e.g. a marketplace)
        and each block has *items* (concrete grantable resources).

    UI stitches the three pieces into the two-column layout: groups on
    the left, resources tree on the right with per-item checkboxes whose
    state derives from ``grants``.
    """
    groups_rows = UserGroupsRepository(conn).list_all()
    members_repo = UserGroupMembersRepository(conn)
    grants_repo = ResourceGrantsRepository(conn)

    groups = []
    for g in groups_rows:
        groups.append({
            "id": g["id"],
            "name": g["name"],
            "description": g.get("description"),
            "is_system": bool(g.get("is_system", False)),
            "member_count": members_repo.count_members(g["id"]),
            "grant_count": grants_repo.count_for_group(g["id"]),
        })

    grants = [
        {
            "id": r["id"],
            "group_id": r["group_id"],
            "resource_type": r["resource_type"],
            "resource_id": r["resource_id"],
        }
        for r in grants_repo.list_all()
    ]

    # Per-resource-type hierarchies. Driven by the registry in
    # app.resource_types — adding a new type there is the one place that
    # surfaces here, no extra wiring.
    resources = [
        {
            "type_key": spec.key.value,
            "type_display": spec.display_name,
            "blocks": spec.list_blocks(conn),
        }
        for spec in RESOURCE_TYPES.values()
    ]

    return {"groups": groups, "grants": grants, "resources": resources}


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------


class GroupResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_system: bool = False
    origin: str = "admin"  # 'system' | 'admin' | 'google_sync'
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    member_count: int = 0
    grant_count: int = 0


class CreateGroupRequest(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


def _derive_origin(g: dict) -> str:
    """Project a 3-value origin tag from the existing user_groups columns.

    - ``is_system=TRUE``                       → 'system'  (Admin / Everyone)
    - ``created_by`` starts with 'system:'     → 'google_sync' (or other auto)
    - else                                     → 'admin' (created via UI/CLI)

    The OAuth callback stamps ``created_by='system:google-sync'`` when it
    auto-creates a group from a Cloud Identity claim, so the origin is
    derivable without a new column.
    """
    if g.get("is_system"):
        return "system"
    cb = g.get("created_by") or ""
    if cb.startswith("system:google"):
        return "google_sync"
    if cb.startswith("system:"):
        return "system"
    return "admin"


def _group_to_response(
    g: dict,
    members_repo: UserGroupMembersRepository,
    grants_repo: ResourceGrantsRepository,
) -> GroupResponse:
    return GroupResponse(
        id=g["id"],
        name=g["name"],
        description=g.get("description"),
        is_system=bool(g.get("is_system", False)),
        origin=_derive_origin(g),
        created_at=str(g["created_at"]) if g.get("created_at") else None,
        created_by=g.get("created_by"),
        member_count=members_repo.count_members(g["id"]),
        grant_count=grants_repo.count_for_group(g["id"]),
    )


@router.get("/groups", response_model=List[GroupResponse])
async def list_groups(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    groups = UserGroupsRepository(conn).list_all()
    members_repo = UserGroupMembersRepository(conn)
    grants_repo = ResourceGrantsRepository(conn)
    return [_group_to_response(g, members_repo, grants_repo) for g in groups]


@router.get("/groups/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Single-group payload for the /admin/groups/{id} detail page header."""
    g = UserGroupsRepository(conn).get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    members_repo = UserGroupMembersRepository(conn)
    grants_repo = ResourceGrantsRepository(conn)
    return _group_to_response(g, members_repo, grants_repo)


@router.post("/groups", response_model=GroupResponse, status_code=201)
async def create_group(
    payload: CreateGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    repo = UserGroupsRepository(conn)
    if repo.get_by_name(name):
        raise HTTPException(status_code=409, detail=f"Group {name!r} already exists")
    g = repo.create(
        name=name,
        description=payload.description,
        created_by=user.get("email"),
    )
    _audit(
        conn, user["id"], "user_group.created", f"group:{g['id']}",
        {"name": name},
    )
    members_repo = UserGroupMembersRepository(conn)
    grants_repo = ResourceGrantsRepository(conn)
    return _group_to_response(g, members_repo, grants_repo)


@router.patch("/groups/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: str,
    payload: UpdateGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserGroupsRepository(conn)
    g = repo.get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    if g.get("is_system"):
        # Allow description edits but not rename — the canonical names
        # 'Admin' / 'Everyone' are referenced from the codebase.
        if payload.name is not None and payload.name != g["name"]:
            raise HTTPException(
                status_code=409,
                detail="Cannot rename a system group",
            )
    updates: dict = {}
    if payload.name is not None:
        updates["name"] = payload.name.strip()
    if payload.description is not None:
        updates["description"] = payload.description
    if updates:
        try:
            repo.update(group_id, **updates)
        except SystemGroupProtected:
            raise HTTPException(
                status_code=409, detail="Cannot rename a system group",
            )
        _audit(conn, user["id"], "user_group.updated", f"group:{group_id}", updates)
    g = repo.get(group_id)
    members_repo = UserGroupMembersRepository(conn)
    grants_repo = ResourceGrantsRepository(conn)
    return _group_to_response(g, members_repo, grants_repo)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserGroupsRepository(conn)
    g = repo.get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    if g.get("is_system"):
        raise HTTPException(status_code=409, detail="Cannot delete a system group")
    try:
        repo.delete(group_id)
    except SystemGroupProtected:
        raise HTTPException(status_code=409, detail="Cannot delete a system group")
    # Cascade members + grants for this group so dangling references go away.
    conn.execute("DELETE FROM user_group_members WHERE group_id = ?", [group_id])
    conn.execute("DELETE FROM resource_grants WHERE group_id = ?", [group_id])
    _audit(
        conn, user["id"], "user_group.deleted", f"group:{group_id}",
        {"name": g["name"]},
    )


# ---------------------------------------------------------------------------
# Group members
# ---------------------------------------------------------------------------


class MemberResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    active: bool = True
    source: str
    added_at: Optional[str] = None
    added_by: Optional[str] = None


class AddMemberRequest(BaseModel):
    email: str


@router.get("/groups/{group_id}/members", response_model=List[MemberResponse])
async def list_members(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not UserGroupsRepository(conn).get(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    rows = UserGroupMembersRepository(conn).list_members_for_group(group_id)
    return [
        MemberResponse(
            user_id=r["id"],
            email=r["email"],
            name=r.get("name"),
            active=bool(r.get("active", True)),
            source=r["source"],
            added_at=str(r["added_at"]) if r.get("added_at") else None,
            added_by=r.get("added_by"),
        )
        for r in rows
    ]


@router.post("/groups/{group_id}/members", response_model=MemberResponse, status_code=201)
async def add_member(
    group_id: str,
    payload: AddMemberRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not UserGroupsRepository(conn).get(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    target = UserRepository(conn).get_by_email(payload.email)
    if not target:
        raise HTTPException(status_code=404, detail=f"User {payload.email!r} not found")
    members = UserGroupMembersRepository(conn)
    if members.has_membership(target["id"], group_id):
        raise HTTPException(status_code=409, detail="User already a member")
    members.add_member(
        user_id=target["id"],
        group_id=group_id,
        source="admin",
        added_by=user.get("email"),
    )
    _audit(
        conn, user["id"], "user_group.member_added",
        f"group:{group_id}",
        {"user_email": payload.email},
    )
    return MemberResponse(
        user_id=target["id"],
        email=target["email"],
        name=target.get("name"),
        active=bool(target.get("active", True)),
        source="admin",
        added_at=None,
        added_by=user.get("email"),
    )


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
async def remove_member(
    group_id: str,
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    members = UserGroupMembersRepository(conn)
    # Block removing yourself from Admin if you're the last admin — same
    # protection as the user-management endpoints.
    group = UserGroupsRepository(conn).get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["name"] == "Admin" and user_id == user["id"]:
        if UserRepository(conn).count_admins(active_only=True) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot remove yourself from Admin — you are the last admin",
            )
    # Only delete admin-source rows from this endpoint. Google-sync rows
    # rebuild themselves on next login; system_seed rows survive deploys.
    removed = members.remove_member(user_id, group_id, require_source="admin")
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="No admin-managed membership for this user in this group",
        )
    _audit(
        conn, user["id"], "user_group.member_removed",
        f"group:{group_id}",
        {"user_id": user_id},
    )


# ---------------------------------------------------------------------------
# Resource grants
# ---------------------------------------------------------------------------


class GrantResponse(BaseModel):
    id: str
    group_id: str
    group_name: str
    resource_type: str
    resource_id: str
    assigned_at: Optional[str] = None
    assigned_by: Optional[str] = None


class CreateGrantRequest(BaseModel):
    group_id: str
    resource_type: str
    resource_id: str


def _grant_to_response(g: dict) -> GrantResponse:
    return GrantResponse(
        id=g["id"],
        group_id=g["group_id"],
        group_name=g.get("group_name", ""),
        resource_type=g["resource_type"],
        resource_id=g["resource_id"],
        assigned_at=str(g["assigned_at"]) if g.get("assigned_at") else None,
        assigned_by=g.get("assigned_by"),
    )


@router.get("/grants", response_model=List[GrantResponse])
async def list_grants(
    resource_type: Optional[str] = None,
    group_id: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if resource_type:
        _validate_resource_type(resource_type)
    rows = ResourceGrantsRepository(conn).list_all(
        resource_type=resource_type, group_id=group_id,
    )
    return [_grant_to_response(r) for r in rows]


@router.post("/grants", response_model=GrantResponse, status_code=201)
async def create_grant(
    payload: CreateGrantRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rt = _validate_resource_type(payload.resource_type)
    if not payload.resource_id.strip():
        raise HTTPException(status_code=400, detail="resource_id is required")
    if not UserGroupsRepository(conn).get(payload.group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    grants = ResourceGrantsRepository(conn)
    try:
        grant_id = grants.create(
            group_id=payload.group_id,
            resource_type=rt.value,
            resource_id=payload.resource_id,
            assigned_by=user.get("email"),
        )
    except duckdb.ConstraintException:
        raise HTTPException(
            status_code=409,
            detail="Grant already exists for this group/resource_type/resource_id",
        )
    _audit(
        conn, user["id"], "resource_grant.created",
        f"grant:{grant_id}",
        {
            "group_id": payload.group_id,
            "resource_type": rt.value,
            "resource_id": payload.resource_id,
        },
    )
    # Re-read with the group name joined for the response.
    rows = grants.list_all()
    fresh = next((r for r in rows if r["id"] == grant_id), None)
    if not fresh:
        raise HTTPException(status_code=500, detail="Grant created but lookup failed")
    return _grant_to_response(fresh)


@router.delete("/grants/{grant_id}", status_code=204)
async def delete_grant(
    grant_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    grants = ResourceGrantsRepository(conn)
    existing = grants.get(grant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Grant not found")
    grants.delete(grant_id)
    _audit(
        conn, user["id"], "resource_grant.deleted", f"grant:{grant_id}",
        {
            "group_id": existing["group_id"],
            "resource_type": existing["resource_type"],
            "resource_id": existing["resource_id"],
        },
    )


# ---------------------------------------------------------------------------
# User-centric views — back the /admin/users/{id} detail page.
# ---------------------------------------------------------------------------


class UserMembershipResponse(BaseModel):
    group_id: str
    group_name: str
    is_system: bool = False
    source: str
    added_at: Optional[str] = None
    added_by: Optional[str] = None


class AddUserToGroupRequest(BaseModel):
    group_id: str


@router.get(
    "/users/{user_id}/memberships",
    response_model=List[UserMembershipResponse],
)
async def list_user_memberships(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Groups a user belongs to, joined with group metadata for display.

    Includes ``source`` so the UI can distinguish admin-managed memberships
    (deletable from this page) from Google-synced or system-seeded ones
    (read-only — managed by their own writer).
    """
    if not UserRepository(conn).get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    rows = conn.execute(
        """SELECT m.group_id, g.name AS group_name, g.is_system,
                  m.source, m.added_at, m.added_by
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           WHERE m.user_id = ?
           ORDER BY g.is_system DESC, g.name""",
        [user_id],
    ).fetchall()
    return [
        UserMembershipResponse(
            group_id=r[0],
            group_name=r[1],
            is_system=bool(r[2]),
            source=r[3],
            added_at=str(r[4]) if r[4] else None,
            added_by=r[5],
        )
        for r in rows
    ]


@router.post(
    "/users/{user_id}/memberships",
    response_model=UserMembershipResponse,
    status_code=201,
)
async def add_user_to_group(
    user_id: str,
    payload: AddUserToGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Add a user to a group from the user-centric page.

    Mirror of POST /api/admin/groups/{id}/members but keyed on the user.
    Always writes ``source='admin'`` so the row survives Google sync.
    """
    if not UserRepository(conn).get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    group = UserGroupsRepository(conn).get(payload.group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    members = UserGroupMembersRepository(conn)
    if members.has_membership(user_id, payload.group_id):
        raise HTTPException(status_code=409, detail="Already a member")
    members.add_member(
        user_id=user_id,
        group_id=payload.group_id,
        source="admin",
        added_by=user.get("email"),
    )
    _audit(
        conn, user["id"], "user_group.member_added",
        f"user:{user_id}",
        {"group_id": payload.group_id, "group_name": group["name"]},
    )
    return UserMembershipResponse(
        group_id=payload.group_id,
        group_name=group["name"],
        is_system=bool(group.get("is_system", False)),
        source="admin",
        added_at=None,
        added_by=user.get("email"),
    )


@router.delete(
    "/users/{user_id}/memberships/{group_id}",
    status_code=204,
)
async def remove_user_from_group(
    user_id: str,
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove a user from a group from the user-centric page.

    Only deletes admin-source rows (Google-sync / system-seed managed
    elsewhere). Last-admin guard: refuse to remove yourself from Admin
    when you'd be the only remaining admin — keeps the system unlockable.
    """
    group = UserGroupsRepository(conn).get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["name"] == "Admin" and user_id == user["id"]:
        if UserRepository(conn).count_admins(active_only=True) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot remove yourself from Admin — you are the last admin",
            )
    members = UserGroupMembersRepository(conn)
    removed = members.remove_member(user_id, group_id, require_source="admin")
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="No admin-managed membership for this user in this group",
        )
    _audit(
        conn, user["id"], "user_group.member_removed",
        f"user:{user_id}",
        {"group_id": group_id, "group_name": group["name"]},
    )


class EffectiveAccessItem(BaseModel):
    resource_type: str
    resource_id: str
    via_groups: List[dict]  # [{group_id, group_name}]


class EffectiveAccessResponse(BaseModel):
    is_admin: bool
    items: List[EffectiveAccessItem]


@router.get(
    "/users/{user_id}/effective-access",
    response_model=EffectiveAccessResponse,
)
async def user_effective_access(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List resources the user effectively has access to, with which group
    grants each one. Admin short-circuits — if the user is in Admin, the
    response sets ``is_admin=true`` and an empty items list (UI renders a
    "Full access via Admin" pill instead of the per-resource breakdown).
    """
    if not UserRepository(conn).get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    from app.auth.access import is_user_admin
    if is_user_admin(user_id, conn):
        return EffectiveAccessResponse(is_admin=True, items=[])

    # JOIN user's group memberships with their grants. group_concat-style
    # aggregation isn't worth it — render side-by-side rows and let the UI
    # collapse same (resource_type, resource_id) into a single line.
    rows = conn.execute(
        """SELECT rg.resource_type, rg.resource_id,
                  g.id AS group_id, g.name AS group_name
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           JOIN resource_grants rg ON rg.group_id = m.group_id
           WHERE m.user_id = ?
           ORDER BY rg.resource_type, rg.resource_id, g.name""",
        [user_id],
    ).fetchall()

    grouped: dict[tuple[str, str], EffectiveAccessItem] = {}
    for rt, rid, gid, gname in rows:
        key = (rt, rid)
        if key not in grouped:
            grouped[key] = EffectiveAccessItem(
                resource_type=rt, resource_id=rid, via_groups=[],
            )
        grouped[key].via_groups.append({"group_id": gid, "group_name": gname})

    return EffectiveAccessResponse(
        is_admin=False,
        items=list(grouped.values()),
    )


# ---------------------------------------------------------------------------
# Self-service: /api/me/effective-access — non-admin can view their own.
# ---------------------------------------------------------------------------

# Separate router so it bypasses the admin gate. Mounted at /api/me/...
me_router = APIRouter(prefix="/api/me", tags=["me"])


@me_router.get(
    "/effective-access",
    response_model=EffectiveAccessResponse,
)
async def my_effective_access(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Same payload as /api/admin/users/{id}/effective-access but scoped to
    the calling user. Drives the /profile page's read-only access summary —
    so non-admin callers can self-audit without elevation."""
    user_id = user["id"]
    from app.auth.access import is_user_admin
    if is_user_admin(user_id, conn):
        return EffectiveAccessResponse(is_admin=True, items=[])

    rows = conn.execute(
        """SELECT rg.resource_type, rg.resource_id,
                  g.id AS group_id, g.name AS group_name
           FROM user_group_members m
           JOIN user_groups g ON g.id = m.group_id
           JOIN resource_grants rg ON rg.group_id = m.group_id
           WHERE m.user_id = ?
           ORDER BY rg.resource_type, rg.resource_id, g.name""",
        [user_id],
    ).fetchall()

    grouped: dict[tuple[str, str], EffectiveAccessItem] = {}
    for rt, rid, gid, gname in rows:
        key = (rt, rid)
        if key not in grouped:
            grouped[key] = EffectiveAccessItem(
                resource_type=rt, resource_id=rid, via_groups=[],
            )
        grouped[key].via_groups.append({"group_id": gid, "group_name": gname})

    return EffectiveAccessResponse(
        is_admin=False,
        items=list(grouped.values()),
    )
