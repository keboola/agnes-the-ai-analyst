"""Repository for user sync settings and dataset permissions."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class SyncSettingsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get_user_settings(self, user_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM user_sync_settings WHERE user_id = ? ORDER BY dataset",
            [user_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def set_dataset_enabled(self, user_id: str, dataset: str, enabled: bool) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO user_sync_settings (user_id, dataset, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, dataset) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
            [user_id, dataset, enabled, now],
        )

    def is_dataset_enabled(self, user_id: str, dataset: str) -> bool:
        result = self.conn.execute(
            "SELECT enabled FROM user_sync_settings WHERE user_id = ? AND dataset = ?",
            [user_id, dataset],
        ).fetchone()
        return bool(result and result[0])

    def get_enabled_datasets(self, user_id: str) -> List[str]:
        results = self.conn.execute(
            "SELECT dataset FROM user_sync_settings WHERE user_id = ? AND enabled = true",
            [user_id],
        ).fetchall()
        return [r[0] for r in results]


class DatasetPermissionRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def grant(self, user_id: str, dataset: str, access: str = "read") -> None:
        self.conn.execute(
            """INSERT INTO dataset_permissions (user_id, dataset, access)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id, dataset) DO UPDATE SET access = excluded.access""",
            [user_id, dataset, access],
        )

    def revoke(self, user_id: str, dataset: str) -> None:
        self.conn.execute(
            "DELETE FROM dataset_permissions WHERE user_id = ? AND dataset = ?",
            [user_id, dataset],
        )

    def has_access(self, user_id: str, dataset: str) -> bool:
        result = self.conn.execute(
            "SELECT access FROM dataset_permissions WHERE user_id = ? AND dataset = ?",
            [user_id, dataset],
        ).fetchone()
        return result is not None and result[0] != "none"

    def get_user_permissions(self, user_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM dataset_permissions WHERE user_id = ? ORDER BY dataset",
            [user_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def get_accessible_datasets(self, user_id: str) -> List[str]:
        results = self.conn.execute(
            "SELECT dataset FROM dataset_permissions WHERE user_id = ? AND access != 'none'",
            [user_id],
        ).fetchall()
        return [r[0] for r in results]
