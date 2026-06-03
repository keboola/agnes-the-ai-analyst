"""Postgres-backed usage repository.

Mirrors ``src/repositories/usage.py``. ``INSERT OR IGNORE`` becomes
``ON CONFLICT DO NOTHING``; ``INSERT OR REPLACE`` becomes
``ON CONFLICT (...) DO UPDATE SET ...``.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.usage import _slow_actions_from_raw


_EVENT_COLS = [
    "id", "session_id", "session_file", "username",
    "event_uuid", "parent_uuid", "event_type",
    "tool_name", "skill_name", "subagent_type", "command_name",
    "is_error", "source", "ref_id", "model", "cwd",
    "occurred_at", "processor_version", "user_id",
]

# Group-by buckets for /telemetry/query — PG dialect.
# 'day' uses CAST(... AS DATE) which is identical to DuckDB.
_GROUP_BY_COLUMNS = {
    "day":       ("CAST(occurred_at AS DATE)", "day"),
    "username":  ("username", "username"),
    "tool_name": ("tool_name", "tool_name"),
    "source":    ("source", "source"),
    "ref_id":    ("ref_id", "ref_id"),
}

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


class UsagePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # telemetry aggregate reads (Postgres).  Mirrors UsageRepository.
    # ------------------------------------------------------------------

    @staticmethod
    def _events_where(filters: dict) -> tuple[str, dict]:
        where = ["occurred_at >= :since"]
        params: dict = {"since": filters["since"]}
        if filters.get("username"):
            where.append("username = :username"); params["username"] = filters["username"]
        if filters.get("tool_name"):
            where.append("tool_name = :tool_name"); params["tool_name"] = filters["tool_name"]
        if filters.get("source"):
            where.append("source = :source"); params["source"] = filters["source"]
        if filters.get("event_type"):
            where.append("event_type = :event_type"); params["event_type"] = filters["event_type"]
        if filters.get("only_errors"):
            where.append("is_error = TRUE")
        if filters.get("q"):
            where.append(
                "(tool_name LIKE :q OR skill_name LIKE :q OR subagent_type LIKE :q "
                "OR command_name LIKE :q)"
            )
            params["q"] = f"%{filters['q']}%"
        return " AND ".join(where), params

    def summary_top_tools(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT tool_name, source, COUNT(*) AS n
                       FROM usage_events
                       WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                       GROUP BY tool_name, source ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [
            {"tool_name": r[0], "source": r[1], "invocations": int(r[2])}
            for r in rows
        ]

    def summary_top_users(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT username, COUNT(*) AS n FROM usage_events
                       WHERE occurred_at >= :cutoff
                       GROUP BY username ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [{"username": r[0], "tool_calls": int(r[1])} for r in rows]

    def summary_error_rate(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT tool_name, COUNT(*) AS n,
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS err
                       FROM usage_events
                       WHERE occurred_at >= :cutoff AND tool_name IS NOT NULL
                       GROUP BY tool_name HAVING COUNT(*) > 0
                       ORDER BY n DESC LIMIT :lim"""
                ),
                {"cutoff": cutoff, "lim": limit},
            ).fetchall()
        return [
            {"tool_name": r[0], "invocations": int(r[1]), "errors": int(r[2]),
             "rate": float(r[2]) / float(r[1]) if r[1] else 0.0}
            for r in rows
        ]

    def summary_dau(self, start_date: date) -> Dict[date, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT CAST(occurred_at AS DATE) AS day,
                              COUNT(DISTINCT username) AS n
                       FROM usage_events
                       WHERE CAST(occurred_at AS DATE) >= :start
                       GROUP BY day ORDER BY day"""
                ),
                {"start": start_date},
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def summary_slow_actions(self, cutoff: datetime, limit: int = 10) -> List[dict]:
        # PG has no approx_quantile; pull raw durations and reuse the shared
        # Python percentile helper so DuckDB and PG return identical shapes.
        with self._engine.connect() as conn:
            raw = conn.execute(
                sa.text(
                    """SELECT action, duration_ms FROM audit_log
                       WHERE timestamp >= :cutoff
                         AND duration_ms IS NOT NULL AND duration_ms > 0"""
                ),
                {"cutoff": cutoff},
            ).fetchall()
        return _slow_actions_from_raw([(r[0], r[1]) for r in raw], limit)

    def telemetry_facets(self, since: datetime) -> dict:
        def _facet(col: str, lim: int) -> list:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        f"SELECT {col}, COUNT(*) AS n FROM usage_events "
                        f"WHERE occurred_at >= :since AND {col} IS NOT NULL "
                        f"GROUP BY {col} ORDER BY n DESC LIMIT :lim"
                    ),
                    {"since": since, "lim": lim},
                ).fetchall()
            return [{"value": r[0], "count": r[1]} for r in rows]

        return {
            "users":       _facet("username", 50),
            "tools":       _facet("tool_name", 50),
            "sources":     _facet("source", 20),
            "event_types": _facet("event_type", 20),
        }

    def telemetry_kpis(self, filters: dict) -> dict:
        where_sql, params = self._events_where(filters)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              COUNT(DISTINCT tool_name),
                              SUM(CASE WHEN is_error THEN 1 ELSE 0 END)
                       FROM usage_events WHERE {where_sql}"""
                ),
                params,
            ).fetchone()
        total, users, tools, errors = (int(x or 0) for x in row)
        return {"events_total": total, "distinct_users": users,
                "distinct_tools": tools, "errors": errors}

    def usage_query(
        self,
        filters: dict,
        *,
        group_by: str | None,
        sort_col: str,
        sort_dir: str,
        limit: int,
        offset: int,
    ) -> dict:
        """Filtered + optionally grouped read against usage_events (PG).

        ``group_by`` ∈ {None, 'day', 'username', 'tool_name', 'source', 'ref_id'}.
        Mirrors ``UsageRepository.usage_query`` with named :params for PG.
        """
        where_sql, params = self._events_where(filters)
        sort_dir = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        if group_by and group_by in _GROUP_BY_COLUMNS:
            expr, alias = _GROUP_BY_COLUMNS[group_by]
            valid_sort = {
                "bucket":            expr,
                "invocations":       "COUNT(*)",
                "distinct_users":    "COUNT(DISTINCT username)",
                "distinct_sessions": "COUNT(DISTINCT session_id)",
                "errors":            "SUM(CASE WHEN is_error THEN 1 ELSE 0 END)",
            }
            order_expr = valid_sort.get(sort_col, "COUNT(*)")
            with self._engine.connect() as conn:
                total_buckets = int(conn.execute(
                    sa.text(
                        f"SELECT COUNT(DISTINCT {expr}) FROM usage_events WHERE {where_sql}"
                    ),
                    params,
                ).scalar() or 0)
                rows = conn.execute(
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
                    dict(params, lim=limit, off=offset),
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
                "group_by":    group_by,
                "group_alias": alias,
                "rows":        out,
                "total":       total_buckets,
                "limit":       limit,
                "offset":      offset,
                "next_offset": offset + limit if (offset + limit) < total_buckets else None,
            }

        # ungrouped — raw events
        _COLS = [
            "id", "occurred_at", "username", "source", "ref_id", "event_type",
            "tool_name", "skill_name", "subagent_type", "command_name", "is_error",
            "session_id", "model",
        ]
        valid_sort_raw = {"occurred_at": "occurred_at", "invocations": "occurred_at"}
        order_expr = valid_sort_raw.get(sort_col, "occurred_at")
        with self._engine.connect() as conn:
            total = int(conn.execute(
                sa.text(f"SELECT COUNT(*) FROM usage_events WHERE {where_sql}"),
                params,
            ).scalar() or 0)
            rows = conn.execute(
                sa.text(
                    f"""SELECT {','.join(_COLS)}
                       FROM usage_events WHERE {where_sql}
                       ORDER BY {order_expr} {sort_dir}
                       LIMIT :lim OFFSET :off"""
                ),
                dict(params, lim=limit, off=offset),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(_COLS, r))
            if d.get("occurred_at"):
                d["occurred_at"] = d["occurred_at"].isoformat()
            out.append(d)
        return {
            "group_by":    None,
            "rows":        out,
            "total":       total,
            "limit":       limit,
            "offset":      offset,
            "next_offset": offset + limit if (offset + limit) < total else None,
        }

    # ------------------------------------------------------------------
    # session summary aggregate reads (Postgres).
    # ------------------------------------------------------------------

    @staticmethod
    def _sessions_where(filters: dict) -> tuple[str, dict]:
        where = ["started_at >= :since"]
        params: dict = {"since": filters["since"]}
        if filters.get("username"):
            where.append("username = :username"); params["username"] = filters["username"]
        if filters.get("model"):
            where.append("primary_model = :model"); params["model"] = filters["model"]
        if filters.get("only_errors"):
            where.append("tool_errors > 0")
        if filters.get("q"):
            where.append("(session_id LIKE :q OR session_file LIKE :q)")
            params["q"] = f"%{filters['q']}%"
        return " AND ".join(where), params

    def sessions_count(self, filters: dict) -> int:
        where_sql, params = self._sessions_where(filters)
        with self._engine.connect() as conn:
            v = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM usage_session_summary WHERE {where_sql}"),
                params,
            ).scalar()
        return int(v or 0)

    def sessions_list(self, filters: dict, *, sort_col: str, direction: str,
                      limit: int, offset: int) -> List[dict]:
        where_sql, params = self._sessions_where(filters)
        col = _SESSION_SORT_KEYS.get(sort_col, "started_at")
        direction = "ASC" if direction.upper() == "ASC" else "DESC"
        params = dict(params, lim=limit, off=offset)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT {','.join(_SESSION_COLS)}
                       FROM usage_session_summary WHERE {where_sql}
                       ORDER BY {col} {direction}
                       LIMIT :lim OFFSET :off"""
                ),
                params,
            ).fetchall()
        return [dict(zip(_SESSION_COLS, r)) for r in rows]

    def sessions_kpis(self, filters: dict) -> dict:
        where_sql, params = self._sessions_where(filters)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"""SELECT COUNT(*),
                              COUNT(DISTINCT username),
                              SUM(CASE WHEN tool_errors > 0 THEN 1 ELSE 0 END),
                              SUM(tool_calls),
                              SUM(tool_errors)
                       FROM usage_session_summary WHERE {where_sql}"""
                ),
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

    def sessions_facets(self, since: datetime) -> dict:
        """Distinct usernames + models present in usage_session_summary for the window."""
        with self._engine.connect() as conn:
            users = conn.execute(
                sa.text(
                    "SELECT username, COUNT(*) AS n FROM usage_session_summary "
                    "WHERE started_at >= :since AND username IS NOT NULL "
                    "GROUP BY username ORDER BY n DESC LIMIT 50"
                ),
                {"since": since},
            ).fetchall()
            models = conn.execute(
                sa.text(
                    "SELECT primary_model, COUNT(*) AS n FROM usage_session_summary "
                    "WHERE started_at >= :since AND primary_model IS NOT NULL "
                    "GROUP BY primary_model ORDER BY n DESC LIMIT 30"
                ),
                {"since": since},
            ).fetchall()
        return {
            "users":  [{"value": r[0], "count": r[1]} for r in users],
            "models": [{"value": r[0], "count": r[1]} for r in models],
        }

    def get_session_summary(self, session_file: str) -> dict | None:
        """Return a summary row dict for a single session_file, or None."""
        _KEYS = (
            "session_id", "started_at", "ended_at", "active_seconds", "wall_seconds",
            "user_messages", "assistant_messages", "tool_calls", "tool_errors",
            "primary_model",
        )
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT session_id, started_at, ended_at, active_seconds, wall_seconds, "
                    "user_messages, assistant_messages, tool_calls, tool_errors, "
                    "primary_model FROM usage_session_summary WHERE session_file = :sf"
                ),
                {"sf": session_file},
            ).fetchone()
        if row is None:
            return None
        return dict(zip(_KEYS, row))

    # ------------------------------------------------------------------
    # write methods
    # ------------------------------------------------------------------

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        if not rows:
            return 0
        placeholders = ",".join(f":{c}" for c in _EVENT_COLS)
        sql = (
            f"INSERT INTO usage_events ({','.join(_EVENT_COLS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        with self._engine.begin() as conn:
            for r in rows:
                params = {c: r.get(c) for c in _EVENT_COLS}
                # Backfill the DEFAULT-FALSE columns DuckDB would coerce from
                # NULL silently. PG is strict on NOT NULL even when a default
                # exists — see GH-XXX for the equivalent INSERT semantics gap.
                if params.get("is_error") is None:
                    params["is_error"] = False
                params["processor_version"] = processor_version
                conn.execute(sa.text(sql), params)
        return len(rows)

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        Mirrors ``UsageRepository.emit_server_event`` (DuckDB). ``props`` is
        serialized into the ``friction_tags`` JSONB column via
        ``CAST(:friction_tags AS JSONB)`` (the project's PG-JSONB write
        convention); ``session_id`` / ``session_file`` are server-synthetic so
        the NOT NULL constraints stay satisfied.
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO usage_events
                       (id, session_id, session_file, username, event_type,
                        is_error, source, occurred_at, processor_version,
                        friction_tags, user_id)
                       VALUES (:id, :session_id, :session_file, :username, :event_type,
                               FALSE, 'server', :occurred_at, 1,
                               CAST(:friction_tags AS JSONB), :user_id)"""
                ),
                {
                    "id": event_id,
                    "session_id": f"server-{event_id[:8]}",
                    "session_file": f"server/{event_type}.jsonl",
                    "username": username or (user_id or "anonymous"),
                    "event_type": event_type,
                    "occurred_at": now,
                    "friction_tags": json.dumps(props) if props else None,
                    "user_id": user_id,
                },
            )
        return event_id

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO usage_session_summary
                        (session_file, session_id, username, started_at, ended_at,
                         active_seconds, wall_seconds, user_messages, assistant_messages,
                         tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                         mcp_calls, slash_commands, distinct_tools, distinct_skills,
                         primary_model, input_tokens, output_tokens, cache_read_tokens,
                         cache_creation_tokens, processor_version, user_id)
                    VALUES (:sf, :sid, :u, :sa, :ea,
                            :acts, :walls, :um, :am,
                            :tc, :te, :si, :sd,
                            :mc, :sc, :dt, :ds,
                            :pm, :it, :ot, :crt,
                            :cct, :pv, :uid)
                    ON CONFLICT (session_file) DO UPDATE SET
                      session_id           = EXCLUDED.session_id,
                      username             = EXCLUDED.username,
                      started_at           = EXCLUDED.started_at,
                      ended_at             = EXCLUDED.ended_at,
                      active_seconds       = EXCLUDED.active_seconds,
                      wall_seconds         = EXCLUDED.wall_seconds,
                      user_messages        = EXCLUDED.user_messages,
                      assistant_messages   = EXCLUDED.assistant_messages,
                      tool_calls           = EXCLUDED.tool_calls,
                      tool_errors          = EXCLUDED.tool_errors,
                      skill_invocations    = EXCLUDED.skill_invocations,
                      subagent_dispatches  = EXCLUDED.subagent_dispatches,
                      mcp_calls            = EXCLUDED.mcp_calls,
                      slash_commands       = EXCLUDED.slash_commands,
                      distinct_tools       = EXCLUDED.distinct_tools,
                      distinct_skills      = EXCLUDED.distinct_skills,
                      primary_model        = EXCLUDED.primary_model,
                      input_tokens         = EXCLUDED.input_tokens,
                      output_tokens        = EXCLUDED.output_tokens,
                      cache_read_tokens    = EXCLUDED.cache_read_tokens,
                      cache_creation_tokens= EXCLUDED.cache_creation_tokens,
                      processor_version    = EXCLUDED.processor_version,
                      user_id              = EXCLUDED.user_id
                    """
                ),
                {
                    "sf": summary["session_file"],
                    "sid": summary.get("session_id", ""),
                    "u": summary["username"],
                    "sa": summary.get("started_at"),
                    "ea": summary.get("ended_at"),
                    "acts": summary.get("active_seconds", 0),
                    "walls": summary.get("wall_seconds", 0),
                    "um": summary.get("user_messages", 0),
                    "am": summary.get("assistant_messages", 0),
                    "tc": summary.get("tool_calls", 0),
                    "te": summary.get("tool_errors", 0),
                    "si": summary.get("skill_invocations", 0),
                    "sd": summary.get("subagent_dispatches", 0),
                    "mc": summary.get("mcp_calls", 0),
                    "sc": summary.get("slash_commands", 0),
                    "dt": summary.get("distinct_tools", 0),
                    "ds": summary.get("distinct_skills", 0),
                    "pm": summary.get("primary_model"),
                    "it": summary.get("input_tokens", 0),
                    "ot": summary.get("output_tokens", 0),
                    "crt": summary.get("cache_read_tokens", 0),
                    "cct": summary.get("cache_creation_tokens", 0),
                    "pv": processor_version,
                    "uid": summary.get("user_id"),
                },
            )

    def purge_for_session(self, session_file: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM usage_events WHERE session_file = :sf RETURNING 1"
                ),
                {"sf": session_file},
            ).all()
            events_deleted = len(rows)
            conn.execute(
                sa.text(
                    "DELETE FROM usage_session_summary WHERE session_file = :sf"
                ),
                {"sf": session_file},
            )
        return events_deleted

    def delete_older_than(self, days: int) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM usage_events "
                    "WHERE occurred_at < (CURRENT_TIMESTAMP - (:days::TEXT || ' days')::INTERVAL) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)
