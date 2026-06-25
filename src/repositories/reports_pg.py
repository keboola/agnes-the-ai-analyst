"""Postgres-backed reports repository.

Mirrors ``src/repositories/reports.py`` — same method names and return shapes,
PG dialect (``:param`` binds, ``sa.text``). The aggregate SQL itself is
backend-portable (``CAST(... AS DATE)``, ``COUNT(DISTINCT ...)``,
``SUM(CASE WHEN is_error THEN 1 ELSE 0 END)``), so the two files stay nearly
identical.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

ItemKey = Tuple[str, str, str, str]  # (source, type, parent_plugin, name)


class ReportsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # ---- usage_events / sessions windows ---------------------------------
    def event_window(self, start: datetime, end: datetime) -> dict:
        with self._engine.connect() as conn:
            r = conn.execute(
                sa.text(
                    """SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end"""
                ),
                {"start": start, "end": end},
            ).fetchone()
        return {"invocations": int(r[0] or 0),
                "active_users": int(r[1] or 0),
                "errors": int(r[2] or 0)}

    def session_count(self, start: datetime, end: datetime) -> int:
        with self._engine.connect() as conn:
            r = conn.execute(
                sa.text(
                    """SELECT COUNT(DISTINCT session_id) FROM usage_session_summary
                       WHERE started_at >= :start AND started_at < :end"""
                ),
                {"start": start, "end": end},
            ).fetchone()
        return int(r[0] or 0)

    def events_daily(self, start: datetime, end: datetime) -> Dict[date, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST(occurred_at AS DATE) AS d,
                              COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end
                       GROUP BY d"""
                ),
                {"start": start, "end": end},
            ).fetchall()
        return {
            r[0]: {"invocations": int(r[1] or 0),
                   "active_users": int(r[2] or 0),
                   "errors": int(r[3] or 0)}
            for r in rows
        }

    def by_source(self, start: datetime, end: datetime) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT source, COUNT(*), COUNT(DISTINCT username),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events
                       WHERE occurred_at >= :start AND occurred_at < :end
                         AND source IS NOT NULL
                       GROUP BY source ORDER BY 2 DESC"""
                ),
                {"start": start, "end": end},
            ).fetchall()
        return [
            {"source": r[0], "invocations": int(r[1] or 0),
             "distinct_users": int(r[2] or 0), "error_count": int(r[3] or 0)}
            for r in rows
        ]

    # ---- marketplace-item rollups ----------------------------------------
    def items_window(self, start_day: date, end_day: date) -> Dict[ItemKey, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT source, type, parent_plugin, name,
                              SUM(count), SUM(distinct_users), SUM(error_count)
                       FROM usage_marketplace_item_daily
                       WHERE day >= :start AND day < :end
                       GROUP BY source, type, parent_plugin, name"""
                ),
                {"start": start_day, "end": end_day},
            ).fetchall()
        return {
            (r[0], r[1], r[2], r[3]): {
                "invocations": int(r[4] or 0),
                "distinct_users": int(r[5] or 0),
                "error_count": int(r[6] or 0),
            }
            for r in rows
        }

    # ---- installs / adoption ---------------------------------------------
    def install_counts(self, start: datetime, end: datetime) -> dict:
        with self._engine.connect() as conn:
            curated = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_plugin_optouts "
                    "WHERE opted_out_at >= :start AND opted_out_at < :end"
                ),
                {"start": start, "end": end},
            ).fetchone()[0]
            flea = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_store_installs "
                    "WHERE installed_at >= :start AND installed_at < :end"
                ),
                {"start": start, "end": end},
            ).fetchone()[0]
        return {"curated": int(curated or 0), "flea": int(flea or 0)}

    def installs_daily(self, start: datetime, end: datetime) -> Dict[date, int]:
        out: Dict[date, int] = {}
        stmts = (
            "SELECT CAST(opted_out_at AS DATE) AS d, COUNT(*) FROM user_plugin_optouts "
            "WHERE opted_out_at >= :start AND opted_out_at < :end GROUP BY d",
            "SELECT CAST(installed_at AS DATE) AS d, COUNT(*) FROM user_store_installs "
            "WHERE installed_at >= :start AND installed_at < :end GROUP BY d",
        )
        with self._engine.connect() as conn:
            for sql in stmts:
                for r in conn.execute(sa.text(sql), {"start": start, "end": end}).fetchall():
                    out[r[0]] = out.get(r[0], 0) + int(r[1] or 0)
        return out

    def installs_curated_detail(self, start: datetime, end: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT marketplace_id, plugin_name, COUNT(*) AS n
                       FROM user_plugin_optouts
                       WHERE opted_out_at >= :start AND opted_out_at < :end
                       GROUP BY marketplace_id, plugin_name ORDER BY n DESC LIMIT :lim"""
                ),
                {"start": start, "end": end, "lim": limit},
            ).fetchall()
        return [
            {"ref_id": f"{r[0]}/{r[1]}", "name": r[1], "installs": int(r[2] or 0)}
            for r in rows
        ]

    def installs_flea_detail(self, start: datetime, end: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT usi.entity_id, s.name, COUNT(*) AS n
                       FROM user_store_installs usi
                       LEFT JOIN store_entities s ON s.id = usi.entity_id
                       WHERE usi.installed_at >= :start AND usi.installed_at < :end
                       GROUP BY usi.entity_id, s.name ORDER BY n DESC LIMIT :lim"""
                ),
                {"start": start, "end": end, "lim": limit},
            ).fetchall()
        return [
            {"entity_id": r[0], "name": r[1], "installs": int(r[2] or 0)}
            for r in rows
        ]
