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
    ) -> str:
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO audit_log (id, timestamp, user_id, action, resource, params, result, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [entry_id, now, user_id, action, resource,
             json.dumps(params) if params else None, result, duration_ms],
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
