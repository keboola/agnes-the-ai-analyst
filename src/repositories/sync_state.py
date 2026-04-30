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

    def list_history(
        self,
        limit: int = 50,
        table_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """All sync_history rows, newest first. Powers the /admin/sync
        recent-runs table. Optional ``table_id`` filter narrows to a
        single table; without it the result spans every source / table."""
        if table_id:
            sql = (
                "SELECT * FROM sync_history WHERE table_id = ? "
                "ORDER BY synced_at DESC LIMIT ?"
            )
            params: list = [table_id, limit]
        else:
            sql = "SELECT * FROM sync_history ORDER BY synced_at DESC LIMIT ?"
            params = [limit]
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)
