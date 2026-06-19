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

from src.repositories import (
    audit_repo,
    observability_views_repo,
    users_repo,
)
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

    data = audit_repo().facets(
        since=since,
        scheduler_actions=list(_SCHEDULER_ACTION_FALLBACK),
    )

    # The facets 'users' bucket carries ids + counts only; resolve readable
    # labels here, reproducing the old COALESCE(email, user_id).
    emails = users_repo().get_by_ids([u["id"] for u in data["users"]])

    return {
        "window_minutes": since_minutes,
        "users":     [
            {"id": u["id"], "label": emails.get(u["id"]) or u["id"], "count": u["count"]}
            for u in data["users"]
        ],
        "actions":   data["actions"],
        "results":   data["results"],
        "resources": data["resources"],
        "sources":   data["sources"],
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

    k = audit_repo().kpis(since=since)
    total = k["events_total"]
    errors = k["errors"]
    p95 = k["p95"]

    rate = (errors / total) if total else 0.0
    return {
        "window_minutes": since_minutes,
        "events_total": int(total or 0),
        "active_users": int(k["active_users"] or 0),
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
    return {"views": observability_views_repo().list_for_user(user_id)}


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
    # Per-user view-count cap — admin is the only role here, but a
    # runaway script shouldn't be able to fill system.duckdb with
    # thousands of views. 100 is well above any plausible curation
    # ceiling; ON CONFLICT updates an existing name rather than
    # adding rows, so this only bites genuine fan-out.
    # Routed through the factory so the cap reads the active backend (PG /
    # DuckDB) — a raw conn.execute here reads the frozen DuckDB file on a
    # Postgres instance, so the count would always be 0 and never cap.
    views_repo = observability_views_repo()
    existing = views_repo.count_for_user(user_id)
    already_exists = views_repo.name_exists(user_id, name)
    if existing >= 100 and not already_exists:
        raise HTTPException(
            status_code=400,
            detail="saved-view count for this user has reached 100; delete one before adding another",
        )
    return views_repo.create(user_id, name, query)


@router.delete("/views/{view_id}", status_code=204)
def delete_view(
    view_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    ok = observability_views_repo().delete(user_id, view_id)
    if not ok:
        raise HTTPException(status_code=404, detail="view not found")
