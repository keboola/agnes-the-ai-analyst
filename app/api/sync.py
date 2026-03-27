"""Sync endpoints — manifest, trigger."""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, require_role, Role, _get_db
from src.repositories.sync_state import SyncStateRepository

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _file_hash(path: Path) -> str:
    """Compute MD5 hash of a file for change detection."""
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


@router.get("/manifest")
async def sync_manifest(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user."""
    repo = SyncStateRepository(conn)
    all_states = repo.get_all_states()

    data_dir = _get_data_dir()
    parquet_dir = data_dir / "src_data" / "parquet"

    # Build table manifest
    tables = {}
    for state in all_states:
        table_id = state["table_id"]
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
                # Directory — hash based on mtime of newest file
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


@router.post("/trigger")
async def trigger_sync(
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger data sync from configured source. Admin only."""
    # This will call DataSyncManager when integrated
    # For now, return a stub response
    return {
        "status": "triggered",
        "message": "Data sync triggered. Check /api/health for progress.",
    }
