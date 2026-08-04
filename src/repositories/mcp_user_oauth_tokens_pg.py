"""Postgres-backed repository for `mcp_user_oauth_tokens` (v109).

Mirrors ``src/repositories/mcp_user_oauth_tokens.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.secrets_vault import decrypt_optional, encrypt_secret


class MCPUserOAuthTokenPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(
        self,
        source_id: str,
        user_id: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        scopes: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        access_enc = encrypt_secret(access_token)
        refresh_enc = encrypt_secret(refresh_token) if refresh_token else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO mcp_user_oauth_tokens
                           (source_id, user_id, access_token_enc, refresh_token_enc, expires_at, scopes, created_at, updated_at)
                       VALUES (:source_id, :user_id, :access_token_enc, :refresh_token_enc, :expires_at, :scopes, :now, :now)
                       ON CONFLICT (source_id, user_id) DO UPDATE SET
                           access_token_enc  = EXCLUDED.access_token_enc,
                           refresh_token_enc = EXCLUDED.refresh_token_enc,
                           expires_at        = EXCLUDED.expires_at,
                           scopes            = EXCLUDED.scopes,
                           updated_at        = EXCLUDED.updated_at"""
                ),
                {
                    "source_id": source_id,
                    "user_id": user_id,
                    "access_token_enc": access_enc,
                    "refresh_token_enc": refresh_enc,
                    "expires_at": expires_at,
                    "scopes": scopes,
                    "now": now,
                },
            )

    def get(self, source_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM mcp_user_oauth_tokens WHERE source_id = :source_id AND user_id = :user_id"),
                    {"source_id": source_id, "user_id": user_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        d = dict(row)
        access_enc = d.pop("access_token_enc", None)
        refresh_enc = d.pop("refresh_token_enc", None)
        access_token = decrypt_optional(
            access_enc, context=f"mcp_user_oauth_tokens.access_token[{source_id}/{user_id}]"
        )
        if access_token is None:
            return None
        d["access_token"] = access_token
        d["refresh_token"] = decrypt_optional(
            refresh_enc, context=f"mcp_user_oauth_tokens.refresh_token[{source_id}/{user_id}]"
        )
        return d

    def delete(self, source_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mcp_user_oauth_tokens WHERE source_id = :source_id AND user_id = :user_id"),
                {"source_id": source_id, "user_id": user_id},
            )

    def delete_for_source(self, source_id: str) -> int:
        """Drop every user's tokens for ``source_id`` (source-delete cascade).
        Returns the deleted-row count."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM mcp_user_oauth_tokens WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
            return result.rowcount or 0

    def has(self, source_id: str, user_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM mcp_user_oauth_tokens WHERE source_id = :source_id AND user_id = :user_id LIMIT 1"
                ),
                {"source_id": source_id, "user_id": user_id},
            ).fetchone()
        return row is not None
