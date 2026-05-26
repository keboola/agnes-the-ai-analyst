"""Postgres-backed sync state + history repository.

Mirrors ``src/repositories/sync_state.py``. The upsert in
``update_sync`` uses PG's ``ON CONFLICT (table_id) DO UPDATE`` so the
DuckDB and PG impls converge on identical semantics.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SyncStatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_table_state(self, table_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM sync_state WHERE table_id = :t"),
                {"t": table_id},
            ).mappings().first()
        return dict(row) if row else None

    def get_last_sync(self, table_id: str) -> Optional[datetime]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT last_sync FROM sync_state WHERE table_id = :t"),
                {"t": table_id},
            ).first()
        return row[0] if row else None

    def get_all_states(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM sync_state ORDER BY table_id")
            ).mappings().all()
        return [dict(r) for r in rows]

    def update_sync(
        self,
        table_id: str,
        rows: int,
        file_size_bytes: int,
        hash: str,
        uncompressed_size_bytes: int = 0,
        columns: int = 0,
        status: str = "ok",
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO sync_state
                       (table_id, last_sync, rows, file_size_bytes,
                        uncompressed_size_bytes, columns, hash, status, error)
                       VALUES (:table_id, :now, :rows, :fsb, :usb, :cols, :hash, :status, :error)
                       ON CONFLICT (table_id) DO UPDATE SET
                         last_sync = EXCLUDED.last_sync,
                         rows = EXCLUDED.rows,
                         file_size_bytes = EXCLUDED.file_size_bytes,
                         uncompressed_size_bytes = EXCLUDED.uncompressed_size_bytes,
                         columns = EXCLUDED.columns,
                         hash = EXCLUDED.hash,
                         status = EXCLUDED.status,
                         error = EXCLUDED.error"""
                ),
                {
                    "table_id": table_id,
                    "now": now,
                    "rows": rows,
                    "fsb": file_size_bytes,
                    "usb": uncompressed_size_bytes,
                    "cols": columns,
                    "hash": hash,
                    "status": status,
                    "error": error,
                },
            )
            conn.execute(
                sa.text(
                    """INSERT INTO sync_history
                       (id, table_id, synced_at, rows, duration_ms, status, error)
                       VALUES (:id, :table_id, :now, :rows, :dms, :status, :error)"""
                ),
                {
                    "id": str(uuid.uuid4()),
                    "table_id": table_id,
                    "now": now,
                    "rows": rows,
                    "dms": duration_ms,
                    "status": status,
                    "error": error,
                },
            )

    def get_sync_history(self, table_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT * FROM sync_history WHERE table_id = :t
                       ORDER BY synced_at DESC LIMIT :limit"""
                ),
                {"t": table_id, "limit": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    def list_recent(
        self,
        *,
        since: datetime,
        limit: int = 100,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sync_history WHERE synced_at >= :since"
        params: Dict[str, Any] = {"since": since, "limit": limit}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY synced_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def set_error(self, table_id: str, error_message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO sync_state (table_id, status, error)
                       VALUES (:t, 'error', :err)
                       ON CONFLICT (table_id) DO UPDATE SET
                         status = 'error',
                         error = EXCLUDED.error"""
                ),
                {"t": table_id, "err": error_message},
            )

    def clear_error(self, table_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE sync_state SET status = 'ok', error = ''
                       WHERE table_id = :t AND status = 'error'"""
                ),
                {"t": table_id},
            )

    def delete_for_table(self, table_id: str) -> None:
        """Drop the sync_state + sync_history rows for one table.

        Called by ``DELETE /api/admin/registry/{table_id}`` so an
        unregistered table stops surfacing in the manifest served to
        ``agnes pull``. Idempotent — missing rows are a no-op.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM sync_state WHERE table_id = :t"),
                {"t": table_id},
            )
            conn.execute(
                sa.text("DELETE FROM sync_history WHERE table_id = :t"),
                {"t": table_id},
            )
