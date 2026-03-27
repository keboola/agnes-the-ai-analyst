"""Catalog endpoints — table profiles, metrics."""

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
import duckdb
import yaml

from app.auth.dependencies import get_current_user, _get_db
from src.repositories.profiles import ProfileRepository

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


@router.get("/profile/{table_name}")
async def get_table_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get profiler data for a specific table."""
    repo = ProfileRepository(conn)
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


@router.get("/tables")
async def list_catalog_tables(
    user: dict = Depends(get_current_user),
):
    """List all available tables from data_description.md."""
    try:
        from src.config import get_config
        config = get_config()
        tables = []
        for tc in config.tables:
            tables.append({
                "id": tc.id,
                "name": tc.name,
                "description": tc.description,
                "dataset": getattr(tc, "dataset", None),
                "sync_strategy": tc.sync_strategy,
                "query_mode": getattr(tc, "query_mode", "local"),
            })
        return {"tables": tables, "count": len(tables)}
    except Exception as e:
        return {"tables": [], "count": 0, "error": str(e)}


@router.get("/metrics/{metric_path:path}")
async def get_metric(
    metric_path: str,
    user: dict = Depends(get_current_user),
):
    """Get a metric YAML definition parsed as structured JSON."""
    if not re.match(r"^[a-z_]+/[a-z_]+\.yml$", metric_path):
        raise HTTPException(status_code=400, detail="Invalid metric path")

    docs_dir = _get_data_dir() / "docs" / "metrics"
    file_path = docs_dir / metric_path

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Metric file not found")

    # Security: ensure path doesn't escape docs dir
    if not file_path.resolve().is_relative_to(docs_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")

    try:
        content = yaml.safe_load(file_path.read_text())
        return content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parsing metric: {e}")
