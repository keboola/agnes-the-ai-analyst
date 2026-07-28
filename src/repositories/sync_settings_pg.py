"""Postgres-backed user sync settings repository.

Mirrors ``src/repositories/sync_settings.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SyncSettingsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_user_settings(self, user_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM user_sync_settings WHERE user_id = :u ORDER BY dataset"
                ),
                {"u": user_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def set_dataset_enabled(self, user_id: str, dataset: str, enabled: bool) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO user_sync_settings (user_id, dataset, enabled, updated_at)
                       VALUES (:u, :d, :e, :now)
                       ON CONFLICT (user_id, dataset) DO UPDATE SET
                         enabled = EXCLUDED.enabled,
                         updated_at = EXCLUDED.updated_at"""
                ),
                {"u": user_id, "d": dataset, "e": enabled, "now": now},
            )

    def is_dataset_enabled(self, user_id: str, dataset: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT enabled FROM user_sync_settings WHERE user_id = :u AND dataset = :d"
                ),
                {"u": user_id, "d": dataset},
            ).first()
        return bool(row and row[0])

    def get_enabled_datasets(self, user_id: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT dataset FROM user_sync_settings WHERE user_id = :u AND enabled = TRUE"
                ),
                {"u": user_id},
            ).all()
        return [r[0] for r in rows]
