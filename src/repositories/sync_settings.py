"""Repository for user sync settings.

``DatasetPermissionRepository`` was removed in v19 — table access is now
exclusively via ``resource_grants(resource_type='table')`` (see
``app.auth.access.can_access`` and ``src/rbac.py``).
"""

from datetime import datetime, timezone
from typing import Any, List, Dict

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
