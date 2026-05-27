"""User sync settings endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import duckdb

from app.auth.access import can_access
from app.auth.dependencies import get_current_user, _get_db
from app.resource_types import ResourceType
from src.repositories.sync_settings import SyncSettingsRepository

router = APIRouter(prefix="/api/settings", tags=["settings"])


class DatasetSettingRequest(BaseModel):
    dataset: str
    enabled: bool


@router.get("")
async def get_settings(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get current user's sync settings.

    The legacy ``permissions`` field that mirrored ``dataset_permissions``
    was removed in v19 — table access is now via ``resource_grants``,
    queryable through ``GET /api/me/effective-access``.
    """
    settings_repo = SyncSettingsRepository(conn)
    return {
        "user_id": user["id"],
        "sync_settings": settings_repo.get_user_settings(user["id"]),
    }


@router.put("/dataset")
async def update_dataset_setting(
    request: DatasetSettingRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Enable or disable a dataset for sync.

    Gate: the user must have a matching ``resource_grants`` row (or be
    Admin). The user_sync_settings layer is per-user preference, not
    authorization — gating the toggle here stops users from enabling
    sync on tables they cannot read.
    """
    if not can_access(user["id"], ResourceType.TABLE.value, request.dataset, conn):
        raise HTTPException(status_code=403, detail=f"No access to dataset '{request.dataset}'")

    settings_repo = SyncSettingsRepository(conn)
    settings_repo.set_dataset_enabled(user["id"], request.dataset, request.enabled)
    return {"dataset": request.dataset, "enabled": request.enabled}
