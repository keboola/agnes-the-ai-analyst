"""Repository for the marketplace usage digest (admin reports).

Cross-table reporting reads kept behind the backend switch so the digest
endpoint never runs raw state SQL on the always-DuckDB ``_get_db`` connection
(the #518 backend-split bug). The PG mirror lives in ``reports_pg.py``.

Window convention: every ``(start, end)`` is a half-open interval with an
EXCLUSIVE upper bound; day-grain windows take ``date`` bounds, timestamp-grain
windows take aware ``datetime`` bounds.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Tuple

import duckdb

ItemKey = Tuple[str, str, str, str]  # (source, type, parent_plugin, name)


class ReportsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ---- usage_events / sessions windows ---------------------------------
    def event_window(self, start: datetime, end: datetime) -> dict:
        r = self.conn.execute(
            """SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ?""",
            [start, end],
        ).fetchone()
        return {"invocations": int(r[0] or 0), "active_users": int(r[1] or 0), "errors": int(r[2] or 0)}

    def session_count(self, start: datetime, end: datetime) -> int:
        return int(
            self.conn.execute(
                """SELECT COUNT(DISTINCT session_id) FROM usage_session_summary
               WHERE started_at >= ? AND started_at < ?""",
                [start, end],
            ).fetchone()[0]
            or 0
        )

    def events_daily(self, start: datetime, end: datetime) -> Dict[date, dict]:
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS d,
                      COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ?
               GROUP BY d""",
            [start, end],
        ).fetchall()
        return {
            r[0]: {"invocations": int(r[1] or 0), "active_users": int(r[2] or 0), "errors": int(r[3] or 0)}
            for r in rows
        }

    def by_source(self, start: datetime, end: datetime) -> List[dict]:
        rows = self.conn.execute(
            """SELECT source, COUNT(*), COUNT(DISTINCT username),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events
               WHERE occurred_at >= ? AND occurred_at < ? AND source IS NOT NULL
               GROUP BY source ORDER BY 2 DESC""",
            [start, end],
        ).fetchall()
        return [
            {
                "source": r[0],
                "invocations": int(r[1] or 0),
                "distinct_users": int(r[2] or 0),
                "error_count": int(r[3] or 0),
            }
            for r in rows
        ]

    # ---- marketplace-item rollups ----------------------------------------
    def items_window(self, start_day: date, end_day: date) -> Dict[ItemKey, dict]:
        rows = self.conn.execute(
            """SELECT source, type, parent_plugin, name,
                      SUM(count), SUM(distinct_users), SUM(error_count)
               FROM usage_marketplace_item_daily
               WHERE day >= ? AND day < ?
               GROUP BY source, type, parent_plugin, name""",
            [start_day, end_day],
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
        curated = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_plugin_optouts WHERE opted_out_at >= ? AND opted_out_at < ?",
                [start, end],
            ).fetchone()[0]
            or 0
        )
        flea = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_store_installs WHERE installed_at >= ? AND installed_at < ?",
                [start, end],
            ).fetchone()[0]
            or 0
        )
        return {"curated": curated, "flea": flea}

    def installs_daily(self, start: datetime, end: datetime) -> Dict[date, int]:
        out: Dict[date, int] = {}
        for sql in (
            "SELECT CAST(opted_out_at AS DATE) AS d, COUNT(*) FROM user_plugin_optouts "
            "WHERE opted_out_at >= ? AND opted_out_at < ? GROUP BY d",
            "SELECT CAST(installed_at AS DATE) AS d, COUNT(*) FROM user_store_installs "
            "WHERE installed_at >= ? AND installed_at < ? GROUP BY d",
        ):
            for r in self.conn.execute(sql, [start, end]).fetchall():
                out[r[0]] = out.get(r[0], 0) + int(r[1] or 0)
        return out

    def installs_curated_detail(self, start: datetime, end: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT marketplace_id, plugin_name, COUNT(*) AS n
               FROM user_plugin_optouts
               WHERE opted_out_at >= ? AND opted_out_at < ?
               GROUP BY marketplace_id, plugin_name ORDER BY n DESC LIMIT ?""",
            [start, end, limit],
        ).fetchall()
        return [{"ref_id": f"{r[0]}/{r[1]}", "name": r[1], "installs": int(r[2] or 0)} for r in rows]

    def installs_flea_detail(self, start: datetime, end: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT usi.entity_id, s.name, COUNT(*) AS n
               FROM user_store_installs usi
               LEFT JOIN store_entities s ON s.id = usi.entity_id
               WHERE usi.installed_at >= ? AND usi.installed_at < ?
               GROUP BY usi.entity_id, s.name ORDER BY n DESC LIMIT ?""",
            [start, end, limit],
        ).fetchall()
        return [{"entity_id": r[0], "name": r[1], "installs": int(r[2] or 0)} for r in rows]
