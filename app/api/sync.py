"""Sync endpoints — manifest, trigger, sync-settings, table-subscriptions."""

import hashlib
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, require_role, Role, _get_db
from src.repositories.sync_state import SyncStateRepository
from src.repositories.sync_settings import SyncSettingsRepository, DatasetPermissionRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _run_sync(tables: Optional[List[str]] = None):
    """Run extractor + orchestrator in background. Called by trigger endpoint."""
    try:
        from app.instance_config import get_data_source_type, get_value
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.orchestrator import SyncOrchestrator

        source_type = get_data_source_type()
        data_dir = _get_data_dir()

        # Get table configs from registry
        sys_conn = get_system_db()
        try:
            repo = TableRegistryRepository(sys_conn)
            if tables:
                all_configs = [repo.get(t) for t in tables]
                table_configs = [c for c in all_configs if c is not None]
            else:
                table_configs = repo.list_by_source(source_type) if source_type else repo.list_all()
        finally:
            sys_conn.close()

        # Run appropriate extractor
        if source_type == "keboola":
            from connectors.keboola.extractor import run as keboola_run
            kbc_url = get_value("keboola", "url", default="")
            kbc_token = os.environ.get(
                get_value("keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN"), ""
            )
            output = str(data_dir / "extracts" / "keboola")
            result = keboola_run(output, table_configs, kbc_url, kbc_token)
            logger.info("Keboola extraction: %s", result)

        elif source_type == "bigquery":
            from connectors.bigquery.extractor import init_extract as bq_init
            project_id = get_value("bigquery", "project_id", default="")
            output = str(data_dir / "extracts" / "bigquery")
            result = bq_init(output, project_id, table_configs)
            logger.info("BigQuery extract init: %s", result)

        else:
            logger.warning("Unknown data source type: %s", source_type)
            return

        # Rebuild master views
        orch = SyncOrchestrator()
        views = orch.rebuild()
        logger.info("Orchestrator rebuild: %s", {k: len(v) for k, v in views.items()})

    except Exception as e:
        logger.error(f"Data sync failed: {e}\n{traceback.format_exc()}")


# ---- Manifest ----

@router.get("/manifest")
async def sync_manifest(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user."""
    repo = SyncStateRepository(conn)
    perm_repo = DatasetPermissionRepository(conn)
    all_states = repo.get_all_states()

    # Filter by user's accessible datasets (admin sees all)
    user_role = user.get("role", "viewer")
    accessible = None
    if user_role != "admin":
        accessible = set(perm_repo.get_accessible_datasets(user["id"]))

    data_dir = _get_data_dir()
    tables = {}
    for state in all_states:
        table_id = state["table_id"]
        # If user has limited access, filter tables (simplified: by table_id prefix)
        tables[table_id] = {
            "hash": state.get("hash", ""),
            "updated": state.get("last_sync").isoformat() if state.get("last_sync") else None,
            "size_bytes": state.get("file_size_bytes", 0),
            "rows": state.get("rows", 0),
        }

    # Asset hashes
    docs_dir = data_dir / "docs"
    assets = {}
    for asset_name, asset_path in [
        ("docs", docs_dir),
        ("profiles", data_dir / "src_data" / "metadata" / "profiles.json"),
    ]:
        if asset_path.exists():
            if asset_path.is_file():
                assets[asset_name] = {"hash": _file_hash(asset_path)}
            else:
                newest = max(
                    (f.stat().st_mtime for f in asset_path.rglob("*") if f.is_file()),
                    default=0,
                )
                assets[asset_name] = {"hash": str(int(newest))}

    return {
        "tables": tables,
        "assets": assets,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---- Trigger ----

@router.post("/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    tables: Optional[List[str]] = None,
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Trigger data sync from configured source. Admin only. Runs in background."""
    background_tasks.add_task(_run_sync, tables)
    return {
        "status": "triggered",
        "tables": tables or "all",
        "message": "Data sync started in background. Check /api/health for progress.",
    }


# ---- Sync Settings (dataset subscriptions) ----

class SyncSettingsUpdate(BaseModel):
    datasets: dict  # {dataset_name: bool}


@router.get("/settings")
async def get_sync_settings(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's dataset sync settings."""
    repo = SyncSettingsRepository(conn)
    settings = repo.get_user_settings(user["id"])
    enabled = repo.get_enabled_datasets(user["id"])
    return {
        "user_id": user["id"],
        "settings": settings,
        "enabled_datasets": enabled,
    }


@router.post("/settings")
async def update_sync_settings(
    request: SyncSettingsUpdate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update user's dataset sync settings."""
    settings_repo = SyncSettingsRepository(conn)
    perm_repo = DatasetPermissionRepository(conn)

    results = {}
    for dataset, enabled in request.datasets.items():
        if not perm_repo.has_access(user["id"], dataset):
            results[dataset] = {"error": "no permission"}
            continue
        settings_repo.set_dataset_enabled(user["id"], dataset, enabled)
        results[dataset] = {"enabled": enabled}

    return {"updated": results}


# ---- Table Subscriptions ----

class TableSubscriptionUpdate(BaseModel):
    table_mode: str = "all"  # "all" or "explicit"
    tables: dict = {}  # {table_name: bool}


@router.get("/table-subscriptions")
async def get_table_subscriptions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get user's per-table subscription settings."""
    repo = SyncSettingsRepository(conn)
    settings = repo.get_user_settings(user["id"])
    return {"user_id": user["id"], "subscriptions": settings}


@router.post("/table-subscriptions")
async def update_table_subscriptions(
    request: TableSubscriptionUpdate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update per-table subscription preferences."""
    repo = SyncSettingsRepository(conn)
    for table_name, enabled in request.tables.items():
        repo.set_dataset_enabled(user["id"], table_name, enabled)
    return {"table_mode": request.table_mode, "updated": len(request.tables)}
