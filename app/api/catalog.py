"""Catalog endpoints — table profiles, metrics."""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.rbac import can_access_table

from src.repositories import (
    profile_repo,
    table_registry_repo,
)
router = APIRouter(prefix="/api/catalog", tags=["catalog"])


class CatalogTableItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source_type: Optional[str] = None
    sync_strategy: Optional[str] = None
    query_mode: str = "local"


class CatalogTablesResponse(BaseModel):
    tables: List[CatalogTableItem]
    count: int


@router.get("/profile/{table_name}")
async def get_table_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get profiler data for a specific table."""
    # Check table-level access
    if not can_access_table(user, table_name, conn):
        raise HTTPException(status_code=403, detail=f"Access denied to table '{table_name}'")
    repo = profile_repo()
    profile = repo.get(table_name)
    if not profile:
        # Fallback: try loading from profiles.json on disk
        profiles_path = _get_data_dir() / "src_data" / "metadata" / "profiles.json"
        if profiles_path.exists():
            try:
                all_profiles = json.loads(profiles_path.read_text())
                tables = all_profiles.get("tables", all_profiles)
                if table_name in tables:
                    return tables[table_name]
            except Exception:
                pass
        raise HTTPException(status_code=404, detail=f"Profile not found for '{table_name}'")
    return profile


@router.get("/tables", response_model=CatalogTablesResponse)
async def list_catalog_tables(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all available tables from table_registry."""
    repo = table_registry_repo()
    all_tables = repo.list_all()

    # Filter by user's accessible tables. ``can_access_table`` has its own
    # admin shortcut (Admin group → True), so no need to pre-branch here.
    all_tables = [t for t in all_tables if can_access_table(user, t["id"], conn)]

    tables = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t.get("description"),
            "source_type": t.get("source_type"),
            "sync_strategy": t.get("sync_strategy"),
            "query_mode": t.get("query_mode", "local"),
        }
        for t in all_tables
    ]
    return {"tables": tables, "count": len(tables)}


@router.get("/metrics/{metric_path:path}", deprecated=True)
async def get_metric(
    metric_path: str,
    user: dict = Depends(get_current_user),
):
    """Deprecated: use GET /api/metrics/{metric_id} instead."""
    from fastapi.responses import RedirectResponse
    metric_id = metric_path.replace(".yml", "")
    return RedirectResponse(url=f"/api/metrics/{metric_id}", status_code=301)


@router.post("/profile/{table_name}/refresh")
async def refresh_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-generate profile for a table on demand."""
    # Check table-level access
    if not can_access_table(user, table_name, conn):
        raise HTTPException(status_code=403, detail=f"Access denied to table '{table_name}'")
    from src.profiler import profile_table, TableInfo

    data_dir = _get_data_dir()
    extracts_dir = data_dir / "extracts"
    candidates = list(extracts_dir.rglob(f"data/{table_name}.parquet"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No parquet for '{table_name}'")

    try:
        table_info = TableInfo(name=table_name, table_id=table_name)
        profile = profile_table(table_info, candidates[0], [], {}, {})
        profile_repo().save(table_name, profile)
        return {"status": "ok", "table": table_name, "columns": len(profile.get("columns", {}))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile failed: {e}")
