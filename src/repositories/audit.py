"""Repository for audit logging."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class AuditRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

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
        """Insert one audit_log row. Returns the new row id.

        The four kwargs after `*` are v40 additions; legacy callers using
        positional args or the original kwargs are unaffected. `params_before`
        is only used for mutating actions where rollback / diff is meaningful;
        leave None for reads, ticks, queries.
        """
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO audit_log
               (id, timestamp, user_id, action, resource, params, result, duration_ms,
                params_before, client_ip, client_kind, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                entry_id, now, user_id, action, resource,
                json.dumps(params) if params else None,
                result, duration_ms,
                json.dumps(params_before) if params_before else None,
                client_ip, client_kind, correlation_id,
            ],
        )
        return entry_id

    def query(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: List[Any] = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        results = self.conn.execute(sql, params).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def query_actions(
        self,
        actions: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return rows whose action is in the given list, newest first."""
        if not actions:
            return []
        placeholders = ",".join("?" for _ in actions)
        sql = f"SELECT * FROM audit_log WHERE action IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?"
        results = self.conn.execute(sql, list(actions) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def query_for_resources(
        self,
        resources: List[str],
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Activity timeline for one or more resource refs.

        Each ``resources`` entry is a full ``resource`` value (e.g.
        ``"store_submission:abc123"``, ``"store_entity:def456"``). Used
        by the submission-detail page to render *"when did each rescan /
        override / approval happen, and who did it"* — proves that the
        latest verdict on the row is fresh and not a stale render.
        """
        if not resources:
            return []
        placeholders = ",".join("?" for _ in resources)
        sql = (
            f"SELECT * FROM audit_log WHERE resource IN ({placeholders}) "
            f"ORDER BY timestamp DESC LIMIT ?"
        )
        results = self.conn.execute(sql, list(resources) + [limit]).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        rows: List[Dict[str, Any]] = []
        for row in results:
            d = dict(zip(columns, row))
            v = d.get("params")
            if isinstance(v, str):
                try:
                    d["params"] = json.loads(v) if v else None
                except (ValueError, TypeError):
                    pass
            rows.append(d)
        return rows
