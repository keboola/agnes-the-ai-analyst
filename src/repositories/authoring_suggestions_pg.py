"""Postgres-backed repository for ``authoring_suggestions`` (v77).

Mirrors ``src/repositories/authoring_suggestions.py`` (the DuckDB impl) on the
``AuthoringSuggestionsRepository`` public surface. Cross-engine parity is
covered by ``tests/db_pg/test_authoring_suggestions_contract.py``.

The ``payload`` column is JSON; we serialize on write and parse on read so the
public surface returns a Python dict on both engines. ID prefix matches the
DuckDB sibling (``asug_<uuid12>``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_COLS = (
    "id, domain, payload, status, created_by, created_at, "
    "resolved_at, resolved_by, resolution_note, created_resource_id"
)


class AuthoringSuggestionsPgRepository:
    """Postgres twin of ``AuthoringSuggestionsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        *,
        domain: str,
        payload: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> str:
        sid = f"asug_{uuid.uuid4().hex[:12]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO authoring_suggestions "
                    "(id, domain, payload, status, created_by) "
                    "VALUES (:id, :domain, :payload, 'pending', :created_by)"
                ),
                {
                    "id": sid,
                    "domain": domain,
                    "payload": json.dumps(payload),
                    "created_by": created_by,
                },
            )
        return sid

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT {_COLS} FROM authoring_suggestions WHERE id = :id"),
                {"id": sid},
            ).fetchone()
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
        params: Dict[str, Any] = {"limit": limit}
        if status is not None:
            where.append("status = :status")
            params["status"] = status
        if domain is not None:
            where.append("domain = :domain")
            params["domain"] = domain
        if created_by is not None:
            where.append("created_by = :created_by")
            params["created_by"] = created_by
        sql = f"SELECT {_COLS} FROM authoring_suggestions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(sa.text("SELECT COUNT(*) FROM authoring_suggestions WHERE status = 'pending'")).scalar()
                or 0
            )

    def resolve(
        self,
        sid: str,
        *,
        status: str,
        resolved_by: Optional[str],
        resolution_note: Optional[str] = None,
        created_resource_id: Optional[str] = None,
    ) -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        with self._engine.begin() as conn:
            was_pending = (
                conn.execute(
                    sa.text("SELECT 1 FROM authoring_suggestions WHERE id = :id AND status = 'pending'"),
                    {"id": sid},
                ).fetchone()
                is not None
            )
            conn.execute(
                sa.text(
                    "UPDATE authoring_suggestions "
                    "SET status = :status, resolved_at = CURRENT_TIMESTAMP, "
                    "    resolved_by = :resolved_by, resolution_note = :note, "
                    "    created_resource_id = :rid "
                    "WHERE id = :id AND status = 'pending'"
                ),
                {
                    "status": status,
                    "resolved_by": resolved_by,
                    "note": resolution_note,
                    "rid": created_resource_id,
                    "id": sid,
                },
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
