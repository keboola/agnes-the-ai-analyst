"""Health check endpoint — structured diagnostics for AI agents."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
import duckdb

from app.auth.dependencies import _get_db
from src.repositories.sync_state import SyncStateRepository

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health_check(conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    """Structured health check. No auth required."""
    checks = {}

    # DuckDB state
    try:
        conn.execute("SELECT 1").fetchone()
        checks["duckdb_state"] = {"status": "ok"}
    except Exception as e:
        checks["duckdb_state"] = {"status": "error", "detail": str(e)}

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

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    }
