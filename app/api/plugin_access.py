"""Admin endpoints for user groups and plugin-access grants.

Two resources, both admin-only:

  - /api/user-groups       → CRUD over named groups (id, name, description)
  - /api/plugin-access     → grants of (group_id, marketplace_id, plugin_name)

Consumers (e.g. a future per-group dynamic marketplace endpoint) read the
grants joined against `marketplace_plugins` to materialise per-group
plugin lists.
"""

import logging
from datetime import datetime
from typing import List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import Role, _get_db, require_role
from src.repositories.audit import AuditRepository
from src.repositories.marketplace_plugins import MarketplacePluginsRepository
from src.repositories.marketplace_registry import MarketplaceRegistryRepository
from src.repositories.plugin_access import (
    PluginAccessRepository,
    SystemGroupProtected,
    UserGroupsRepository,
)

logger = logging.getLogger(__name__)

groups_router = APIRouter(prefix="/api/user-groups", tags=["user-groups"])
access_router = APIRouter(prefix="/api/plugin-access", tags=["plugin-access"])


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe: Optional[dict] = None
        if params:
            safe = {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in params.items()
            }
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=resource, params=safe
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------


class GroupResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_system: bool = False
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    member_count: int = 0  # number of plugin grants for this group


class CreateGroupRequest(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


def _group_to_response(row: dict, grant_count: int = 0) -> GroupResponse:
    return GroupResponse(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        is_system=bool(row.get("is_system")),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        created_by=row.get("created_by"),
        member_count=grant_count,
    )


def _grant_counts_by_group(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT group_id, COUNT(*) FROM plugin_access GROUP BY group_id"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


@groups_router.get("", response_model=List[GroupResponse])
async def list_groups(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    counts = _grant_counts_by_group(conn)
    return [
        _group_to_response(g, counts.get(g["id"], 0))
        for g in UserGroupsRepository(conn).list_all()
    ]


@groups_router.post("", response_model=GroupResponse, status_code=201)
async def create_group(
    payload: CreateGroupRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    repo = UserGroupsRepository(conn)
    if repo.get_by_name(name):
        raise HTTPException(status_code=409, detail=f"group '{name}' already exists")
    created = repo.create(
        name=name,
        description=(payload.description or None),
        created_by=user.get("email"),
    )
    _audit(conn, user["id"], "user_group.create", f"group:{created['id']}", {"name": name})
    return _group_to_response(created, 0)


@groups_router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: str,
    payload: UpdateGroupRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserGroupsRepository(conn)
    existing = repo.get(group_id)
    if not existing:
        raise HTTPException(status_code=404, detail="group not found")

    new_name: Optional[str] = None
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        clash = repo.get_by_name(new_name)
        if clash and clash["id"] != group_id:
            raise HTTPException(
                status_code=409, detail=f"group '{new_name}' already exists"
            )

    try:
        repo.update(
            group_id,
            name=new_name,
            description=payload.description if payload.description is not None else None,
        )
    except SystemGroupProtected as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    _audit(
        conn,
        user["id"],
        "user_group.update",
        f"group:{group_id}",
        {"name": new_name, "description": payload.description},
    )
    counts = _grant_counts_by_group(conn)
    return _group_to_response(repo.get(group_id), counts.get(group_id, 0))


@groups_router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = UserGroupsRepository(conn)
    if not repo.get(group_id):
        raise HTTPException(status_code=404, detail="group not found")
    try:
        repo.delete(group_id)
    except SystemGroupProtected as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    _audit(conn, user["id"], "user_group.delete", f"group:{group_id}")


# ---------------------------------------------------------------------------
# Plugin access grants
# ---------------------------------------------------------------------------


class AccessGrant(BaseModel):
    group_id: str
    marketplace_id: str
    plugin_name: str
    granted_at: Optional[str] = None
    granted_by: Optional[str] = None


class GrantRequest(BaseModel):
    group_id: str
    marketplace_id: str
    plugin_name: str


class GrantBulkRequest(BaseModel):
    group_id: str
    # Full desired set for this group; anything not listed is revoked.
    grants: List[dict]  # each: {"marketplace_id": str, "plugin_name": str}


@access_router.get("", response_model=List[AccessGrant])
async def list_access(
    group_id: Optional[str] = None,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = PluginAccessRepository(conn)
    rows = repo.list_for_group(group_id) if group_id else repo.list_all()
    return [
        AccessGrant(
            group_id=r["group_id"],
            marketplace_id=r["marketplace_id"],
            plugin_name=r["plugin_name"],
            granted_at=str(r["granted_at"]) if r.get("granted_at") else None,
            granted_by=r.get("granted_by"),
        )
        for r in rows
    ]


def _ensure_plugin_exists(
    conn: duckdb.DuckDBPyConnection, marketplace_id: str, plugin_name: str
) -> None:
    if not MarketplaceRegistryRepository(conn).get(marketplace_id):
        raise HTTPException(
            status_code=404, detail=f"marketplace '{marketplace_id}' not found"
        )
    row = conn.execute(
        "SELECT 1 FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=(
                f"plugin '{plugin_name}' not found in marketplace '{marketplace_id}' — "
                "sync the marketplace first so its plugin list is cached"
            ),
        )


@access_router.post("", status_code=204)
async def grant_access(
    payload: GrantRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not UserGroupsRepository(conn).get(payload.group_id):
        raise HTTPException(status_code=404, detail="group not found")
    _ensure_plugin_exists(conn, payload.marketplace_id, payload.plugin_name)
    PluginAccessRepository(conn).grant(
        payload.group_id,
        payload.marketplace_id,
        payload.plugin_name,
        granted_by=user.get("email"),
    )
    _audit(
        conn,
        user["id"],
        "plugin_access.grant",
        f"group:{payload.group_id}",
        {"marketplace": payload.marketplace_id, "plugin": payload.plugin_name},
    )


@access_router.delete("", status_code=204)
async def revoke_access(
    payload: GrantRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    PluginAccessRepository(conn).revoke(
        payload.group_id, payload.marketplace_id, payload.plugin_name
    )
    _audit(
        conn,
        user["id"],
        "plugin_access.revoke",
        f"group:{payload.group_id}",
        {"marketplace": payload.marketplace_id, "plugin": payload.plugin_name},
    )


@access_router.put("", status_code=204)
async def replace_access_for_group(
    payload: GrantBulkRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Replace the complete grant set for a single group.

    Anything in `grants` is kept/added; anything currently granted but
    absent from `grants` is revoked. Useful for a "Save" button on the
    admin UI that tracks the whole matrix.
    """
    group_repo = UserGroupsRepository(conn)
    if not group_repo.get(payload.group_id):
        raise HTTPException(status_code=404, detail="group not found")

    desired: set[tuple[str, str]] = set()
    for g in payload.grants:
        m = (g.get("marketplace_id") or "").strip()
        p = (g.get("plugin_name") or "").strip()
        if not m or not p:
            raise HTTPException(
                status_code=400,
                detail="each grant must include marketplace_id and plugin_name",
            )
        _ensure_plugin_exists(conn, m, p)
        desired.add((m, p))

    access = PluginAccessRepository(conn)
    current = {(r["marketplace_id"], r["plugin_name"]) for r in access.list_for_group(payload.group_id)}

    to_add = desired - current
    to_remove = current - desired

    email = user.get("email")
    for m, p in to_add:
        access.grant(payload.group_id, m, p, granted_by=email)
    for m, p in to_remove:
        access.revoke(payload.group_id, m, p)

    _audit(
        conn,
        user["id"],
        "plugin_access.replace",
        f"group:{payload.group_id}",
        {"added": len(to_add), "removed": len(to_remove), "total": len(desired)},
    )


# ---------------------------------------------------------------------------
# Aggregated view — everything the admin page needs in one round-trip
# ---------------------------------------------------------------------------


class PluginAccessOverview(BaseModel):
    groups: List[GroupResponse]
    marketplaces: List[dict]  # [{id, name, plugin_count, plugins: [...]}, ...]
    grants: List[AccessGrant]


@access_router.get("/overview", response_model=PluginAccessOverview)
async def access_overview(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    plugins_repo = MarketplacePluginsRepository(conn)
    groups_repo = UserGroupsRepository(conn)
    registry = MarketplaceRegistryRepository(conn)

    counts = _grant_counts_by_group(conn)
    groups = [_group_to_response(g, counts.get(g["id"], 0)) for g in groups_repo.list_all()]

    plugin_counts = plugins_repo.count_by_marketplace()
    marketplaces = []
    for m in registry.list_all():
        plugs = plugins_repo.list_for_marketplace(m["id"])
        marketplaces.append(
            {
                "id": m["id"],
                "name": m["name"],
                "url": m["url"],
                "plugin_count": plugin_counts.get(m["id"], 0),
                "plugins": [
                    {
                        "name": p["name"],
                        "description": p.get("description"),
                        "version": p.get("version"),
                        "source_type": p.get("source_type"),
                        "category": p.get("category"),
                    }
                    for p in plugs
                ],
            }
        )

    grants = [
        AccessGrant(
            group_id=r["group_id"],
            marketplace_id=r["marketplace_id"],
            plugin_name=r["plugin_name"],
            granted_at=str(r["granted_at"]) if r.get("granted_at") else None,
            granted_by=r.get("granted_by"),
        )
        for r in PluginAccessRepository(conn).list_all()
    ]
    return PluginAccessOverview(
        groups=groups, marketplaces=marketplaces, grants=grants
    )
