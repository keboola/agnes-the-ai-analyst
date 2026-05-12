"""GET /api/admin/usage/summary?window=7d|30d|all — aggregated telemetry overview.

Drives the /admin/usage HTML page. All admin-only, audit-logged.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/admin/usage", tags=["admin-usage"])
logger = logging.getLogger(__name__)


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

    # Top tools (from usage_events)
    top_tools = [
        {"tool_name": r[0], "source": r[1], "invocations": int(r[2])}
        for r in conn.execute(
            """SELECT tool_name, source, COUNT(*) AS n
               FROM usage_events
               WHERE occurred_at >= ? AND tool_name IS NOT NULL
               GROUP BY tool_name, source ORDER BY n DESC LIMIT 10""",
            [cutoff],
        ).fetchall()
    ]

    # Top users
    top_users = [
        {"username": r[0], "tool_calls": int(r[1])}
        for r in conn.execute(
            """SELECT username, COUNT(*) AS n FROM usage_events
               WHERE occurred_at >= ? GROUP BY username ORDER BY n DESC LIMIT 10""",
            [cutoff],
        ).fetchall()
    ]

    # Error rate (from usage_events)
    error_rows = conn.execute(
        """SELECT tool_name, COUNT(*) AS n, SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
           FROM usage_events
           WHERE occurred_at >= ? AND tool_name IS NOT NULL
           GROUP BY tool_name HAVING COUNT(*) > 0 ORDER BY n DESC LIMIT 10""",
        [cutoff],
    ).fetchall()
    error_rate = [
        {"tool_name": r[0], "invocations": int(r[1]), "errors": int(r[2]),
         "rate": float(r[2]) / float(r[1]) if r[1] else 0.0}
        for r in error_rows
    ]

    # DAU series — always 30 days for the sparkline
    dau_start = (now - timedelta(days=30)).date()
    dau_rows = conn.execute(
        """SELECT CAST(occurred_at AS DATE) AS day, COUNT(DISTINCT username) AS n
           FROM usage_events
           WHERE CAST(occurred_at AS DATE) >= ?
           GROUP BY day ORDER BY day""",
        [dau_start],
    ).fetchall()
    dau_dict = {r[0]: int(r[1]) for r in dau_rows}
    dau_series = []
    for i in range(30):
        d = (dau_start + timedelta(days=i))
        dau_series.append({"day": d.isoformat(), "active_users": dau_dict.get(d, 0)})
    dau_avg = sum(s["active_users"] for s in dau_series) / 30 if dau_series else 0

    # Slow actions from audit_log durations — use approx_quantile aggregate
    try:
        slow_rows = conn.execute(
            """SELECT action,
                      approx_quantile(duration_ms, 0.5)  AS p50,
                      approx_quantile(duration_ms, 0.95) AS p95,
                      approx_quantile(duration_ms, 0.99) AS p99,
                      MAX(duration_ms) AS max_ms,
                      COUNT(*) AS n
               FROM audit_log
               WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0
               GROUP BY action HAVING n >= 5
               ORDER BY p95 DESC LIMIT 10""",
            [cutoff],
        ).fetchall()
        slow_actions = [
            {"action": r[0], "p50": int(r[1] or 0), "p95": int(r[2] or 0),
             "p99": int(r[3] or 0), "max_ms": int(r[4] or 0), "n": int(r[5])}
            for r in slow_rows
        ]
    except Exception:
        # Pure-Python fallback for environments where approx_quantile aggregate
        # isn't available or returns unexpected types.
        logger.debug("approx_quantile unavailable; falling back to Python percentile")
        action_durations: dict[str, list[float]] = {}
        for row in conn.execute(
            """SELECT action, duration_ms FROM audit_log
               WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0""",
            [cutoff],
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

    try:
        AuditRepository(conn).log(
            user_id=user.get("id"),
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
