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
        source_type: Optional[str] = None, bucket: Optional[str] = None,
        source_table: Optional[str] = None, source_query: Optional[str] = None,
        query_mode: str = "local",
        sync_schedule: Optional[str] = None, profile_after_sync: bool = True,
        is_public: bool = True,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO table_registry (id, name, folder, sync_strategy,
                primary_key, description, registered_by, registered_at,
                source_type, bucket, source_table, source_query, query_mode,
                sync_schedule, profile_after_sync, is_public)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy, primary_key = excluded.primary_key,
                description = excluded.description, registered_at = excluded.registered_at,
                source_type = excluded.source_type, bucket = excluded.bucket,
                source_table = excluded.source_table, source_query = excluded.source_query,
                query_mode = excluded.query_mode,
                sync_schedule = excluded.sync_schedule, profile_after_sync = excluded.profile_after_sync,
                is_public = excluded.is_public""",
            [id, name, folder, sync_strategy, primary_key, description, registered_by, now,
             source_type, bucket, source_table, source_query, query_mode,
             sync_schedule, profile_after_sync, is_public],
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

    def list_by_source(self, source_type: str) -> List[Dict[str, Any]]:
        """List tables for a given source type (keboola, bigquery, jira, etc.)."""
        results = self.conn.execute(
            "SELECT * FROM table_registry WHERE source_type = ? ORDER BY name",
            [source_type],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_local(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tables with query_mode='local' (data downloaded to parquet)."""
        if source_type:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' AND source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' ORDER BY name",
            ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]
