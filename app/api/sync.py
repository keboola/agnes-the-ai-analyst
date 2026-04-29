"""Sync endpoints — manifest, trigger, sync-settings, table-subscriptions."""

import hashlib
import logging
import os
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.repositories.sync_state import SyncStateRepository
from src.repositories.sync_settings import SyncSettingsRepository, DatasetPermissionRepository
from src.repositories.table_registry import TableRegistryRepository
from src.rbac import can_access_table
from src.scheduler import filter_due_tables

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


def _run_sync(tables: Optional[List[str]] = None):
    """Run extractor as subprocess + orchestrator rebuild.

    Reads table configs from DuckDB (in main process which has the shared
    connection), passes them as JSON via stdin to the extractor subprocess.
    This avoids DuckDB lock conflicts — subprocess never opens system.duckdb.
    """
    import json as _json
    import sys

    try:
        from app.instance_config import get_data_source_type, get_value
        from src.db import get_system_db

        source_type = get_data_source_type()
        data_dir = _get_data_dir()

        # Read table configs in main process (has shared DuckDB connection)
        sys_conn = get_system_db()
        try:
            repo = TableRegistryRepository(sys_conn)
            if tables:
                # Manual operator override — bypass schedule filter entirely
                # so an admin saying "sync these specific tables now" wins.
                all_configs = [repo.get(t) for t in tables]
                table_configs = [c for c in all_configs if c is not None]
            else:
                table_configs = repo.list_local(source_type) if source_type else repo.list_local()
                # Without this filter, every scheduler tick would re-sync
                # every table regardless of its sync_schedule cadence,
                # making the field a no-op at trigger time. Tables with
                # no schedule pass through unchanged (opt-in feature).
                state_repo = SyncStateRepository(sys_conn)
                table_configs = filter_due_tables(table_configs, state_repo)
        finally:
            sys_conn.close()

        if not table_configs:
            # Auto-discover tables on first sync when registry is empty
            if source_type == "keboola" and os.environ.get("KEBOOLA_STORAGE_TOKEN"):
                logger.info("No tables registered — running auto-discovery from Keboola")
                try:
                    from app.api.admin import _discover_and_register_tables
                    auto_conn = get_system_db()
                    try:
                        result = _discover_and_register_tables(auto_conn, "auto-discovery")
                        logger.info("Auto-discovered %d tables, skipped %d", result["registered"], result["skipped"])
                    finally:
                        auto_conn.close()
                    # Re-read table configs after auto-registration
                    sys_conn2 = get_system_db()
                    try:
                        table_configs = TableRegistryRepository(sys_conn2).list_local(source_type)
                    finally:
                        sys_conn2.close()
                except Exception as e:
                    logger.warning("Auto-discovery failed: %s", e)

            if not table_configs:
                logger.warning("No tables to sync for source_type=%s", source_type)
                return

        # Serialize configs — strip non-serializable fields
        serializable = []
        for tc in table_configs:
            serializable.append({k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                                 for k, v in tc.items() if v is not None})

        # Run extractor subprocess with table configs via stdin
        # Subprocess does NOT open system.duckdb — no lock conflict
        env = {**os.environ}
        cmd = [sys.executable, "-c", """
import json, sys, os, logging
from pathlib import Path
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

configs = json.load(sys.stdin)
url = os.environ.get("KEBOOLA_STACK_URL", "")
token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

if not url or not token:
    print("ERROR: Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN", file=sys.stderr)
    sys.exit(1)

from connectors.keboola.extractor import run, compute_exit_code
data_dir = Path(os.environ.get("DATA_DIR", "./data"))
result = run(str(data_dir / "extracts" / "keboola"), configs, url, token)
print(json.dumps(result))
# Issue #81 Group B: surface partial-failure as exit 2 so the API
# caller can distinguish "every table failed" from "9/10 succeeded".
sys.exit(compute_exit_code(result, len(configs)))
"""]

        import sys as _sys
        print(f"[SYNC] Starting extractor subprocess for {len(table_configs)} tables", file=_sys.stderr, flush=True)

        result = subprocess.run(
            cmd, input=_json.dumps(serializable), capture_output=True, text=True,
            timeout=1800, env=env,
            cwd=str(Path(__file__).parent.parent.parent),
        )

        if result.stdout:
            print(f"[SYNC] Extractor stdout: {result.stdout.strip()[-500:]}", file=_sys.stderr, flush=True)
        if result.stderr:
            print(f"[SYNC] Extractor stderr: {result.stderr[-500:]}", file=_sys.stderr, flush=True)
        # Issue #81 Group B: three exit codes. 0 = full success,
        # 1 = full failure, 2 = partial. Partial is a data-quality
        # alert, not a crash — the orchestrator's per-table _meta
        # machinery already captured which tables succeeded; we just
        # need to log loudly so operator alerting can pick it up.
        if result.returncode == 0:
            print(f"[SYNC] Extractor OK", file=_sys.stderr, flush=True)
        elif result.returncode == 2:
            print(
                f"[SYNC] Extractor PARTIAL FAILURE (exit 2) — some tables "
                f"succeeded, some failed; see stderr for per-table errors. "
                f"Successful tables will still be published by the orchestrator.",
                file=_sys.stderr, flush=True,
            )
        else:
            print(f"[SYNC] Extractor FAILED (exit {result.returncode})", file=_sys.stderr, flush=True)

        # Run custom connectors (Tier A: local mount)
        connectors_dir = Path(os.environ.get("CONNECTORS_DIR", str(Path(__file__).parent.parent.parent / "connectors" / "custom")))
        if connectors_dir.exists():
            for connector_dir in sorted(connectors_dir.iterdir()):
                if not connector_dir.is_dir():
                    continue
                extractor = connector_dir / "extractor.py"
                if not extractor.exists():
                    continue
                logger.info("Running custom connector: %s", connector_dir.name)
                try:
                    custom_result = subprocess.run(
                        [sys.executable, str(extractor)],
                        env=env, capture_output=True, text=True, timeout=600,
                        cwd=str(Path(__file__).parent.parent.parent),
                    )
                    if custom_result.returncode != 0:
                        logger.error("Custom connector %s failed: %s", connector_dir.name, custom_result.stderr[-500:])
                    else:
                        logger.info("Custom connector %s completed", connector_dir.name)
                except subprocess.TimeoutExpired:
                    logger.error("Custom connector %s timed out", connector_dir.name)

        # Rebuild master views (reads extract.duckdb files, no write conflict)
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator()
        views = orch.rebuild()
        print(f"[SYNC] Orchestrator rebuild: {{{', '.join(f'{k}: {len(v)}' for k, v in views.items())}}}", file=_sys.stderr, flush=True)

        # Auto-profile synced tables (best-effort, don't fail sync on profile error)
        try:
            from src.profiler import profile_table, TableInfo
            from src.repositories.profiles import ProfileRepository

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            extracts_dir = data_dir / "extracts"

            sys_conn = get_system_db()
            try:
                profile_repo = ProfileRepository(sys_conn)
                profiled = 0
                for source_name, table_names in views.items():
                    for table_name in table_names[:10]:  # Limit per sync
                        pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                        if not pq_path.exists():
                            continue
                        try:
                            table_info = TableInfo(name=table_name, table_id=table_name)
                            profile = profile_table(table_info, pq_path, [], {}, {})
                            profile_repo.save(table_name, profile)
                            profiled += 1
                        except Exception as pe:
                            print(f"[SYNC] Profile {table_name}: {pe}", file=_sys.stderr, flush=True)
                print(f"[SYNC] Profiled {profiled} tables", file=_sys.stderr, flush=True)
            finally:
                sys_conn.close()
        except Exception as e:
            print(f"[SYNC] Profiler skipped: {e}", file=_sys.stderr, flush=True)

    except subprocess.TimeoutExpired:
        print("[SYNC] Extractor timed out after 1800s", file=_sys.stderr, flush=True)
    except Exception as e:
        print(f"[SYNC] FAILED: {e}", file=_sys.stderr, flush=True)
        traceback.print_exc()


# ---- Manifest ----

def _build_manifest_for_user(conn, user: dict) -> dict:
    """Build manifest dict filtered by user's accessible tables.

    Joins ``sync_state`` with ``table_registry`` so each table entry exposes
    ``query_mode`` and ``source_type``. The CLI uses these to decide whether
    to download a parquet (local) or skip it (remote, e.g. BigQuery views).

    Defensive defaults: if a sync_state row has no matching registry entry
    (race / manual deletion), fall back to ``query_mode='local'`` and
    ``source_type=''`` so the manifest still serializes cleanly.
    """
    sync_repo = SyncStateRepository(conn)
    table_repo = TableRegistryRepository(conn)
    all_states = sync_repo.get_all_states()
    # `sync_state.table_id` is sourced from `_meta.table_name` which equals
    # `table_registry.name`, NOT `table_registry.id`. Auto-discovered Keboola
    # tables and manually-registered ones with mixed-case/spaced names produce
    # id != name; an id-keyed lookup would miss them and silently default to
    # `query_mode=local`, causing the CLI to try downloading remote tables.
    registry_by_name = {t["name"]: t for t in table_repo.list_all()}

    # Filter by user's accessible tables (admin sees all). `can_access_table`
    # internally does a registry lookup BY ID, but `s["table_id"]` is sourced
    # from `_meta.table_name` = registry `name`. When id != name we need to
    # translate name→id first, or the RBAC lookup misses and falls through.
    if user.get("role") != "admin":
        def _id_for(state):
            reg = registry_by_name.get(state["table_id"])
            return reg["id"] if reg else state["table_id"]
        all_states = [s for s in all_states if can_access_table(user, _id_for(s), conn)]

    data_dir = _get_data_dir()
    tables = {}
    for state in all_states:
        table_id = state["table_id"]
        reg = registry_by_name.get(table_id, {})
        tables[table_id] = {
            "hash": state.get("hash", ""),
            "updated": state.get("last_sync").isoformat() if state.get("last_sync") else None,
            "size_bytes": state.get("file_size_bytes", 0),
            "rows": state.get("rows", 0),
            "query_mode": reg.get("query_mode") or "local",
            "source_type": reg.get("source_type") or "",
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


@router.get("/manifest")
async def sync_manifest(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return hash-based manifest of all synced data, filtered per user."""
    return _build_manifest_for_user(conn, user)


# ---- Trigger ----

@router.post("/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    tables: Optional[List[str]] = None,
    user: dict = Depends(require_admin),
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
