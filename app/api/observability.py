"""Observability page support endpoints.

Powers the unified /admin/activity page. The audit-log timeline itself is
served by app/api/activity.py — these endpoints add the bits that page
needs that didn't exist yet:

    GET    /api/admin/observability/facets   distinct (user, action, result, source) for filter dropdowns
    GET    /api/admin/observability/kpis     headline numbers for the top-bar cards
    GET    /api/admin/observability/views    list this user's saved views
    POST   /api/admin/observability/views    save / overwrite a view
    DELETE /api/admin/observability/views/{id}  delete one
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.observability_views import ObservabilityViewsRepository

router = APIRouter(prefix="/api/admin/observability", tags=["observability"])


# ---------------------------------------------------------------------------
# Facets — distinct values for the filter dropdowns, scoped to the window
# ---------------------------------------------------------------------------

# Source classification mirrors the rule on /admin/scheduler-runs:
# a row is `scheduler` when client_kind = 'scheduler' OR when its action
# matches one of these hardcoded names (back-compat with pre-v41 audit rows
# that didn't carry client_kind). 'cli' / 'web' come straight from
# client_kind. Anything else is bucketed as 'other' so the dropdown is
# closed-set.
_SCHEDULER_ACTION_FALLBACK = (
    "run_session_collector",
    "run_verification_detector",
    "run_corporate_memory",
    "marketplace.sync_all",
)


def _window_since(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


@router.get("/facets")
def facets(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the distinct facet values present in `audit_log` for the
    selected window, each with a count. The UI uses these to populate the
    filter dropdowns — so an admin sees only users/actions that actually
    exist, not a free-text guess.

    Counts are capped at 50 per facet (largest first). 50 is comfortable in
    a dropdown; tighter windows usually have <20 anyway.
    """
    since = _window_since(since_minutes)

    # users (joined to users.email so the UI shows a readable label)
    users = conn.execute(
        """
        SELECT a.user_id AS id, COALESCE(u.email, a.user_id) AS label, COUNT(*) AS n
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.timestamp >= ? AND a.user_id IS NOT NULL
        GROUP BY a.user_id, u.email
        ORDER BY n DESC
        LIMIT 50
        """,
        [since],
    ).fetchall()

    actions = conn.execute(
        """
        SELECT action AS label, COUNT(*) AS n
        FROM audit_log WHERE timestamp >= ? AND action IS NOT NULL
        GROUP BY action ORDER BY n DESC LIMIT 50
        """,
        [since],
    ).fetchall()

    results = conn.execute(
        """
        SELECT COALESCE(result, '—') AS label, COUNT(*) AS n
        FROM audit_log WHERE timestamp >= ?
        GROUP BY result ORDER BY n DESC LIMIT 50
        """,
        [since],
    ).fetchall()

    resources = conn.execute(
        """
        SELECT resource AS label, COUNT(*) AS n
        FROM audit_log WHERE timestamp >= ? AND resource IS NOT NULL
        GROUP BY resource ORDER BY n DESC LIMIT 50
        """,
        [since],
    ).fetchall()

    # Sources — derive client_kind union with the legacy action whitelist.
    sched_in = ",".join("?" for _ in _SCHEDULER_ACTION_FALLBACK)
    source_rows = conn.execute(
        f"""
        SELECT
          CASE
            WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind
            WHEN action IN ({sched_in}) THEN 'scheduler'
            WHEN user_id IS NULL THEN 'system'
            ELSE 'other'
          END AS src,
          COUNT(*) AS n
        FROM audit_log WHERE timestamp >= ?
        GROUP BY src ORDER BY n DESC
        """,
        list(_SCHEDULER_ACTION_FALLBACK) + [since],
    ).fetchall()

    return {
        "window_minutes": since_minutes,
        "users":     [{"id": r[0], "label": r[1], "count": r[2]} for r in users],
        "actions":   [{"value": r[0], "count": r[1]} for r in actions],
        "results":   [{"value": r[0], "count": r[1]} for r in results],
        "resources": [{"value": r[0], "count": r[1]} for r in resources],
        "sources":   [{"value": r[0], "count": r[1]} for r in source_rows],
    }


# ---------------------------------------------------------------------------
# KPIs — headline numbers for the top-bar stats cards
# ---------------------------------------------------------------------------

@router.get("/kpis")
def kpis(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Four KPIs for the top-bar cards: events, active users, error rate, p95."""
    since = _window_since(since_minutes)

    total = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE timestamp >= ?", [since],
    ).fetchone()[0]
    active_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM audit_log "
        "WHERE timestamp >= ? AND user_id IS NOT NULL",
        [since],
    ).fetchone()[0]
    errors = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE timestamp >= ? AND result IS NOT NULL AND result LIKE 'error%'",
        [since],
    ).fetchone()[0]
    # Latency p95 over rows that recorded duration_ms.
    p95 = conn.execute(
        "SELECT CAST(approx_quantile(duration_ms, 0.95) AS INTEGER) "
        "FROM audit_log WHERE timestamp >= ? AND duration_ms IS NOT NULL",
        [since],
    ).fetchone()[0]

    rate = (errors / total) if total else 0.0
    return {
        "window_minutes": since_minutes,
        "events_total": int(total or 0),
        "active_users": int(active_users or 0),
        "errors": int(errors or 0),
        "error_rate": round(rate, 4),
        "p95_duration_ms": int(p95) if p95 is not None else None,
    }


# ---------------------------------------------------------------------------
# Saved views — per-user CRUD
# ---------------------------------------------------------------------------

@router.get("/views")
def list_views(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    return {"views": ObservabilityViewsRepository(conn).list_for_user(user_id)}


@router.post("/views")
def save_view(
    payload: dict[str, Any] = Body(...),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    name = (payload.get("name") or "").strip()
    query = payload.get("query")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not isinstance(query, dict):
        raise HTTPException(status_code=400, detail="query must be an object")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="name too long (max 80 chars)")
    # Cap the saved-view payload so an admin can't bloat system.duckdb
    # with a malformed save. 64 KiB is generous for the saved-view shape
    # (window + a handful of short filter values + sort).
    import json as _json
    if len(_json.dumps(query)) > 64 * 1024:
        raise HTTPException(
            status_code=400,
            detail="query payload too large (max 64 KiB)",
        )
    return ObservabilityViewsRepository(conn).create(user_id, name, query)


@router.delete("/views/{view_id}")
def delete_view(
    view_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    ok = ObservabilityViewsRepository(conn).delete(user_id, view_id)
    if not ok:
        raise HTTPException(status_code=404, detail="view not found")
    return {"deleted": view_id}
