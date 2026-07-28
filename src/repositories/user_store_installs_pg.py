"""Postgres-backed user_store_installs repository.

Mirrors ``src/repositories/user_store_installs.py``.
"""
from __future__ import annotations

from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class UserStoreInstallsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def install(self, user_id: str, entity_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "INSERT INTO user_store_installs (user_id, entity_id) "
                    "VALUES (:u, :e) "
                    "ON CONFLICT (user_id, entity_id) DO NOTHING "
                    "RETURNING 1"
                ),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def uninstall(self, user_id: str, entity_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "DELETE FROM user_store_installs "
                    "WHERE user_id = :u AND entity_id = :e RETURNING 1"
                ),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT
                           se.id, se.owner_user_id, se.owner_username, se.type,
                           se.name, se.description, se.category, se.version,
                           se.photo_path, se.video_url, se.file_size,
                           se.install_count, se.created_at, se.updated_at,
                           se.visibility_status,
                           se.title, se.tagline, se.synthetic_name,
                           usi.installed_at
                       FROM user_store_installs usi
                       JOIN store_entities se ON se.id = usi.entity_id
                       WHERE usi.user_id = :u
                         AND se.visibility_status IN ('approved', 'archived')
                       ORDER BY usi.installed_at DESC, se.id"""
                ),
                {"u": user_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def is_installed(self, user_id: str, entity_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM user_store_installs "
                    "WHERE user_id = :u AND entity_id = :e"
                ),
                {"u": user_id, "e": entity_id},
            ).first()
        return row is not None

    def installer_count(self, entity_id: str) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_store_installs WHERE entity_id = :e"
                ),
                {"e": entity_id},
            ).first()
        return int(row[0]) if row else 0

    def delete_all_for_entity(self, entity_id: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM user_store_installs WHERE entity_id = :e RETURNING 1"
                ),
                {"e": entity_id},
            ).all()
        return len(rows)
