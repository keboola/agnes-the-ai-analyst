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


def _json_param(v: Optional[dict]) -> Optional[str]:
    """Serialize dict to JSON text for the ``CAST(:p AS JSONB)`` bind.

    Passing ``None`` through unchanged so the DB stores SQL NULL, not the
    JSON null literal.
    """
    if v is None:
        return None
    import json
    return json.dumps(v)
