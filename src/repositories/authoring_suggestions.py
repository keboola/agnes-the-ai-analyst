"""Repository for ``authoring_suggestions`` (v77).

Generic non-admin suggestion queue for the authoring studio (data-package /
mcp / marketplace / corporate-memory). A non-admin submits a proposed create
``payload`` (``status='pending'``); an admin approves — replaying the payload
through the real endpoint and stamping ``status='approved'`` +
``created_resource_id`` — or rejects. Generalizes ``memory_domain_suggestions``
across all studio domains. Resolved rows are retained for audit.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import duckdb

_COLS = (
    "id, domain, payload, status, created_by, created_at, "
    "resolved_at, resolved_by, resolution_note, created_resource_id"
)


class AuthoringSuggestionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        domain: str,
        payload: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> str:
        sid = f"asug_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO authoring_suggestions(id, domain, payload, status, created_by) VALUES (?, ?, ?, 'pending', ?)",
            [sid, domain, json.dumps(payload), created_by],
        )
        return sid

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {_COLS} FROM authoring_suggestions WHERE id = ?", [sid]).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        domain: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if domain is not None:
            where.append("domain = ?")
            params.append(domain)
        if created_by is not None:
            where.append("created_by = ?")
            params.append(created_by)
        sql = f"SELECT {_COLS} FROM authoring_suggestions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM authoring_suggestions WHERE status = 'pending'").fetchone()[0]

    def resolve(
        self,
        sid: str,
        *,
        status: str,  # 'approved' or 'rejected'
        resolved_by: Optional[str],
        resolution_note: Optional[str] = None,
        created_resource_id: Optional[str] = None,
    ) -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        was_pending = (
            self.conn.execute(
                "SELECT 1 FROM authoring_suggestions WHERE id = ? AND status = 'pending'",
                [sid],
            ).fetchone()
            is not None
        )
        self.conn.execute(
            "UPDATE authoring_suggestions "
            "SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
            "    resolved_by = ?, resolution_note = ?, created_resource_id = ? "
            "WHERE id = ? AND status = 'pending'",
            [status, resolved_by, resolution_note, created_resource_id, sid],
        )
        return was_pending

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        keys = (
            "id",
            "domain",
            "payload",
            "status",
            "created_by",
            "created_at",
            "resolved_at",
            "resolved_by",
            "resolution_note",
            "created_resource_id",
        )
        d = dict(zip(keys, row))
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except (ValueError, TypeError):
                pass
        return d
