"""Health check endpoint — structured diagnostics for AI agents."""

import os
from datetime import datetime, timezone
from pathlib import Path

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


def _check_bq_billing_project() -> dict | None:
    """Surface the USER_PROJECT_DENIED footgun when a BQ instance has
    `billing_project` falling back to (or explicitly equal to) `project`.

    Background: connectors/bigquery/access.py:339-342 lets `billing` default
    to `data` when `billing_project` is unset. A service account with
    `roles/bigquery.dataViewer` on the data project but no
    `serviceusage.services.use` on it then 403s on every BQ call with
    USER_PROJECT_DENIED. The config is technically valid, so we warn rather
    than error — the operator's billable project must be set distinctly.

    Returns:
      None when the check doesn't apply (non-BQ instance, or BQ deps missing).
      A service-entry dict otherwise: {"status": "ok"} or
      {"status": "warning", "detail": ..., "hint": ..., "billing_project": ...,
       "data_project": ...}.
    """
    try:
        from app.instance_config import get_data_source_type
    except Exception:
        return None
    if (get_data_source_type() or "").lower() != "bigquery":
        return None

    try:
        from connectors.bigquery.access import get_bq_access
        bq = get_bq_access()
        billing = bq.projects.billing
        data = bq.projects.data
    except Exception as e:
        # Resolution failure (missing google-cloud-bigquery, auth error,
        # malformed config) is itself a problem worth surfacing. Returning
        # status='ok' would mask the failure from automated alerting that
        # keys on `status != 'ok'`. Use 'unknown' so the entry shows as
        # non-green in operator dashboards but doesn't promote the overall
        # check to 'degraded' (which 'warning' does). Devin finding
        # 2026-05-01: ANALYSIS_pr-review-job-642ff90f_0007.
        return {
            "status": "unknown",
            "detail": f"could not resolve BQ projects: {e}",
        }

    if not data:
        # not_configured sentinel — surfaced elsewhere; nothing to warn about here.
        return {"status": "ok", "detail": "BigQuery project not configured"}

    if billing == data:
        return {
            "status": "warning",
            "detail": "BigQuery billing project equals data project",
            "hint": (
                "Set data_source.bigquery.billing_project in instance.yaml to a "
                "project the SA can bill against (typically your dev/billable "
                "project, distinct from a shared read-only data project). "
                "Otherwise BQ calls 403 USER_PROJECT_DENIED whenever the SA "
                "lacks serviceusage.services.use on the data project. "
                "Configurable via /admin/server-config UI."
            ),
            "billing_project": billing,
            "data_project": data,
        }

    return {
        "status": "ok",
        "billing_project": billing,
        "data_project": data,
    }


def _check_session_pipeline(conn: duckdb.DuckDBPyConnection) -> dict:
    """Detect a stuck session pipeline: jsonls land but never get processed.

    Heuristic (#176):
      max(mtime of /data/user_sessions/**/*.jsonl) <=
      max(processed_at in session_extraction_state) + grace_seconds

    grace_seconds = 2 × the verification-detector cadence (default 15m → 30m).
    Operators with a custom SCHEDULER_VERIFICATION_DETECTOR_INTERVAL can
    extend the grace by setting that env var.

    Returns ``warning`` (never ``error``) — the LLM may be down for
    maintenance, not a hard failure. Returns ``ok`` when no session
    files exist (cold-start case).
    """
    # Resolve user_sessions dir from the same DATA_DIR conftest sets up.
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    user_sessions = data_dir / "user_sessions"

    try:
        session_files = list(user_sessions.glob("**/*.jsonl"))
    except OSError:
        # Permission / FS error — surface as 'unknown' rather than ok/warning.
        return {"status": "unknown", "detail": "could not scan user_sessions"}

    if not session_files:
        return {"status": "ok", "detail": "no session files yet"}

    try:
        latest_session_mtime = max(f.stat().st_mtime for f in session_files)
    except OSError:
        return {"status": "unknown", "detail": "could not stat session files"}

    # Look up the most recent processed_at.
    try:
        row = conn.execute(
            "SELECT MAX(processed_at) FROM session_extraction_state"
        ).fetchone()
    except Exception as e:
        return {"status": "unknown", "detail": f"could not query session_extraction_state: {e}"}

    last_processed = row[0] if row else None

    grace_seconds = _verification_detector_grace_seconds()

    if last_processed is None:
        # Files exist but state table is empty — pipeline never ran here.
        if (datetime.now(timezone.utc).timestamp() - latest_session_mtime) > grace_seconds:
            return {
                "status": "warning",
                "detail": (
                    "session_extraction_state is empty but jsonl files exist. "
                    "Check the verification-detector scheduler job."
                ),
                "session_files": len(session_files),
            }
        return {"status": "ok", "session_files": len(session_files)}

    # Both available — compare. session_extraction_state.processed_at is
    # stored as DuckDB TIMESTAMP (naive). DuckDB converts tz-aware writes
    # to local time before storing, so the only safe interpretation is
    # local-naive on read. Compute the lag against `datetime.now()` (also
    # local-naive) and only convert to epoch via the OS's local timezone
    # mapping at the comparison boundary.
    now_local_naive = datetime.now()
    if hasattr(last_processed, "tzinfo") and last_processed.tzinfo is not None:
        last_processed = last_processed.replace(tzinfo=None)
    proc_age_seconds = (now_local_naive - last_processed).total_seconds()
    file_age_seconds = time_now() - latest_session_mtime

    # File is newer than the last processed_at by more than grace_seconds.
    if proc_age_seconds - file_age_seconds > grace_seconds:
        lag_seconds = int(proc_age_seconds - file_age_seconds)
        return {
            "status": "warning",
            "detail": (
                f"session jsonls newer than session_extraction_state by ~{lag_seconds}s "
                f"(grace={grace_seconds}s). Check the verification-detector scheduler "
                f"job — uploads are not being processed."
            ),
            "lag_seconds": lag_seconds,
            "session_files": len(session_files),
        }

    return {"status": "ok", "session_files": len(session_files)}


def time_now() -> float:
    """Wall-clock seconds since epoch — separated out for test seam parity."""
    import time as _t
    return _t.time()


def _verification_detector_grace_seconds() -> int:
    """Compute the staleness grace window for the session pipeline check."""
    cadence_seconds_default = 15 * 60
    raw = os.environ.get("SCHEDULER_VERIFICATION_DETECTOR_INTERVAL")
    if raw:
        try:
            cadence_seconds = int(raw)
            if cadence_seconds > 0:
                return 2 * cadence_seconds
        except ValueError:
            pass
    return 2 * cadence_seconds_default


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

    # BigQuery billing-project sanity check (USER_PROJECT_DENIED footgun).
    bq_cfg = _check_bq_billing_project()
    if bq_cfg is not None:
        checks["bq_config"] = bq_cfg

    # Session pipeline (#176): warn when uploaded jsonls aren't getting
    # processed by the verification-detector cadence.
    try:
        checks["session_pipeline"] = _check_session_pipeline(conn)
    except Exception as e:
        checks["session_pipeline"] = {"status": "unknown", "detail": str(e)}

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
