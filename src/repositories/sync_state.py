"""Repository for sync state and history."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class SyncStateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_table_state(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM sync_state WHERE table_id = ?", [table_id]
        ).fetchone()
        return self._row_to_dict(result)

    def get_last_sync(self, table_id: str) -> Optional[datetime]:
        result = self.conn.execute(
            "SELECT last_sync FROM sync_state WHERE table_id = ?", [table_id]
        ).fetchone()
        return result[0] if result else None

    def get_all_states(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM sync_state ORDER BY table_id").fetchall()
        return self._rows_to_dicts(results)

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
        self.conn.execute(
            """INSERT INTO sync_state (table_id, last_sync, rows, file_size_bytes,
                uncompressed_size_bytes, columns, hash, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                last_sync = excluded.last_sync,
                rows = excluded.rows,
                file_size_bytes = excluded.file_size_bytes,
                uncompressed_size_bytes = excluded.uncompressed_size_bytes,
                columns = excluded.columns,
                hash = excluded.hash,
                status = excluded.status,
                error = excluded.error""",
            [table_id, now, rows, file_size_bytes, uncompressed_size_bytes,
             columns, hash, status, error],
        )
        self.conn.execute(
            """INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [str(uuid.uuid4()), table_id, now, rows, duration_ms, status, error],
        )

    def get_sync_history(self, table_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM sync_history WHERE table_id = ? ORDER BY synced_at DESC LIMIT ?",
            [table_id, limit],
        ).fetchall()
        return self._rows_to_dicts(results)

    def set_error(self, table_id: str, error_message: str) -> None:
        """Record a per-table sync failure on the existing `error` /`status`
        columns so admin endpoints can surface it (`GET /api/admin/registry`
        joins this column into each row's `last_sync_error`).

        Upserts a sync_state row when one doesn't exist yet (a row that
        errored on its first ever materialize had no prior `update_sync`
        write). `last_sync` is left NULL on first-ever-error so the manifest
        doesn't claim a sync happened. Existing rows keep their last
        successful `last_sync` / `rows` / `hash` fields — only `status` and
        `error` flip — so analysts who already pulled the prior good
        parquet via `da sync` keep serving from it while the operator fixes
        the source.
        """
        self.conn.execute(
            """INSERT INTO sync_state (table_id, status, error)
            VALUES (?, 'error', ?)
            ON CONFLICT (table_id) DO UPDATE SET
                status = 'error',
                error = excluded.error""",
            [table_id, error_message],
        )

    def clear_error(self, table_id: str) -> None:
        """Clear an `error` / `status='error'` flag without disturbing the
        rest of the sync_state row. Called after a successful materialize so
        the registry response stops surfacing stale failure messages.
        Idempotent — silently no-ops on rows that don't exist or already
        have status='ok'.
        """
        self.conn.execute(
            """UPDATE sync_state
            SET status = 'ok', error = ''
            WHERE table_id = ? AND status = 'error'""",
            [table_id],
        )
