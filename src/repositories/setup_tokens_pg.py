"""Postgres-backed SetupTokenRepository.

Mirrors ``src/repositories/setup_tokens.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SetupTokenPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        id: str,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO setup_tokens
                       (id, user_id, token_hash, expires_at, created_at)
                       VALUES (:id, :user_id, :token_hash, :expires_at, :now)"""
                ),
                {
                    "id": id,
                    "user_id": user_id,
                    "token_hash": token_hash,
                    "expires_at": expires_at,
                    "now": datetime.now(timezone.utc),
                },
            )

    def get_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM setup_tokens WHERE token_hash = :hash"),
                {"hash": token_hash},
            ).mappings().first()
        return dict(row) if row else None

    def mark_used(self, token_id: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE setup_tokens SET used_at = :now "
                    "WHERE id = :id AND used_at IS NULL "
                    "RETURNING id"
                ),
                {"now": datetime.now(timezone.utc), "id": token_id},
            )
        return result.rowcount > 0

    def list_active_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT * FROM setup_tokens
                       WHERE user_id = :uid
                         AND used_at IS NULL
                         AND expires_at > :now
                       ORDER BY created_at DESC"""
                ),
                {"uid": user_id, "now": now},
            ).mappings().all()
        return [dict(r) for r in rows]

    def count_active_for_user(self, user_id: str) -> int:
        now = datetime.now(timezone.utc)
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT COUNT(*) FROM setup_tokens
                       WHERE user_id = :uid
                         AND used_at IS NULL
                         AND expires_at > :now"""
                ),
                {"uid": user_id, "now": now},
            ).scalar()
        return row or 0

    def delete(self, token_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM setup_tokens WHERE id = :id"),
                {"id": token_id},
            )

    def delete_expired(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM setup_tokens WHERE expires_at < :cutoff"),
                {"cutoff": cutoff},
            )
        return result.rowcount or 0
