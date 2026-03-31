"""Admin endpoints — table discovery, registry management."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import duckdb

from app.auth.dependencies import require_role, Role, _get_db
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = "full_refresh"
    primary_key: Optional[str] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = True


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = None
    primary_key: Optional[str] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = None


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Discover all available tables from the configured data source."""
    try:
        from app.instance_config import get_data_source_type
        source_type = get_data_source_type()

        if source_type == "keboola":
            from connectors.keboola.client import KeboolaClient
            import os
            from app.instance_config import get_value
            url = get_value("keboola", "url", default="")
            token = os.environ.get(get_value("keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN"), "")
            client = KeboolaClient(token=token, url=url)
            tables = client.discover_all_tables()
            return {"tables": tables, "count": len(tables), "source": "keboola"}
        else:
            return {"tables": [], "count": 0, "source": source_type, "error": "Discovery not implemented for this source"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry."""
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()
    return {"tables": tables, "count": len(tables)}


@router.post("/register-table", status_code=201)
async def register_table(
    request: RegisterTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system."""
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")
    repo = TableRegistryRepository(conn)
    table_id = request.name.strip().lower().replace(" ", "_")

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
        source_type=request.source_type,
        bucket=request.bucket,
        source_table=request.source_table,
        query_mode=request.query_mode,
        sync_schedule=request.sync_schedule,
        profile_after_sync=request.profile_after_sync,
    )

    return {"id": table_id, "name": request.name, "status": "registered"}


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if updates:
        existing = repo.get(table_id)
        merged = {k: v for k, v in existing.items() if k != "registered_at"}
        merged.update(updates)
        merged.pop("id", None)  # avoid duplicate id kwarg
        repo.register(id=table_id, **merged)
    return {"id": table_id, "updated": list(updates.keys())}


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system."""
    repo = TableRegistryRepository(conn)
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="Table not found")
    repo.unregister(table_id)
