"""Admin permissions API — grant/revoke dataset access."""

import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import require_role, get_current_user, Role, _get_db
from src.repositories.sync_settings import DatasetPermissionRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/permissions", tags=["permissions"])


class PermissionRequest(BaseModel):
    user_id: str
    dataset: str  # table_id, bucket wildcard, or dataset group
    access: str = "read"  # "read" or "none"


@router.post("", status_code=201)
async def grant_permission(
    request: PermissionRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Grant a user access to a dataset/table."""
    repo = DatasetPermissionRepository(conn)
    repo.grant(request.user_id, request.dataset, request.access)
    return {"user_id": request.user_id, "dataset": request.dataset, "access": request.access}


@router.delete("")
async def revoke_permission(
    request: PermissionRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke a user's access to a dataset/table."""
    repo = DatasetPermissionRepository(conn)
    repo.revoke(request.user_id, request.dataset)
    return {"revoked": True}


@router.get("/{user_id}")
async def get_user_permissions(
    user_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all permissions for a user."""
    repo = DatasetPermissionRepository(conn)
    permissions = repo.get_user_permissions(user_id)
    return {"user_id": user_id, "permissions": permissions}


@router.get("")
async def list_all_permissions(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all dataset permissions."""
    results = conn.execute("SELECT * FROM dataset_permissions ORDER BY user_id, dataset").fetchall()
    if not results:
        return {"permissions": []}
    columns = [desc[0] for desc in conn.description]
    return {"permissions": [dict(zip(columns, row)) for row in results]}
