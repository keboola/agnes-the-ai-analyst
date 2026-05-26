"""Self-scoped Stats endpoints for /me/stats — the analyst's own
analytics dashboard.

Four tabs, four endpoints, all gated by ``get_current_user`` so a
caller can only see their own data:

- ``GET /api/me/stats/sessions?limit=&offset=`` — paginated session
  list joined from ``usage_session_summary`` (post-processor) with a
  filesystem scan of un-processed JSONL (matches the admin
  ``list_user_sessions`` shape).
- ``GET /api/me/stats/tokens?days=30`` — daily token series + by-model
  breakdown + top-10 biggest sessions. Powers the Tokens tab chart.
- ``GET /api/me/stats/queries?cursor_ts=&cursor_id=&limit=`` —
  ``audit_log`` rows where ``action LIKE 'query.%'`` (BQ + local
  DuckDB queries) for this user. Cursor-paginated (keyset on
  ``(timestamp, id)``).
- ``GET /api/me/stats/sync?cursor_ts=&cursor_id=&limit=`` —
  ``audit_log`` rows where action is ``sync.*`` or ``manifest.*``,
  plus the user's ``last_pull_at`` for prominent header rendering.

Username derivation: ``_username_for_stats(user)`` reuses the
email-local-part rule from ``app.api.me`` so the joins on
``usage_*`` rows (filesystem-derived OS username) align with what
the session collector writes.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import _get_db, get_current_user

from src.repositories import (
    audit_repo,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/stats", tags=["me"])


def _username_for_stats(user: dict) -> str:
    """Return the key that ``usage_session_summary.username`` holds for
    sessions uploaded by *user*.

    Production convention: ``app/api/upload.py`` writes JSONLs under
    ``${DATA_DIR}/user_sessions/<user_id>/``; the session-pipeline
    runner uses the directory name as the ``username`` column when
    extracting summaries. ``/profile/sessions`` (now redirected to
    /me/activity) reads the same dir keyed by ``user_id``. The column
    is historically named ``username`` but its current contents are
    user_ids — return the matching lookup key.

    v45 schema added a separate ``user_id`` column for RBAC purposes
    (#293); reading from the legacy ``username`` column still works
    because it carries the user_id value as the runner-written key.
    """
    return user["id"]


def _session_data_dir() -> Path:
    """Match ``app.api.admin_user_sessions._session_data_dir``."""
    return Path(
        os.environ.get("SESSION_DATA_DIR")
        or os.environ.get("AGNES_SESSION_DATA_DIR")
        or "/data/sessions"
    )


# ---------------------------------------------------------------------------
# Sessions tab
# ---------------------------------------------------------------------------


def _uploaded_sessions_dir(user_id: str) -> Path:
    """``${DATA_DIR}/user_sessions/<user_id>`` — where ``agnes push``
    deposits JSONL files.  Mirrors ``app.web.router.profile_sessions_page``.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return data_dir / "user_sessions" / user_id


@router.get("/sessions")
def list_self_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Paginated session list for the calling user.

    Joins ``usage_session_summary`` (processed=true) with a filesystem
    scan of un-processed JSONL so a session appears immediately even
    before the UsageProcessor runs.  Additionally enriches each row
    with verification-pipeline status (from ``session_processor_state``)
    and a ``download_url`` when the uploaded JSONL exists in
    ``${DATA_DIR}/user_sessions/<user_id>/``.
    """
    username = _username_for_stats(user)
    user_id: str = user["id"]
    user_dir = _session_data_dir() / username

    try:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        with get_engine().connect() as eng_conn:
            rows_db = eng_conn.execute(
                sa.text(
                    """SELECT session_file, session_id, started_at, ended_at,
                              active_seconds, wall_seconds,
                              user_messages, tool_calls, tool_errors,
                              input_tokens, output_tokens,
                              cache_read_tokens, cache_creation_tokens,
                              primary_model
                       FROM usage_session_summary
                       WHERE username = :uname
                       ORDER BY started_at DESC NULLS LAST"""
                ),
                {"uname": username},
            ).fetchall()
    except Exception:
        rows_db = []

    cols = [
        "session_file", "session_id", "started_at", "ended_at",
        "active_seconds", "wall_seconds",
        "user_messages", "tool_calls", "tool_errors",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "primary_model",
    ]
    processed: dict[str, dict] = {}
    for r in rows_db:
        d = dict(zip(cols, r))
        for k in ("started_at", "ended_at"):
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        d["tokens_total"] = (
            int(d.get("input_tokens") or 0)
            + int(d.get("output_tokens") or 0)
            + int(d.get("cache_read_tokens") or 0)
            + int(d.get("cache_creation_tokens") or 0)
        )
        d["processed"] = True
        # Dedup key: BASENAME of ``session_file``. The session-pipeline
        # runner writes ``session_file = f"{username}/{filename}"`` while
        # the filesystem scan below walks bare filenames. Keying by
        # basename makes both views agree without normalizing the stored
        # column.
        processed[Path(d["session_file"]).name] = d

    all_rows: list[dict] = list(processed.values())
    if user_dir.is_dir():
        for p in sorted(
            user_dir.glob("*.jsonl"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            if p.name in processed:
                continue
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
            all_rows.append({
                "session_file": p.name,
                "session_id": p.stem,
                "started_at": mtime,
                "ended_at": None,
                "active_seconds": 0,
                "wall_seconds": 0,
                "user_messages": 0,
                "tool_calls": 0,
                "tool_errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "tokens_total": 0,
                "primary_model": None,
                "processed": False,
            })

    # --- Enrich with verification-pipeline status ---
    _enrich_pipeline_status(all_rows, user_id, conn)

    # --- Enrich with download URLs for uploaded JSONL ---
    uploaded_dir = _uploaded_sessions_dir(user_id)
    uploaded_names: set[str] = set()
    if uploaded_dir.is_dir():
        uploaded_names = {p.name for p in uploaded_dir.glob("*.jsonl")}
    for row in all_rows:
        fname = row.get("session_file", "")
        if fname in uploaded_names:
            row["download_url"] = f"/profile/sessions/{fname}"
        else:
            row["download_url"] = None

    all_rows.sort(
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )
    total = len(all_rows)
    page = all_rows[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": page,
    }


def _enrich_pipeline_status(
    rows: list[dict],
    user_id: str,
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Add ``pipeline_status`` and ``items_extracted`` from
    ``session_processor_state`` (verification processor) to each row
    in-place.  Matches are looked up by ``<user_id>/<session_file>``.
    """
    if not rows:
        return
    keys = [f"{user_id}/{r['session_file']}" for r in rows]
    state_map: dict[str, dict] = {}
    try:
        import sqlalchemy as sa
        from src.db_pg import get_engine
        key_binds: list[str] = []
        params: dict = {}
        for i, k in enumerate(keys):
            kn = f"k_{i}"
            key_binds.append(f":{kn}")
            params[kn] = k
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(
                sa.text(
                    f"""SELECT session_file, processed_at, items_extracted
                        FROM session_processor_state
                        WHERE processor_name = 'verification'
                          AND session_file IN ({','.join(key_binds)})"""
                ),
                params,
            )
            state_cols = list(result.keys())
            db_rows = result.fetchall()
        for row in db_rows:
            d = dict(zip(state_cols, row))
            state_map[d["session_file"]] = d
    except Exception:
        pass
    for row in rows:
        key = f"{user_id}/{row['session_file']}"
        state = state_map.get(key)
        if state is None:
            row["pipeline_status"] = "pending"
            row["items_extracted"] = None
        else:
            items = state.get("items_extracted")
            row["items_extracted"] = items
            row["pipeline_status"] = (
                "extracted" if items and items > 0 else "processed"
            )


# ---------------------------------------------------------------------------
# Tokens tab
# ---------------------------------------------------------------------------


@router.get("/tokens")
def get_tokens(
    days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Token breakdown for the Tokens tab.

    Returns a daily series (last *days* days), by-model breakdown
    (lifetime), top-10 biggest sessions (by total tokens, lifetime),
    and the lifetime grand total. Single round-trip via three
    sub-queries — each scans the same per-user partition of
    ``usage_session_summary`` which the
    ``idx_usage_session_user`` index supports.
    """
    username = _username_for_stats(user)

    import sqlalchemy as sa
    from src.db_pg import get_engine
    eng = get_engine()
    days_int = int(days)
    with eng.connect() as eng_conn:
        daily = eng_conn.execute(
            sa.text(
                f"""SELECT
                        CAST(started_at AS DATE) AS day,
                        COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                        COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                        COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                        COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                        COUNT(*) AS sessions
                    FROM usage_session_summary
                    WHERE username = :uname
                      AND started_at >= current_timestamp - INTERVAL '{days_int} days'
                    GROUP BY 1
                    ORDER BY 1"""
            ),
            {"uname": username},
        ).fetchall()
    daily_series = [
        {
            "day": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "input": int(i or 0),
            "output": int(o or 0),
            "cache_read": int(cr or 0),
            "cache_creation": int(cc or 0),
            "sessions": int(s or 0),
            "total": int((i or 0) + (o or 0) + (cr or 0) + (cc or 0)),
        }
        for (d, i, o, cr, cc, s) in daily
    ]

    with eng.connect() as eng_conn:
        by_model = eng_conn.execute(
            sa.text(
                """SELECT
                       COALESCE(primary_model, '(unknown)') AS model,
                       COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                       COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation,
                       COUNT(*) AS sessions
                   FROM usage_session_summary
                   WHERE username = :uname
                   GROUP BY 1
                   ORDER BY (
                       COALESCE(SUM(input_tokens), 0)
                       + COALESCE(SUM(output_tokens), 0)
                       + COALESCE(SUM(cache_read_tokens), 0)
                       + COALESCE(SUM(cache_creation_tokens), 0)
                   ) DESC"""
            ),
            {"uname": username},
        ).fetchall()
    model_breakdown = [
        {
            "model": m, "input": int(i or 0), "output": int(o or 0),
            "cache_read": int(cr or 0), "cache_creation": int(cc or 0),
            "sessions": int(s or 0),
            "total": int((i or 0) + (o or 0) + (cr or 0) + (cc or 0)),
        }
        for (m, i, o, cr, cc, s) in by_model
    ]

    with eng.connect() as eng_conn:
        top_sessions = eng_conn.execute(
            sa.text(
                """SELECT
                       session_file, session_id, started_at, primary_model,
                       input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens,
                       (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                        + COALESCE(cache_read_tokens, 0)
                        + COALESCE(cache_creation_tokens, 0)) AS tokens_total
                   FROM usage_session_summary
                   WHERE username = :uname
                   ORDER BY tokens_total DESC
                   LIMIT 10"""
            ),
            {"uname": username},
        ).fetchall()
    top = [
        {
            "session_file": sf,
            "session_id": sid,
            "started_at": st.isoformat() if hasattr(st, "isoformat") else st,
            "primary_model": pm,
            "input": int(i or 0), "output": int(o or 0),
            "cache_read": int(cr or 0), "cache_creation": int(cc or 0),
            "total": int(tt or 0),
        }
        for (sf, sid, st, pm, i, o, cr, cc, tt) in top_sessions
    ]

    with eng.connect() as eng_conn:
        totals_row = eng_conn.execute(
            sa.text(
                """SELECT
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM(cache_creation_tokens), 0),
                       COUNT(*)
                   FROM usage_session_summary
                   WHERE username = :uname"""
            ),
            {"uname": username},
        ).first()
    ti, to, tcr, tcc, tses = totals_row or (0, 0, 0, 0, 0)
    totals = {
        "input": int(ti or 0),
        "output": int(to or 0),
        "cache_read": int(tcr or 0),
        "cache_creation": int(tcc or 0),
        "total": int((ti or 0) + (to or 0) + (tcr or 0) + (tcc or 0)),
        "sessions": int(tses or 0),
    }

    return {
        "days": days,
        "daily": daily_series,
        "by_model": model_breakdown,
        "top_sessions": top,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# Data access tab (BQ + DuckDB queries)
# ---------------------------------------------------------------------------


@router.get("/queries")
def list_self_queries(
    limit: int = Query(50, ge=1, le=200),
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Audit-log rows where ``action LIKE 'query.%'`` for the caller.

    Covers query.local (DuckDB on parquet), query.hybrid (BQ + local
    join), query.remote (BQ direct), and query.internal (admin
    internal queries that get attributed to the actor). Cursor
    pagination on (timestamp, id) so streams under concurrent
    writes don't double-render rows.
    """
    cursor = (cursor_ts, cursor_id) if cursor_ts and cursor_id else None
    rows, next_cursor = audit_repo().query(
        user_id=user["id"],
        action_prefix="query.",
        cursor=cursor,
        limit=limit,
    )
    return _audit_response(rows, next_cursor, limit)


# ---------------------------------------------------------------------------
# Sync activity tab
# ---------------------------------------------------------------------------


@router.get("/sync")
def list_self_sync_activity(
    limit: int = Query(50, ge=1, le=200),
    cursor_ts: Optional[datetime] = None,
    cursor_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Audit-log rows where action is ``sync.*`` or ``manifest.*``
    for the caller, plus ``users.last_pull_at`` for the header card.

    Two action prefixes are merged with a UNION-ish IN filter via
    ``AuditRepository.query(action_in=[...])`` — but the repo helper
    doesn't take both prefix and IN, so we call twice and merge.
    Cheaper alternative is two SELECTs in the repo; for now we fetch
    two pages and interleave because cursor merging across two
    independent streams is fiddly without a unified ORDER. To keep
    the code obvious, we use ``query_actions(...)`` and accept that
    the cursor is single-stream (start over to page back; first page
    is what matters for the dashboard).
    """
    actions_seen = audit_repo().query_actions(
        actions=_sync_action_list(),
        limit=limit,
    )
    # Filter to this user — query_actions doesn't take user_id.
    user_rows = [r for r in actions_seen if r.get("user_id") == user["id"]]

    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        last_pull_row = eng_conn.execute(
            sa.text("SELECT last_pull_at FROM users WHERE id = :uid"),
            {"uid": user["id"]},
        ).first()
    last_pull_at = last_pull_row[0] if last_pull_row else None

    return {
        "last_pull_at": last_pull_at.isoformat()
        if last_pull_at and hasattr(last_pull_at, "isoformat")
        else last_pull_at,
        "rows": [_audit_row_to_payload(r) for r in user_rows[:limit]],
        # No next_cursor in this branch — fetched a single newest-window
        # page. Pagination beyond the first page is rarely needed for
        # personal sync history; the timeline tab in /admin/activity is
        # the place for deeper dives.
        "next_cursor": None,
    }


def _sync_action_list() -> list[str]:
    """The set of audit actions that surface on the Sync activity tab.

    Concrete known actions today:
    - ``sync.trigger`` — admin manually kicks a sync.
    - ``manifest.fetch`` — added in this PR; bumped on every
      ``GET /api/sync/manifest``.

    Listed explicitly (vs. a prefix LIKE) so accidental future
    ``sync.*`` actions (e.g. an admin-only ``sync.config_change``)
    don't leak into the analyst-facing view without review.
    """
    return ["sync.trigger", "manifest.fetch"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _audit_row_to_payload(row: dict) -> dict:
    """Convert a raw audit_log row dict to the JSON shape the Stats
    tabs render. Drops `correlation_id` (not useful per-user) and
    iso-stringifies the timestamp."""
    ts = row.get("timestamp")
    return {
        "id": row.get("id"),
        "timestamp": ts.isoformat() if ts and hasattr(ts, "isoformat") else ts,
        "action": row.get("action"),
        "resource": row.get("resource"),
        "result": row.get("result"),
        "duration_ms": row.get("duration_ms"),
        "params": row.get("params"),
        "client_kind": row.get("client_kind"),
    }


def _audit_response(rows: list[dict], next_cursor, limit: int) -> dict:
    """Shared shape for the queries and (alternate path) sync endpoints."""
    if next_cursor is not None:
        ts, cid = next_cursor
        nc = {
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "id": cid,
        }
    else:
        nc = None
    return {
        "limit": limit,
        "rows": [_audit_row_to_payload(r) for r in rows],
        "next_cursor": nc,
    }
