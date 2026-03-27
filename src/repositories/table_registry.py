"""Repository for table registry."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class TableRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def register(
        self, id: str, name: str, folder: Optional[str] = None,
        sync_strategy: Optional[str] = None, primary_key: Optional[str] = None,
        description: Optional[str] = None, registered_by: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO table_registry (id, name, folder, sync_strategy,
                primary_key, description, registered_by, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy, primary_key = excluded.primary_key,
                description = excluded.description, registered_at = excluded.registered_at""",
            [id, name, folder, sync_strategy, primary_key, description, registered_by, now],
        )

    def unregister(self, table_id: str) -> None:
        self.conn.execute("DELETE FROM table_registry WHERE id = ?", [table_id])

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM table_registry WHERE id = ?", [table_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM table_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
