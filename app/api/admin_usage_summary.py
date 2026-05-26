"""GET /api/admin/usage/summary?window=7d|30d|all — aggregated telemetry overview.

Drives the legacy /admin/usage HTML page. All admin-only, audit-logged.

Newer endpoints — /facets, /query, /kpis — back the interactive Usage page
modeled after /admin/activity (filter dropdowns + group-by + searchable
table). They live in the same module so all telemetry HTTP entry points
share the audit-suppression cache and require_admin gate.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.api.activity import _should_audit

from src.repositories import (
    audit_repo,
)
router = APIRouter(prefix="/api/admin/telemetry", tags=["admin-telemetry"])
logger = logging.getLogger(__name__)

_GROUP_BY_COLUMNS = {
    "day":       ("CAST(occurred_at AS DATE)", "day"),
    "username":  ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source":    ("source", "source"),
    "ref_id":    ("ref_id", "ref_id"),
}


def _percentile(values: list[float], p: float) -> float:
    """Pure-Python percentile (linear interpolation) — fallback when the
    aggregate-only approx_quantile isn't usable on pre-aggregated data."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    idx = p * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


@router.get("/summary")
def usage_summary(
    window: Literal["7d", "30d", "all"] = Query("7d"),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Compute six summaries:
    - top_tools: list[{tool_name, invocations, source}]
    - top_users: list[{username, tool_calls}]
    - error_rate: list[{tool_name, invocations, errors, rate}]
    - dau_series: list[{day, active_users}] — 30 entries even when window=7d (sparkline likes 30)
    - dau_avg: float
    - slow_actions: list[{action, p50, p95, p99, max_ms, n}]
    """
    now = datetime.now(timezone.utc)
    if window == "7d":
        cutoff = now - timedelta(days=7)
    elif window == "30d":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = datetime(1970, 1, 1, tzinfo=timezone.utc)

    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        top_tools = [
            {"tool_name": r[0], "source": r[1], "invocations": int(r[2])}
            for r in eng_conn.execute(
                sa.text(
                    """SELECT tool_name, source, COUNT(*) AS n
                       FROM usage_events
                       WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                       GROUP BY tool_name, source ORDER BY n DESC LIMIT 10"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
        ]

        top_users = [
            {"username": r[0], "tool_calls": int(r[1])}
            for r in eng_conn.execute(
                sa.text(
                    """SELECT username, COUNT(*) AS n FROM usage_events
                       WHERE occurred_at >= :cutoff
                       GROUP BY username ORDER BY n DESC LIMIT 10"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
        ]

        error_rows = eng_conn.execute(
            sa.text(
                """SELECT tool_name, COUNT(*) AS n,
                          SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
                   FROM usage_events
                   WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                   GROUP BY tool_name HAVING COUNT(*) > 0
                   ORDER BY n DESC LIMIT 10"""
            ),
            {"cutoff": cutoff},
        ).fetchall()
        error_rate = [
            {"tool_name": r[0], "invocations": int(r[1]), "errors": int(r[2]),
             "rate": float(r[2]) / float(r[1]) if r[1] else 0.0}
            for r in error_rows
        ]

        dau_start = (now - timedelta(days=30)).date()
        dau_rows = eng_conn.execute(
            sa.text(
                """SELECT CAST(occurred_at AS DATE) AS day,
                          COUNT(DISTINCT username) AS n
                   FROM usage_events
                   WHERE CAST(occurred_at AS DATE) >= :start
                   GROUP BY day ORDER BY day"""
            ),
            {"start": dau_start},
        ).fetchall()
        dau_dict = {r[0]: int(r[1]) for r in dau_rows}
        dau_series = []
        for i in range(30):
            d = (dau_start + timedelta(days=i))
            dau_series.append({"day": d.isoformat(), "active_users": dau_dict.get(d, 0)})
        dau_avg = sum(s["active_users"] for s in dau_series) / 30 if dau_series else 0

        # Slow actions from audit_log durations. DuckDB's approx_quantile is
        # gone with the cutover; Postgres has percentile_cont (ordered-set
        # aggregate) which is also exact, not approximate.
        try:
            slow_rows = eng_conn.execute(
                sa.text(
                    """SELECT action,
                              percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms) AS p50,
                              percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                              percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99,
                              MAX(duration_ms) AS max_ms,
                              COUNT(*) AS n
                       FROM audit_log
                       WHERE timestamp >= :cutoff
                         AND duration_ms IS NOT NULL AND duration_ms > 0
                       GROUP BY action HAVING COUNT(*) >= 5
                       ORDER BY p95 DESC LIMIT 10"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
            slow_actions = [
                {"action": r[0], "p50": int(r[1] or 0), "p95": int(r[2] or 0),
                 "p99": int(r[3] or 0), "max_ms": int(r[4] or 0), "n": int(r[5])}
                for r in slow_rows
            ]
        except Exception:
            logger.debug("percentile_cont unavailable; falling back to Python percentile")
            action_durations: dict[str, list[float]] = {}
            for row in eng_conn.execute(
                sa.text(
                    """SELECT action, duration_ms FROM audit_log
                       WHERE timestamp >= :cutoff
                         AND duration_ms IS NOT NULL AND duration_ms > 0"""
                ),
                {"cutoff": cutoff},
            ).fetchall():
                action_durations.setdefault(row[0], []).append(float(row[1]))

            slow_list = []
            for action, vals in action_durations.items():
                if len(vals) < 5:
                    continue
                slow_list.append({
                    "action": action,
                    "p50": int(_percentile(vals, 0.5)),
                    "p95": int(_percentile(vals, 0.95)),
                    "p99": int(_percentile(vals, 0.99)),
                    "max_ms": int(max(vals)),
                    "n": len(vals),
                })
            slow_actions = sorted(slow_list, key=lambda x: x["p95"], reverse=True)[:10]

    actor_id = user.get("id") or "anonymous"
    if _should_audit(actor_id, {"endpoint": "usage.summary", "window": window}):
        try:
            audit_repo().log(
                user_id=actor_id,
                action="usage.summary",
                params={"window": window},
                result="success",
                client_kind="web",
            )
        except Exception:
            logger.exception("audit_log write failed for usage.summary; continuing")

    return {
        "window": window,
        "top_tools": top_tools,
        "top_users": top_users,
        "error_rate": error_rate,
        "dau_series": dau_series,
        "dau_avg": round(dau_avg, 1),
        "slow_actions": slow_actions,
    }


# ===========================================================================
# Interactive Usage page support — /facets /kpis /query
# ===========================================================================


def _usage_window_cutoff(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


def _usage_where(
    since: datetime,
    username: Optional[str],
    tool_name: Optional[str],
    source: Optional[str],
    event_type: Optional[str],
    only_errors: bool,
    q: Optional[str],
) -> tuple[str, dict]:
    """Compose a parametrised WHERE clause shared by facets/kpis/query.

    Returns ``(sql, params_dict)`` ready to pass to ``sa.text(...)`` with
    ``conn.execute(text, params)``.
    """
    where = ["occurred_at >= :since"]
    params: dict = {"since": since}
    if username:
        where.append("username = :username"); params["username"] = username
    if tool_name:
        where.append("tool_name = :tool_name"); params["tool_name"] = tool_name
    if source:
        where.append("source = :source"); params["source"] = source
    if event_type:
        where.append("event_type = :event_type"); params["event_type"] = event_type
    if only_errors:
        where.append("is_error = TRUE")
    if q:
        from src.sql_safe import pg_like_escape
        where.append(
            "(tool_name LIKE :q ESCAPE '\\' "
            "OR skill_name LIKE :q ESCAPE '\\' "
            "OR subagent_type LIKE :q ESCAPE '\\' "
            "OR command_name LIKE :q ESCAPE '\\')"
        )
        params["q"] = f"%{pg_like_escape(q)}%"
    return " AND ".join(where), params


@router.get("/facets")
def usage_facets(
    since_minutes: int = Query(default=10080, ge=1, le=525600),  # default 7d
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Distinct values present in usage_events for the selected window so the
    UI dropdowns are closed-set instead of free-text guesses."""
    since = _usage_window_cutoff(since_minutes)

    import sqlalchemy as sa
    from src.db_pg import get_engine
    params = {"since": since}
    with get_engine().connect() as eng_conn:
        users = eng_conn.execute(
            sa.text(
                "SELECT username, COUNT(*) AS n FROM usage_events "
                "WHERE occurred_at >= :since AND username IS NOT NULL "
                "GROUP BY username ORDER BY n DESC LIMIT 50"
            ),
            params,
        ).fetchall()
        tools = eng_conn.execute(
            sa.text(
                "SELECT tool_name, COUNT(*) AS n FROM usage_events "
                "WHERE occurred_at >= :since AND tool_name IS NOT NULL "
                "GROUP BY tool_name ORDER BY n DESC LIMIT 50"
            ),
            params,
        ).fetchall()
        sources = eng_conn.execute(
            sa.text(
                "SELECT source, COUNT(*) AS n FROM usage_events "
                "WHERE occurred_at >= :since AND source IS NOT NULL "
                "GROUP BY source ORDER BY n DESC LIMIT 20"
            ),
            params,
        ).fetchall()
        event_types = eng_conn.execute(
            sa.text(
                "SELECT event_type, COUNT(*) AS n FROM usage_events "
                "WHERE occurred_at >= :since AND event_type IS NOT NULL "
                "GROUP BY event_type ORDER BY n DESC LIMIT 20"
            ),
            params,
        ).fetchall()
    return {
        "window_minutes": since_minutes,
        "users":       [{"value": r[0], "count": r[1]} for r in users],
        "tools":       [{"value": r[0], "count": r[1]} for r in tools],
        "sources":     [{"value": r[0], "count": r[1]} for r in sources],
        "event_types": [{"value": r[0], "count": r[1]} for r in event_types],
    }


@router.get("/kpis")
def usage_kpis(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    event_type: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Four headline numbers, scoped to the same filters as /query.

    The cards on /admin/usage echo these as clickable quick-filters, so the
    server applies the same WHERE the table will see — otherwise the cards
    and the table tell different stories at the same time.
    """
    since = _usage_window_cutoff(since_minutes)
    where_sql, params = _usage_where(
        since, username, tool_name, source, event_type, only_errors, q,
    )

    import sqlalchemy as sa
    from src.db_pg import get_engine
    with get_engine().connect() as eng_conn:
        row = eng_conn.execute(
            sa.text(
                f"""SELECT COUNT(*),
                          COUNT(DISTINCT username),
                          COUNT(DISTINCT tool_name),
                          SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                   FROM usage_events WHERE {where_sql}"""
            ),
            params,
        ).first()
    total, users, tools, errors = (int(x or 0) for x in row)
    error_rate = (errors / total) if total else 0.0
    return {
        "window_minutes": since_minutes,
        "events_total":   total,
        "distinct_users": users,
        "distinct_tools": tools,
        "errors":         errors,
        "error_rate":     round(error_rate, 4),
    }


@router.get("/query")
def usage_query(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    event_type: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
    group_by: Optional[str] = Query(default=None),
    sort: str = Query(default="invocations:desc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=50000),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Filtered + optionally grouped read against usage_events.

    `group_by` ∈ {None, 'day', 'username', 'tool_name', 'source', 'ref_id'}.
    When grouped, returns one bucket per row with `invocations`,
    `distinct_users`, `distinct_sessions`, `errors`. When ungrouped, returns
    the raw event rows.

    `sort` syntax: `<column>:<asc|desc>`. For grouped queries the valid
    columns are `bucket`, `invocations`, `distinct_users`, `distinct_sessions`,
    `errors`. For ungrouped queries: `occurred_at`. Unknown sort keys fall
    back to a safe default rather than 400 — UIs evolve faster than this
    endpoint.
    """
    since = _usage_window_cutoff(since_minutes)
    where_sql, params = _usage_where(
        since, username, tool_name, source, event_type, only_errors, q,
    )

    sort_col, _, sort_dir = sort.partition(":")
    sort_dir = "ASC" if (sort_dir or "desc").lower() == "asc" else "DESC"

    import sqlalchemy as sa
    from src.db_pg import get_engine

    if group_by and group_by in _GROUP_BY_COLUMNS:
        expr, alias = _GROUP_BY_COLUMNS[group_by]
        valid_sort = {
            "bucket":            f"{expr}",
            "invocations":       "COUNT(*)",
            "distinct_users":    "COUNT(DISTINCT username)",
            "distinct_sessions": "COUNT(DISTINCT session_id)",
            "errors":            "SUM(CASE WHEN is_error THEN 1 ELSE 0 END)",
        }
        order_expr = valid_sort.get(sort_col, "COUNT(*)")
        lim_params = {**params, "lim": limit, "off": offset}
        with get_engine().connect() as eng_conn:
            total_buckets = eng_conn.execute(
                sa.text(
                    f"SELECT COUNT(DISTINCT {expr}) FROM usage_events WHERE {where_sql}"
                ),
                params,
            ).scalar()
            rows = eng_conn.execute(
                sa.text(
                    f"""SELECT {expr} AS bucket,
                               COUNT(*) AS invocations,
                               COUNT(DISTINCT username) AS distinct_users,
                               COUNT(DISTINCT session_id) AS distinct_sessions,
                               SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS errors
                        FROM usage_events WHERE {where_sql}
                        GROUP BY {expr}
                        ORDER BY {order_expr} {sort_dir}
                        LIMIT :lim OFFSET :off"""
                ),
                lim_params,
            ).fetchall()
        out = [
            {
                "bucket":            (str(r[0]) if r[0] is not None else None),
                "invocations":       int(r[1] or 0),
                "distinct_users":    int(r[2] or 0),
                "distinct_sessions": int(r[3] or 0),
                "errors":            int(r[4] or 0),
            }
            for r in rows
        ]
        return {
            "group_by":     group_by,
            "group_alias":  alias,
            "rows":         out,
            "total":        int(total_buckets or 0),
            "limit":        limit,
            "offset":       offset,
            "next_offset":  offset + limit if (offset + limit) < (total_buckets or 0) else None,
        }

    # ungrouped — raw events
    valid_sort = {"occurred_at": "occurred_at", "invocations": "occurred_at"}
    order_expr = valid_sort.get(sort_col, "occurred_at")
    lim_params = {**params, "lim": limit, "off": offset}
    with get_engine().connect() as eng_conn:
        total = eng_conn.execute(
            sa.text(f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}"),
            params,
        ).scalar()
        rows = eng_conn.execute(
            sa.text(
                f"""SELECT id, occurred_at, username, source, ref_id, event_type,
                          tool_name, skill_name, subagent_type, command_name, is_error,
                          session_id, model
                   FROM usage_events WHERE {where_sql}
                   ORDER BY {order_expr} {sort_dir}
                   LIMIT :lim OFFSET :off"""
            ),
            lim_params,
        ).fetchall()
    cols = [
        "id","occurred_at","username","source","ref_id","event_type",
        "tool_name","skill_name","subagent_type","command_name","is_error",
        "session_id","model",
    ]
    out = [dict(zip(cols, r)) for r in rows]
    for r in out:
        if r.get("occurred_at"):
            r["occurred_at"] = r["occurred_at"].isoformat()
    return {
        "group_by":     None,
        "rows":         out,
        "total":        int(total or 0),
        "limit":        limit,
        "offset":       offset,
        "next_offset":  offset + limit if (offset + limit) < (total or 0) else None,
    }
