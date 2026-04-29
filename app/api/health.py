"""Health check endpoint — structured diagnostics for AI agents."""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
import duckdb

from app.auth.dependencies import _get_db, get_current_user
from src.db import SCHEMA_VERSION, get_system_db
from src.repositories.sync_state import SyncStateRepository

router = APIRouter(tags=["health"])

# Captured at module import (i.e., app process start) — proxy for "deployed at".
# When the cron auto-upgrade pulls a new digest and recreates the container,
# this resets. Accurate enough for a UI "last updated" badge.
_DEPLOYED_AT = datetime.now(timezone.utc).isoformat()


def _check_db_schema() -> dict:
    """Check DB schema version against expected SCHEMA_VERSION.

    Returns a dict with 'db_schema' key and optional 'detail' key.
    """
    try:
        conn = get_system_db()
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"db_schema": "mismatch", "detail": "no schema_version row found"}
        current_version = row[0]
        if current_version == SCHEMA_VERSION:
            return {"db_schema": "ok", "current": current_version, "expected": SCHEMA_VERSION}
        else:
            return {"db_schema": "mismatch", "current": current_version, "expected": SCHEMA_VERSION}
    except Exception as e:
        return {"db_schema": "unreachable", "detail": str(e)}


@router.get("/api/health")
async def health_check():
    """Minimal health check for load balancers / compose healthcheck. No auth required."""
    schema_check = _check_db_schema()
    status = "ok"
    if schema_check["db_schema"] != "ok":
        status = "unhealthy"
    return {"status": status, **schema_check}


@router.get("/api/health/detailed")
async def health_check_detailed(
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    _user: dict = Depends(get_current_user),
):
    """Structured health check with deployment metadata. Requires authentication."""
    checks = {}

    # DuckDB state
    try:
        conn.execute("SELECT 1").fetchone()
        checks["duckdb_state"] = {"status": "ok"}
    except Exception as e:
        checks["duckdb_state"] = {"status": "error", "detail": str(e)}

    # DB schema version check
    checks["db_schema"] = _check_db_schema()

    # Sync state summary
    try:
        repo = SyncStateRepository(conn)
        all_states = repo.get_all_states()
        total_tables = len(all_states)
        total_rows = sum(s.get("rows", 0) or 0 for s in all_states)
        stale = []
        now = datetime.now(timezone.utc)
        for s in all_states:
            last = s.get("last_sync")
            if last:
                try:
                    # Handle both tz-aware and tz-naive datetimes from DuckDB
                    if hasattr(last, 'tzinfo') and last.tzinfo is None:
                        from datetime import timezone as tz
                        last = last.replace(tzinfo=tz.utc)
                    if (now - last).total_seconds() > 86400:
                        stale.append(s["table_id"])
                except (TypeError, AttributeError):
                    pass  # skip if timestamp comparison fails
        checks["data"] = {
            "status": "ok" if not stale else "warning",
            "tables": total_tables,
            "total_rows": total_rows,
            "stale_tables": stale,
        }
    except Exception as e:
        checks["data"] = {"status": "error", "detail": str(e)}

    # User count
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        checks["users"] = {"status": "ok", "count": user_count}
    except Exception as e:
        checks["users"] = {"status": "error", "detail": str(e)}

    overall = "healthy"
    for check in checks.values():
        if check.get("status") == "error":
            overall = "unhealthy"
            break
        if check.get("status") == "warning":
            overall = "degraded"
    # DB schema mismatch or unreachable also makes the overall status unhealthy
    if checks.get("db_schema", {}).get("db_schema") != "ok":
        overall = "unhealthy"

    return {
        "status": overall,
        "version": os.environ.get("AGNES_VERSION", "dev"),
        "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
        "image_tag": os.environ.get("AGNES_TAG", "unknown"),
        "commit_sha": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        "schema_version": SCHEMA_VERSION,
        "deployed_at": _DEPLOYED_AT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    }


@router.get("/api/version")
async def version_info():
    """Lightweight version info — cacheable, no DB touch. Used by UI footer badge."""
    return {
        "version": os.environ.get("AGNES_VERSION", "dev"),
        "channel": os.environ.get("RELEASE_CHANNEL", "dev"),
        "image_tag": os.environ.get("AGNES_TAG", "unknown"),
        "commit_sha": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        "schema_version": SCHEMA_VERSION,
        "deployed_at": _DEPLOYED_AT,
    }
