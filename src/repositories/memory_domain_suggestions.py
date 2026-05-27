"""Repository for ``memory_domain_suggestions`` (v55).

Non-admin users can suggest a new Memory Domain from the
/corporate-memory empty state. Suggestions land in ``status='pending'``
and surface on the admin moderation queue with one-click approve
(creates the real ``memory_domains`` row + stamps the suggestion
``status='approved'``, ``created_domain_id``) or reject.

Resolved rows are retained for audit so the requester sees the
disposition; never hard-deleted by the API.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import duckdb


_COLS = (
    "id, name, description, rationale, status, created_by, created_at, "
    "resolved_at, resolved_by, resolution_note, created_domain_id"
)


class MemoryDomainSuggestionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def create(
        self, *,
        name: str,
        description: Optional[str] = None,
        rationale: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> str:
        sid = f"sug_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO memory_domain_suggestions(id, name, description, "
            "rationale, status, created_by) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            [sid, name, description, rationale, created_by],
        )
        return sid

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM memory_domain_suggestions WHERE id = ?",
            [sid],
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self, *,
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if status is not None:
            where.append("status = ?"); params.append(status)
        if created_by is not None:
            where.append("created_by = ?"); params.append(created_by)
        sql = f"SELECT {_COLS} FROM memory_domain_suggestions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM memory_domain_suggestions WHERE status = 'pending'"
        ).fetchone()[0]

    def resolve(
        self, sid: str, *,
        status: str,  # 'approved' or 'rejected'
        resolved_by: Optional[str],
        resolution_note: Optional[str] = None,
        created_domain_id: Optional[str] = None,
    ) -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        # DuckDB's Python API returns ``-1`` from ``cur.rowcount`` for UPDATE
        # statements regardless of whether any row matched, so we can't use
        # rowcount to detect the guard miss. Instead, pre-check that the row
        # is still ``pending`` inside the same statement window.
        was_pending = self.conn.execute(
            "SELECT 1 FROM memory_domain_suggestions "
            "WHERE id = ? AND status = 'pending'",
            [sid],
        ).fetchone() is not None
        self.conn.execute(
            "UPDATE memory_domain_suggestions "
            "SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
            "    resolved_by = ?, resolution_note = ?, created_domain_id = ? "
            "WHERE id = ? AND status = 'pending'",
            [status, resolved_by, resolution_note, created_domain_id, sid],
        )
        return was_pending

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        keys = (
            "id", "name", "description", "rationale", "status",
            "created_by", "created_at",
            "resolved_at", "resolved_by", "resolution_note",
            "created_domain_id",
        )
        return dict(zip(keys, row))
