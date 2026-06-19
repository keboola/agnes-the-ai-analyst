"""Postgres-backed audit repository.

Mirrors ``src/repositories/audit.py`` (the DuckDB impl) on the
``AuditRepositoryProtocol`` surface. Both must return identical results
for identical inputs; ``tests/db_pg/test_audit_contract.py`` parametrises
across both and fails on any drift.

Implementation differences vs. DuckDB:
  - JSON columns use psycopg's native JSONB adapter — params go in as
    dicts, come out as dicts. No json.dumps in the write path, no
    json.loads in the read path.
  - Keyset pagination uses the standard PG ``(timestamp, id) < (?, ?)``
    row-comparator (DuckDB supports the same syntax; this is a parity
    win, not a divergence).
  - Full-text ``q`` filter is a casts-to-text LIKE — for the future
    PG-specific FTS upgrade, see the "future improvements" section in
    docs/migrations.md.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class AuditPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # -----------------------------------------------------------------
    # write
    # -----------------------------------------------------------------
    def log(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        resource: Optional[str] = None,
        params: Optional[dict] = None,
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        *,
        params_before: Optional[dict] = None,
        client_ip: Optional[str] = None,
        client_kind: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> str:
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO audit_log
                      (id, timestamp, user_id, action, resource, params,
                       result, duration_ms, params_before, client_ip,
                       client_kind, correlation_id)
                    VALUES
                      (:id, :ts, :user_id, :action, :resource,
                       CAST(:params AS JSONB),
                       :result, :duration_ms,
                       CAST(:params_before AS JSONB),
                       :client_ip, :client_kind, :correlation_id)
                    """
                ),
                {
                    "id": entry_id,
                    "ts": now,
                    "user_id": user_id,
                    "action": action,
                    "resource": resource,
                    "params": _json_param(params),
                    "result": result,
                    "duration_ms": duration_ms,
                    "params_before": _json_param(params_before),
                    "client_ip": client_ip,
                    "client_kind": client_kind,
                    "correlation_id": correlation_id,
                },
            )
        return entry_id

    # -----------------------------------------------------------------
    # read — filtered query with cursor pagination
    # -----------------------------------------------------------------
    def query(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        action_prefix: Optional[str] = None,
        action_in: Optional[List[str]] = None,
        resource: Optional[str] = None,
        resource_prefix: Optional[str] = None,
        result_pattern: Optional[str] = None,
        correlation_id: Optional[str] = None,
        q: Optional[str] = None,
        cursor: Optional[Tuple[datetime, str]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[datetime, str]]]:
        where: List[str] = []
        params: Dict[str, Any] = {}

        if since is not None:
            where.append("timestamp >= :since")
            params["since"] = since
        if until is not None:
            where.append("timestamp < :until")
            params["until"] = until
        if user_id is not None:
            where.append("user_id = :user_id")
            params["user_id"] = user_id
        if action is not None:
            where.append("action = :action_eq")
            params["action_eq"] = action
        if action_prefix is not None:
            where.append("action LIKE :action_prefix")
            params["action_prefix"] = action_prefix + "%"
        if action_in:
            in_keys: List[str] = []
            for i, a in enumerate(action_in):
                k = f"action_in_{i}"
                in_keys.append(f":{k}")
                params[k] = a
            where.append(f"action IN ({','.join(in_keys)})")
        if resource is not None:
            where.append("resource = :resource_eq")
            params["resource_eq"] = resource
        if resource_prefix is not None:
            where.append("resource LIKE :resource_prefix")
            params["resource_prefix"] = resource_prefix + "%"
        if result_pattern is not None:
            where.append("result LIKE :result_pattern")
            params["result_pattern"] = result_pattern
        if correlation_id is not None:
            where.append("correlation_id = :correlation_id")
            params["correlation_id"] = correlation_id
        if q:
            # Free-text scan over the JSON params blob. Mirror the DuckDB
            # impl's 7-day cap when caller hasn't passed `since`.
            if since is None:
                since = datetime.now(timezone.utc) - timedelta(days=7)
                where.append("timestamp >= :since")
                params["since"] = since
            where.append("CAST(params AS TEXT) LIKE :q")
            params["q"] = f"%{q}%"
        if cursor is not None:
            ts, cid = cursor
            where.append("(timestamp, id) < (:cursor_ts, :cursor_id)")
            params["cursor_ts"] = ts
            params["cursor_id"] = cid

        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT :limit_plus_one"
        params["limit_plus_one"] = limit + 1

        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            rows = [dict(r._mapping) for r in result]

        if not rows:
            return [], None

        next_cursor: Optional[Tuple[datetime, str]] = None
        if len(rows) > limit:
            last_shown = rows[limit - 1]
            next_cursor = (last_shown["timestamp"], last_shown["id"])
            rows = rows[:limit]
        return rows, next_cursor

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------
    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not actions:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        for i, a in enumerate(actions):
            k = f"action_{i}"
            in_keys.append(f":{k}")
            params[k] = a
        sql = (
            f"SELECT * FROM audit_log WHERE action IN ({','.join(in_keys)}) "
            f"ORDER BY timestamp DESC LIMIT :limit"
        )
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not resources:
            return []
        in_keys: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        for i, r in enumerate(resources):
            k = f"resource_{i}"
            in_keys.append(f":{k}")
            params[k] = r
        sql = (
            f"SELECT * FROM audit_log WHERE resource IN ({','.join(in_keys)}) "
            f"ORDER BY timestamp DESC LIMIT :limit"
        )
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    # -----------------------------------------------------------------
    # aggregates — counts, governance feed, observability facets/KPIs
    # -----------------------------------------------------------------
    def count_for_user(self, user_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM audit_log WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def query_governance(
        self,
        *,
        action: Optional[str] = None,
        prefixes: Tuple[str, ...] = ("corporate_memory.", "km_"),
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        p0, p1 = prefixes
        if action:
            sql = (
                "SELECT * FROM audit_log WHERE action IN (:a0, :a1) "
                "ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset"
            )
            params: Dict[str, Any] = {
                "a0": f"{p0}{action}",
                "a1": f"{p1}{action}",
                "limit": limit,
                "offset": offset,
            }
        else:
            sql = (
                "SELECT * FROM audit_log "
                "WHERE action LIKE :p0 OR action LIKE :p1 "
                "ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset"
            )
            params = {
                "p0": f"{p0}%",
                "p1": f"{p1}%",
                "limit": limit,
                "offset": offset,
            }
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params)
            return [dict(r._mapping) for r in result]

    def facets(
        self,
        *,
        since: datetime,
        scheduler_actions: list[str],
        limit: int = 50,
    ) -> "dict[str, list[dict]]":
        with self._engine.connect() as conn:
            users = conn.execute(
                sa.text(
                    "SELECT user_id AS id, COUNT(*) AS n FROM audit_log "
                    "WHERE timestamp >= :since AND user_id IS NOT NULL "
                    "GROUP BY user_id ORDER BY n DESC LIMIT :limit"
                ),
                {"since": since, "limit": limit},
            ).fetchall()
            actions = conn.execute(
                sa.text(
                    "SELECT action AS label, COUNT(*) AS n FROM audit_log "
                    "WHERE timestamp >= :since AND action IS NOT NULL "
                    "GROUP BY action ORDER BY n DESC LIMIT :limit"
                ),
                {"since": since, "limit": limit},
            ).fetchall()
            results = conn.execute(
                sa.text(
                    "SELECT COALESCE(result, '—') AS label, COUNT(*) AS n "
                    "FROM audit_log WHERE timestamp >= :since "
                    "GROUP BY result ORDER BY n DESC LIMIT :limit"
                ),
                {"since": since, "limit": limit},
            ).fetchall()
            resources = conn.execute(
                sa.text(
                    "SELECT resource AS label, COUNT(*) AS n FROM audit_log "
                    "WHERE timestamp >= :since AND resource IS NOT NULL "
                    "GROUP BY resource ORDER BY n DESC LIMIT :limit"
                ),
                {"since": since, "limit": limit},
            ).fetchall()
            source_rows = conn.execute(
                sa.text(
                    """
                    SELECT
                      CASE
                        WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind
                        WHEN action IN :sched THEN 'scheduler'
                        WHEN user_id IS NULL THEN 'system'
                        ELSE 'other'
                      END AS src,
                      COUNT(*) AS n
                    FROM audit_log WHERE timestamp >= :since
                    GROUP BY src ORDER BY n DESC LIMIT :limit
                    """
                ).bindparams(sa.bindparam("sched", expanding=True)),
                {"since": since, "limit": limit, "sched": list(scheduler_actions)},
            ).fetchall()
        return {
            "users":     [{"id": r[0], "count": r[1]} for r in users],
            "actions":   [{"value": r[0], "count": r[1]} for r in actions],
            "results":   [{"value": r[0], "count": r[1]} for r in results],
            "resources": [{"value": r[0], "count": r[1]} for r in resources],
            "sources":   [{"value": r[0], "count": r[1]} for r in source_rows],
        }

    def kpis(self, *, since: datetime) -> "dict[str, Any]":
        """Headline KPIs over the window: events, active users, errors, p95.

        ``p95`` uses Postgres' exact ``percentile_cont`` (the DuckDB sibling
        uses ``approx_quantile`` — ``approx_quantile`` does not exist on PG, so
        results may differ within tolerance). The error-rate ratio is left to
        the caller.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT
                      COUNT(*) AS events_total,
                      COUNT(DISTINCT user_id) FILTER (WHERE user_id IS NOT NULL) AS active_users,
                      COUNT(*) FILTER (WHERE result IS NOT NULL AND result LIKE 'error%') AS errors,
                      CAST(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS INTEGER) AS p95
                    FROM audit_log WHERE timestamp >= :since
                    """
                ),
                {"since": since},
            ).fetchone()
        if row is None:
            return {"events_total": 0, "active_users": 0, "errors": 0, "p95": None}
        return {
            "events_total": int(row[0] or 0),
            "active_users": int(row[1] or 0),
            "errors": int(row[2] or 0),
            "p95": int(row[3]) if row[3] is not None else None,
        }


def _json_param(v: Optional[dict]) -> Optional[str]:
    """Serialize dict to JSON text for the ``CAST(:p AS JSONB)`` bind.

    Passing ``None`` through unchanged so the DB stores SQL NULL, not the
    JSON null literal.
    """
    if v is None:
        return None
    import json
    return json.dumps(v)
