"""User sync settings endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.repositories.sync_settings import SyncSettingsRepository, DatasetPermissionRepository

router = APIRouter(prefix="/api/settings", tags=["settings"])


class DatasetSettingRequest(BaseModel):
    dataset: str
    enabled: bool


@router.get("")
async def get_settings(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get current user's sync settings and permissions."""
    settings_repo = SyncSettingsRepository(conn)
    perm_repo = DatasetPermissionRepository(conn)

    settings = settings_repo.get_user_settings(user["id"])
    permissions = perm_repo.get_user_permissions(user["id"])

    return {
        "user_id": user["id"],
        "sync_settings": settings,
        "permissions": permissions,
    }


@router.put("/dataset")
async def update_dataset_setting(
    request: DatasetSettingRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Enable or disable a dataset for sync."""
    # Check permission
    perm_repo = DatasetPermissionRepository(conn)
    if not perm_repo.has_access(user["id"], request.dataset):
        raise HTTPException(status_code=403, detail=f"No access to dataset '{request.dataset}'")

    settings_repo = SyncSettingsRepository(conn)
    settings_repo.set_dataset_enabled(user["id"], request.dataset, request.enabled)
    return {"dataset": request.dataset, "enabled": request.enabled}
