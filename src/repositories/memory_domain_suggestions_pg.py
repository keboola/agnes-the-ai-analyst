"""Postgres-backed repository for ``memory_domain_suggestions`` (v55).

Mirrors ``src/repositories/memory_domain_suggestions.py`` (the DuckDB
impl) on the ``MemoryDomainSuggestionsRepository`` public surface.
Cross-engine parity will be covered by Task 1D.3's contract test.

No JSONB columns on this entity — the DDL is plain scalars. ID prefix
matches the DuckDB sibling (``sug_<uuid12>``), so cross-engine
fixtures don't have to special-case suggestion ids.

Status transitions: ``resolve()`` is a guarded UPDATE — only flips a
row that's currently ``status='pending'``. Re-resolving an already
resolved row returns ``False`` (no rowcount), matching the DuckDB
sibling's behavior.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MemoryDomainSuggestionsPgRepository:
    """Postgres twin of ``MemoryDomainSuggestionsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        rationale: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> str:
        """Insert a new ``status='pending'`` suggestion; returns the id."""
        sid = f"sug_{uuid.uuid4().hex[:12]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO memory_domain_suggestions
                      (id, name, description, rationale, status, created_by)
                    VALUES
                      (:id, :name, :description, :rationale, 'pending',
                       :created_by)
                    """
                ),
                {
                    "id": sid,
                    "name": name,
                    "description": description,
                    "rationale": rationale,
                    "created_by": created_by,
                },
            )
        return sid

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        """Fetch a single suggestion by id. Includes resolved rows."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT * FROM memory_domain_suggestions WHERE id = :id"
                ),
                {"id": sid},
            ).mappings().first()
        return dict(row) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List suggestions newest-first with optional status / requester
        filtering. Resolved rows are NOT hidden — admin queue + requester
        history both consume the same list."""
        query = "SELECT * FROM memory_domain_suggestions"
        where: List[str] = []
        params: Dict[str, Any] = {}
        if status is not None:
            where.append("status = :status")
            params["status"] = status
        if created_by is not None:
            where.append("created_by = :created_by")
            params["created_by"] = created_by
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [dict(r) for r in rows]

    def count_pending(self) -> int:
        """Admin-queue badge count."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM memory_domain_suggestions "
                    "WHERE status = 'pending'"
                )
            ).first()
        return int(row[0]) if row else 0

    def resolve(
        self,
        sid: str,
        *,
        status: str,
        resolved_by: Optional[str],
        resolution_note: Optional[str] = None,
        created_domain_id: Optional[str] = None,
    ) -> bool:
        """Flip a pending suggestion to ``approved`` / ``rejected``.

        Guarded by ``WHERE status = 'pending'`` so a double-resolve from
        two admins is a no-op rather than overwriting the first verdict.
        Returns True iff a row was updated (mirrors the DuckDB sibling).
        """
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    """
                    UPDATE memory_domain_suggestions
                       SET status = :status,
                           resolved_at = CURRENT_TIMESTAMP,
                           resolved_by = :resolved_by,
                           resolution_note = :resolution_note,
                           created_domain_id = :created_domain_id
                     WHERE id = :id
                       AND status = 'pending'
                    """
                ),
                {
                    "status": status,
                    "resolved_by": resolved_by,
                    "resolution_note": resolution_note,
                    "created_domain_id": created_domain_id,
                    "id": sid,
                },
            )
        return (result.rowcount or 0) > 0
