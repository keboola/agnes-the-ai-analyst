"""GET /api/admin/reports/marketplace-digest?period=daily|weekly — one
consolidated, report-shaped JSON payload for an external rendering pipeline
(e.g. an n8n workflow whose LLM node fills an HTML template from this JSON and
publishes it).

Why a dedicated endpoint rather than the existing /api/admin/telemetry/* ones:
those expose tool/user/error/DAU rollups only. A marketplace digest also needs
per-item usage (curated/flea/builtin), installs/adoption, sync health, and
"what's not landing" — composed here from the same DuckDB tables the telemetry
endpoints already read, so downstream processing stays near zero.

Admin-only (PAT-gated for headless callers), audit-logged with the same
burst-suppression cache as the other admin telemetry entry points.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timezone, timedelta
from typing import Literal, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.api.activity import _should_audit
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/admin/reports", tags=["admin-reports"])
logger = logging.getLogger(__name__)

_STALE_SYNC_HOURS = 48
_TOP_N = 10
_MOVERS_N = 5
_ZERO_USAGE_N = 25


def _midnight(d: date) -> datetime:
    """UTC midnight at the start of `d` — usage_events.occurred_at is a
    timezone-aware TIMESTAMP, so we compare against aware datetimes (matching
    the cutoff style in admin_usage_summary.py)."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _delta_pct(value: float, prev: float) -> Optional[float]:
    """Percentage change vs the comparison period. None when there's no base
    (prev == 0) so the renderer can show "new" instead of a divide-by-zero
    spike."""
    if not prev:
        return None
    return round((value - prev) / prev * 100.0, 1)


def _kpi(value: float, prev: float) -> dict:
    return {"value": value, "prev": prev, "delta_pct": _delta_pct(value, prev)}


@router.get("/marketplace-digest")
def marketplace_digest(
    period: Literal["daily", "weekly"] = Query("daily"),
    date_str: Optional[str] = Query(
        None, alias="date",
        description="Anchor day (YYYY-MM-DD, UTC) = most recent complete day to "
                    "report. Defaults to yesterday.",
    ),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Compose the full digest payload for `period`.

    Windows (all UTC, upper bound exclusive):
    - daily:  primary = the anchor day; comparison = the day before;
              trend_series = trailing 14 days ending on the anchor.
    - weekly: primary = 7 days ending on the anchor; comparison = the 7 days
              before that; trend_series = trailing 30 days ending on the anchor.
    """
    # ---- resolve the anchor + the primary / comparison / trend ranges --------
    if date_str:
        try:
            anchor = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    else:
        anchor = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    if period == "daily":
        p_start, p_end = anchor, anchor + timedelta(days=1)
        c_start, c_end = anchor - timedelta(days=1), anchor
        trend_len = 14
    else:  # weekly
        p_start, p_end = anchor - timedelta(days=6), anchor + timedelta(days=1)
        c_start, c_end = anchor - timedelta(days=13), anchor - timedelta(days=6)
        trend_len = 30

    p_start_ts, p_end_ts = _midnight(p_start), _midnight(p_end)
    c_start_ts, c_end_ts = _midnight(c_start), _midnight(c_end)
    trend_start = anchor - timedelta(days=trend_len - 1)
    trend_start_ts = _midnight(trend_start)

    # ---- headline KPIs (usage_events / usage_session_summary / installs) ------
    def _event_kpis(start_ts, end_ts):
        row = conn.execute(
            """SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ?""",
            [start_ts, end_ts],
        ).fetchone()
        inv, users, errs = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        return inv, users, errs

    def _session_count(start_ts, end_ts):
        return int(conn.execute(
            """SELECT COUNT(DISTINCT session_id) FROM usage_session_summary
               WHERE started_at >= ? AND started_at < ?""",
            [start_ts, end_ts],
        ).fetchone()[0] or 0)

    def _install_count(start_ts, end_ts):
        curated = int(conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE opted_out_at >= ? AND opted_out_at < ?",
            [start_ts, end_ts],
        ).fetchone()[0] or 0)
        flea = int(conn.execute(
            "SELECT COUNT(*) FROM user_store_installs WHERE installed_at >= ? AND installed_at < ?",
            [start_ts, end_ts],
        ).fetchone()[0] or 0)
        return curated + flea

    p_inv, p_users, p_errs = _event_kpis(p_start_ts, p_end_ts)
    c_inv, c_users, c_errs = _event_kpis(c_start_ts, c_end_ts)
    p_sessions, c_sessions = _session_count(p_start_ts, p_end_ts), _session_count(c_start_ts, c_end_ts)
    p_installs, c_installs = _install_count(p_start_ts, p_end_ts), _install_count(c_start_ts, c_end_ts)
    p_rate = round(p_errs / p_inv, 4) if p_inv else 0.0
    c_rate = round(c_errs / c_inv, 4) if c_inv else 0.0

    headline_kpis = {
        "active_users": _kpi(p_users, c_users),
        "sessions":     _kpi(p_sessions, c_sessions),
        "invocations":  _kpi(p_inv, c_inv),
        "errors":       _kpi(p_errs, c_errs),
        "error_rate":   _kpi(p_rate, c_rate),
        "new_installs": _kpi(p_installs, c_installs),
    }

    # ---- trend_series (per-day, for sparklines / charts) ---------------------
    ev_rows = conn.execute(
        """SELECT CAST(occurred_at AS DATE) AS d,
                  COUNT(*),
                  COUNT(DISTINCT username),
                  SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
           FROM usage_events
           WHERE occurred_at >= ? AND occurred_at < ?
           GROUP BY d""",
        [trend_start_ts, p_end_ts],
    ).fetchall()
    ev_by_day = {r[0]: (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)) for r in ev_rows}

    inst_by_day: dict[date, int] = {}
    for sql, col in (
        ("SELECT CAST(opted_out_at AS DATE) d, COUNT(*) FROM user_plugin_optouts "
         "WHERE opted_out_at >= ? AND opted_out_at < ? GROUP BY d", None),
        ("SELECT CAST(installed_at AS DATE) d, COUNT(*) FROM user_store_installs "
         "WHERE installed_at >= ? AND installed_at < ? GROUP BY d", None),
    ):
        for r in conn.execute(sql, [trend_start_ts, p_end_ts]).fetchall():
            inst_by_day[r[0]] = inst_by_day.get(r[0], 0) + int(r[1] or 0)

    trend_series = []
    for i in range(trend_len):
        d = trend_start + timedelta(days=i)
        inv, users, errs = ev_by_day.get(d, (0, 0, 0))
        trend_series.append({
            "day": d.isoformat(),
            "active_users": users,
            "invocations": inv,
            "errors": errs,
            "installs": inst_by_day.get(d, 0),
        })

    # ---- usage by source (primary window) ------------------------------------
    by_source = [
        {"source": r[0], "invocations": int(r[1] or 0),
         "distinct_users": int(r[2] or 0), "error_count": int(r[3] or 0)}
        for r in conn.execute(
            """SELECT source, COUNT(*), COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ? AND source IS NOT NULL
               GROUP BY source ORDER BY 2 DESC""",
            [p_start_ts, p_end_ts],
        ).fetchall()
    ]

    # ---- marketplace-item aggregates (usage_marketplace_item_daily) ----------
    # Aggregate the primary and comparison windows separately, then take the
    # union of keys so an item that dropped to zero still shows up in `falling`.
    def _item_agg(start_d, end_d):
        rows = conn.execute(
            """SELECT source, type, parent_plugin, name,
                      SUM(count), SUM(distinct_users), SUM(error_count)
               FROM usage_marketplace_item_daily
               WHERE day >= ? AND day < ?
               GROUP BY source, type, parent_plugin, name""",
            [start_d, end_d],
        ).fetchall()
        return {
            (r[0], r[1], r[2], r[3]): (int(r[4] or 0), int(r[5] or 0), int(r[6] or 0))
            for r in rows
        }

    primary = _item_agg(p_start, p_end)
    compare = _item_agg(c_start, c_end)

    items = []
    for key in set(primary) | set(compare):
        source, type_, parent, name = key
        inv, du, err = primary.get(key, (0, 0, 0))
        prev_inv = compare.get(key, (0, 0, 0))[0]
        items.append({
            "source": source, "type": type_, "parent_plugin": parent, "name": name,
            "invocations": inv, "distinct_users": du, "error_count": err,
            "prev_invocations": prev_inv, "delta_pct": _delta_pct(inv, prev_inv),
        })

    def _public(it: dict) -> dict:
        return {k: it[k] for k in (
            "source", "type", "parent_plugin", "name",
            "invocations", "distinct_users", "error_count", "delta_pct",
        )}

    top_items = []
    for rank, it in enumerate(
        sorted([i for i in items if i["invocations"] > 0],
               key=lambda x: x["invocations"], reverse=True)[:_TOP_N],
        start=1,
    ):
        row = _public(it)
        row["rank"] = rank
        top_items.append(row)

    rising = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "delta_pct": it["delta_pct"]}
        for it in sorted(
            [i for i in items if i["invocations"] > i["prev_invocations"] and i["delta_pct"] is not None],
            key=lambda x: x["delta_pct"], reverse=True,
        )[:_MOVERS_N]
    ]
    falling = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "delta_pct": it["delta_pct"]}
        for it in sorted(
            [i for i in items if i["invocations"] < i["prev_invocations"] and i["delta_pct"] is not None],
            key=lambda x: x["delta_pct"],
        )[:_MOVERS_N]
    ]
    failures = [
        {"name": it["name"], "source": it["source"], "type": it["type"],
         "invocations": it["invocations"], "errors": it["error_count"],
         "error_rate": round(it["error_count"] / it["invocations"], 4) if it["invocations"] else 0.0}
        for it in sorted(
            [i for i in items if i["error_count"] > 0],
            key=lambda x: (x["error_count"], x["invocations"]), reverse=True,
        )[:_TOP_N]
    ]

    # ---- zero-usage curated plugins ("not landing") --------------------------
    # Curated catalog rows with no invocations in the primary window. Joined to
    # the registry for curator attribution.
    used_curated = {
        it["name"] for it in items
        if it["source"] == "curated" and it["invocations"] > 0
    }
    zero_usage = []
    for r in conn.execute(
        """SELECT p.marketplace_id, p.name, r.curator_name
           FROM marketplace_plugins p
           LEFT JOIN marketplace_registry r ON r.id = p.marketplace_id
           WHERE COALESCE(p.is_system, FALSE) = FALSE
           ORDER BY p.marketplace_id, p.name""",
    ).fetchall():
        if r[1] not in used_curated:
            zero_usage.append({
                "marketplace_id": r[0], "name": r[1], "curator_name": r[2],
            })
        if len(zero_usage) >= _ZERO_USAGE_N:
            break

    # ---- installs / adoption (primary window) --------------------------------
    installs_curated = [
        {"ref_id": f"{r[0]}/{r[1]}", "name": r[1], "installs": int(r[2] or 0)}
        for r in conn.execute(
            """SELECT marketplace_id, plugin_name, COUNT(*) AS n
               FROM user_plugin_optouts
               WHERE opted_out_at >= ? AND opted_out_at < ?
               GROUP BY marketplace_id, plugin_name ORDER BY n DESC LIMIT ?""",
            [p_start_ts, p_end_ts, _TOP_N],
        ).fetchall()
    ]
    installs_flea = [
        {"entity_id": r[0], "name": r[1], "installs": int(r[2] or 0)}
        for r in conn.execute(
            """SELECT usi.entity_id, s.name, COUNT(*) AS n
               FROM user_store_installs usi
               LEFT JOIN store_entities s ON s.id = usi.entity_id
               WHERE usi.installed_at >= ? AND usi.installed_at < ?
               GROUP BY usi.entity_id, s.name ORDER BY n DESC LIMIT ?""",
            [p_start_ts, p_end_ts, _TOP_N],
        ).fetchall()
    ]
    installs = {
        "curated": installs_curated,
        "flea": installs_flea,
        "total": p_installs,
    }

    # ---- marketplace health (registry + plugin counts) -----------------------
    now = datetime.now(timezone.utc)
    marketplace_health = []
    for r in conn.execute(
        """SELECT r.id, r.name, r.curator_name, r.last_synced_at, r.last_error,
                  (SELECT COUNT(*) FROM marketplace_plugins p WHERE p.marketplace_id = r.id)
           FROM marketplace_registry r
           ORDER BY r.name""",
    ).fetchall():
        last_synced = r[3]
        last_error = r[4]
        if last_error:
            status = "error"
        elif last_synced is None:
            status = "stale"
        else:
            synced = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=timezone.utc)
            status = "stale" if (now - synced) > timedelta(hours=_STALE_SYNC_HOURS) else "ok"
        marketplace_health.append({
            "id": r[0], "name": r[1], "curator_name": r[2],
            "plugin_count": int(r[5] or 0),
            "last_synced_at": last_synced.isoformat() if last_synced else None,
            "sync_status": status,
            "last_error": last_error,
        })

    # ---- audit (burst-suppressed, same cache as telemetry endpoints) ---------
    actor_id = user.get("id") or "anonymous"
    if _should_audit(actor_id, {"endpoint": "reports.marketplace_digest", "period": period}):
        try:
            AuditRepository(conn).log(
                user_id=actor_id,
                action="reports.marketplace_digest",
                params={"period": period, "anchor": anchor.isoformat()},
                result="success",
                client_kind="web",
            )
        except Exception:
            logger.exception("audit_log write failed for reports.marketplace_digest; continuing")

    return {
        "meta": {
            "report_type": period,
            "generated_at": now.isoformat(),
            "period_start": p_start.isoformat(),
            "period_end": (p_end - timedelta(days=1)).isoformat(),  # inclusive last day
            "comparison_start": c_start.isoformat(),
            "comparison_end": (c_end - timedelta(days=1)).isoformat(),
            "timezone": "UTC",
        },
        "headline_kpis": headline_kpis,
        "trend_series": trend_series,
        "by_source": by_source,
        "top_items": top_items,
        "rising": rising,
        "falling": falling,
        "failures": failures,
        "installs": installs,
        "zero_usage": zero_usage,
        "marketplace_health": marketplace_health,
    }
