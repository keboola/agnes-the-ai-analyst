"""Postgres-backed personal_access_tokens repository.

Mirrors ``src/repositories/access_tokens.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class AccessTokenPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        id: str,
        user_id: str,
        name: str,
        token_hash: str,
        prefix: str,
        expires_at: Optional[datetime] = None,
        scopes: Optional[str] = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO personal_access_tokens
                       (id, user_id, name, token_hash, prefix, scopes, created_at, expires_at)
                       VALUES (:id, :user_id, :name, :token_hash, :prefix, :scopes, :now, :expires_at)"""
                ),
                {
                    "id": id,
                    "user_id": user_id,
                    "name": name,
                    "token_hash": token_hash,
                    "prefix": prefix,
                    "scopes": scopes,
                    "now": datetime.now(timezone.utc),
                    "expires_at": expires_at,
                },
            )

    def get_by_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM personal_access_tokens WHERE id = :id"),
                {"id": token_id},
            ).mappings().first()
        return dict(row) if row else None

    def list_for_user(self, user_id: str, include_revoked: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM personal_access_tokens WHERE user_id = :uid"
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"uid": user_id}).mappings().all()
        return [dict(r) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM personal_access_tokens ORDER BY created_at DESC")
            ).mappings().all()
        return [dict(r) for r in rows]

    def list_all_with_user(self, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT t.*, u.email AS user_email
                       FROM personal_access_tokens t
                       LEFT JOIN users u ON u.id = t.user_id
                       ORDER BY t.created_at DESC
                       LIMIT :limit OFFSET :offset"""
                ),
                {"limit": limit, "offset": offset},
            ).mappings().all()
        return [dict(r) for r in rows]

    def revoke(self, token_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE personal_access_tokens SET revoked_at = :now WHERE id = :id"
                ),
                {"now": datetime.now(timezone.utc), "id": token_id},
            )

    def delete(self, token_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM personal_access_tokens WHERE id = :id"),
                {"id": token_id},
            )

    def mark_used(self, token_id: str, ip: Optional[str] = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE personal_access_tokens SET last_used_at = :now, last_used_ip = :ip WHERE id = :id"
                ),
                {"now": datetime.now(timezone.utc), "ip": ip, "id": token_id},
            )
