"""Repository for usage_events and usage_session_summary tables (schema v41)."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


# Group-by buckets shared by the /telemetry/query endpoint. The first element
# is the SQL expression, the second a stable alias the UI keys on.
_GROUP_BY_COLUMNS = {
    "day":       ("CAST(occurred_at AS DATE)", "day"),
    "username":  ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source":    ("source", "source"),
    "ref_id":    ("ref_id", "ref_id"),
}


class UsageRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ------------------------------------------------------------------
    # telemetry aggregate reads (DuckDB).  Mirrored in UsagePgRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _events_where(filters: dict) -> tuple[str, list]:
        """Compose a parametrised WHERE clause over usage_events.

        ``filters`` keys (all optional except ``since``):
          since (datetime, required), username, tool_name, source,
          event_type, only_errors (bool), q (free-text).
        """
        where = ["occurred_at >= ?"]
        params: list = [filters["since"]]
        if filters.get("username"):
            where.append("username = ?"); params.append(filters["username"])
        if filters.get("tool_name"):
            where.append("tool_name = ?"); params.append(filters["tool_name"])
        if filters.get("source"):
            where.append("source = ?"); params.append(filters["source"])
        if filters.get("event_type"):
            where.append("event_type = ?"); params.append(filters["event_type"])
        if filters.get("only_errors"):
            where.append("is_error = TRUE")
        if filters.get("q"):
            where.append(
                "(tool_name LIKE ? OR skill_name LIKE ? OR subagent_type LIKE ? "
                "OR command_name LIKE ?)"
            )
            like = f"%{filters['q']}%"
            params.extend([like, like, like, like])
        return " AND ".join(where), params

    def summary_top_tools(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT tool_name, source, COUNT(*) AS n
               FROM usage_events
               WHERE occurred_at >= ? AND tool_name IS NOT NULL
               GROUP BY tool_name, source ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
        return [
            {"tool_name": r[0], "source": r[1], "invocations": int(r[2])}
            for r in rows
        ]

    def summary_top_users(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT username, COUNT(*) AS n FROM usage_events
               WHERE occurred_at >= ? GROUP BY username ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
        return [{"username": r[0], "tool_calls": int(r[1])} for r in rows]

    def summary_error_rate(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            """SELECT tool_name, COUNT(*) AS n,
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
               FROM usage_events
               WHERE occurred_at >= ? AND tool_name IS NOT NULL
               GROUP BY tool_name HAVING COUNT(*) > 0 ORDER BY n DESC LIMIT ?""",
            [cutoff, limit],
        ).fetchall()
        return [
            {"tool_name": r[0], "invocations": int(r[1]), "errors": int(r[2]),
             "rate": float(r[2]) / float(r[1]) if r[1] else 0.0}
            for r in rows
        ]

    def summary_dau(self, start_date: date) -> Dict[date, int]:
        """DAU map: day → distinct active users, for days >= start_date."""
        rows = self.conn.execute(
            """SELECT CAST(occurred_at AS DATE) AS day, COUNT(DISTINCT username) AS n
               FROM usage_events
               WHERE CAST(occurred_at AS DATE) >= ?
               GROUP BY day ORDER BY day""",
            [start_date],
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def summary_slow_actions(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        """Percentile latency per audit action over the window. Uses
        approx_quantile; on failure falls back to pulling raw durations and
        computing percentiles in Python."""
        try:
            rows = self.conn.execute(
                """SELECT action,
                          approx_quantile(duration_ms, 0.5)  AS p50,
                          approx_quantile(duration_ms, 0.95) AS p95,
                          approx_quantile(duration_ms, 0.99) AS p99,
                          MAX(duration_ms) AS max_ms,
                          COUNT(*) AS n
                   FROM audit_log
                   WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0
                   GROUP BY action HAVING n >= 5
                   ORDER BY p95 DESC LIMIT ?""",
                [cutoff, limit],
            ).fetchall()
            return [
                {"action": r[0], "p50": int(r[1] or 0), "p95": int(r[2] or 0),
                 "p99": int(r[3] or 0), "max_ms": int(r[4] or 0), "n": int(r[5])}
                for r in rows
            ]
        except Exception:
            raw = self.conn.execute(
                """SELECT action, duration_ms FROM audit_log
                   WHERE timestamp >= ? AND duration_ms IS NOT NULL AND duration_ms > 0""",
                [cutoff],
            ).fetchall()
            return _slow_actions_from_raw(raw, limit)

    def telemetry_facets(self, since: datetime) -> dict:
        users = self.conn.execute(
            "SELECT username, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND username IS NOT NULL GROUP BY username ORDER BY n DESC LIMIT 50",
            [since],
        ).fetchall()
        tools = self.conn.execute(
            "SELECT tool_name, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY n DESC LIMIT 50",
            [since],
        ).fetchall()
        sources = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND source IS NOT NULL GROUP BY source ORDER BY n DESC LIMIT 20",
            [since],
        ).fetchall()
        event_types = self.conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM usage_events WHERE occurred_at >= ? "
            "AND event_type IS NOT NULL GROUP BY event_type ORDER BY n DESC LIMIT 20",
            [since],
        ).fetchall()
        return {
            "users":       [{"value": r[0], "count": r[1]} for r in users],
            "tools":       [{"value": r[0], "count": r[1]} for r in tools],
            "sources":     [{"value": r[0], "count": r[1]} for r in sources],
            "event_types": [{"value": r[0], "count": r[1]} for r in event_types],
        }

    def telemetry_kpis(self, filters: dict) -> dict:
        where_sql, params = self._events_where(filters)
        row = self.conn.execute(
            f"""SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      COUNT(DISTINCT tool_name),
                      SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
               FROM usage_events WHERE {where_sql}""",
            params,
        ).fetchone()
        total, users, tools, errors = (int(x or 0) for x in row)
        return {"events_total": total, "distinct_users": users,
                "distinct_tools": tools, "errors": errors}

    # ------------------------------------------------------------------
    # session summary aggregate reads (DuckDB).
    # ------------------------------------------------------------------

    @staticmethod
    def _sessions_where(filters: dict) -> tuple[str, list]:
        where = ["started_at >= ?"]
        params: list = [filters["since"]]
        if filters.get("username"):
            where.append("username = ?"); params.append(filters["username"])
        if filters.get("model"):
            where.append("primary_model = ?"); params.append(filters["model"])
        if filters.get("only_errors"):
            where.append("tool_errors > 0")
        if filters.get("q"):
            where.append("(session_id LIKE ? OR session_file LIKE ?)")
            like = f"%{filters['q']}%"
            params.extend([like, like])
        return " AND ".join(where), params

    _SESSION_SORT_KEYS = {
        "started_at": "started_at", "ended_at": "ended_at",
        "tool_calls": "tool_calls", "tool_errors": "tool_errors",
        "active_seconds": "active_seconds", "username": "username",
        "primary_model": "primary_model",
    }

    _SESSION_COLS = [
        "session_file", "session_id", "username",
        "started_at", "ended_at", "active_seconds", "wall_seconds",
        "user_messages", "assistant_messages",
        "tool_calls", "tool_errors",
        "skill_invocations", "subagent_dispatches",
        "mcp_calls", "slash_commands",
        "distinct_tools", "distinct_skills", "primary_model",
    ]

    def sessions_count(self, filters: dict) -> int:
        where_sql, params = self._sessions_where(filters)
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM usage_session_summary WHERE {where_sql}",
            params,
        ).fetchone()[0] or 0)

    def sessions_list(self, filters: dict, *, sort_col: str, direction: str,
                      limit: int, offset: int) -> List[dict]:
        where_sql, params = self._sessions_where(filters)
        col = self._SESSION_SORT_KEYS.get(sort_col, "started_at")
        direction = "ASC" if direction.upper() == "ASC" else "DESC"
        rows = self.conn.execute(
            f"""SELECT {','.join(self._SESSION_COLS)}
               FROM usage_session_summary WHERE {where_sql}
               ORDER BY {col} {direction}
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [dict(zip(self._SESSION_COLS, r)) for r in rows]

    def sessions_kpis(self, filters: dict) -> dict:
        where_sql, params = self._sessions_where(filters)
        row = self.conn.execute(
            f"""SELECT COUNT(*),
                      COUNT(DISTINCT username),
                      SUM(CASE WHEN tool_errors > 0 THEN 1 ELSE 0 END),
                      SUM(tool_calls),
                      SUM(tool_errors)
               FROM usage_session_summary WHERE {where_sql}""",
            params,
        ).fetchone()
        sessions_total, users, error_sessions, tool_calls_total, tool_errors_total = (
            int(x or 0) for x in row
        )
        return {
            "sessions_total": sessions_total, "distinct_users": users,
            "error_sessions": error_sessions,
            "tool_calls_total": tool_calls_total,
            "tool_errors_total": tool_errors_total,
        }

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        """INSERT OR IGNORE keyed by event id. Returns number of input rows passed (not new inserts;
        DuckDB returns rowcount=-1 for INSERT OR IGNORE so we cannot cheaply count new vs duplicate).
        """
        if not rows:
            return 0
        cols = [
            "id",
            "session_id",
            "session_file",
            "username",
            "event_uuid",
            "parent_uuid",
            "event_type",
            "tool_name",
            "skill_name",
            "subagent_type",
            "command_name",
            "is_error",
            "source",
            "ref_id",
            "model",
            "cwd",
            "occurred_at",
            "processor_version",
            "user_id",
        ]
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO usage_events ({','.join(cols)}) VALUES ({placeholders})"
        self.conn.executemany(
            sql, [[r.get(c) if c != "processor_version" else processor_version for c in cols] for r in rows]
        )
        return len(rows)

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        """INSERT OR REPLACE on session_file PK."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO usage_session_summary
                (session_file, session_id, username, started_at, ended_at,
                 active_seconds, wall_seconds, user_messages, assistant_messages,
                 tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                 mcp_calls, slash_commands, distinct_tools, distinct_skills,
                 primary_model, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, processor_version, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                summary["session_file"],
                summary.get("session_id", ""),
                summary["username"],
                summary.get("started_at"),
                summary.get("ended_at"),
                summary.get("active_seconds", 0),
                summary.get("wall_seconds", 0),
                summary.get("user_messages", 0),
                summary.get("assistant_messages", 0),
                summary.get("tool_calls", 0),
                summary.get("tool_errors", 0),
                summary.get("skill_invocations", 0),
                summary.get("subagent_dispatches", 0),
                summary.get("mcp_calls", 0),
                summary.get("slash_commands", 0),
                summary.get("distinct_tools", 0),
                summary.get("distinct_skills", 0),
                summary.get("primary_model"),
                summary.get("input_tokens", 0),
                summary.get("output_tokens", 0),
                summary.get("cache_read_tokens", 0),
                summary.get("cache_creation_tokens", 0),
                processor_version,
                summary.get("user_id"),
            ],
        )

    def purge_for_session(self, session_file: str) -> int:
        """DELETE events + summary for one session — used on reprocess."""
        r = self.conn.execute("DELETE FROM usage_events WHERE session_file = ?", [session_file])
        events_deleted = r.rowcount if r.rowcount else 0
        self.conn.execute("DELETE FROM usage_session_summary WHERE session_file = ?", [session_file])
        return events_deleted

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        v49 unified-stack telemetry — Section 9.2 of the design spec. Reuses
        the existing usage_events table for ``stack.subscribe``,
        ``memory.dismiss``, ``data_package.view`` etc. so /admin/telemetry can
        slice them through the existing summary tools.

        Conventions:
          - ``event_type``   → goes in the column of the same name (e.g.
            ``stack.subscribe``); dotted namespacing is intentional so admins
            can prefix-filter.
          - ``source='server'`` distinguishes these from CC-session events.
          - ``session_id`` / ``session_file`` are server-synthetic UUIDs so
            the NOT NULL constraints stay satisfied. Telemetry consumers
            should key on ``user_id`` + ``event_type``, not session.
          - ``props`` is serialized into ``friction_tags`` (JSON column we
            piggyback for arbitrary event payload — the rename is tracked in
            spec Section 9 as a follow-up).
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO usage_events
               (id, session_id, session_file, username, event_type,
                is_error, source, occurred_at, processor_version,
                friction_tags, user_id)
               VALUES (?, ?, ?, ?, ?, FALSE, 'server', ?, 1, ?, ?)""",
            [
                event_id,
                f"server-{event_id[:8]}",
                f"server/{event_type}.jsonl",
                username or (user_id or "anonymous"),
                event_type,
                now,
                json.dumps(props) if props else None,
                user_id,
            ],
        )
        return event_id

    def delete_older_than(self, days: int) -> int:
        """Retention prune — DELETE events older than now - days."""
        r = self.conn.execute(
            """
            DELETE FROM usage_events
            WHERE occurred_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY)
            """,
            [days],
        )
        return r.rowcount if r.rowcount else 0


def _percentile(values: list[float], p: float) -> float:
    """Pure-Python percentile (linear interpolation)."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    idx = p * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def _slow_actions_from_raw(raw_rows: list, limit: int) -> list[dict]:
    """Compute p50/p95/p99/max per action from (action, duration_ms) rows."""
    action_durations: Dict[str, List[float]] = {}
    for action, duration_ms in raw_rows:
        action_durations.setdefault(action, []).append(float(duration_ms))
    out = []
    for action, vals in action_durations.items():
        if len(vals) < 5:
            continue
        out.append({
            "action": action,
            "p50": int(_percentile(vals, 0.5)),
            "p95": int(_percentile(vals, 0.95)),
            "p99": int(_percentile(vals, 0.99)),
            "max_ms": int(max(vals)),
            "n": len(vals),
        })
    return sorted(out, key=lambda x: x["p95"], reverse=True)[:limit]
