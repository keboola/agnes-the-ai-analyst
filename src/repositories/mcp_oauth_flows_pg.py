"""Postgres-backed repository for `mcp_oauth_flows` (v109).

Mirrors ``src/repositories/mcp_oauth_flows.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.secrets_vault import decrypt_optional, encrypt_secret
from src.repositories.mcp_oauth_flows import DEFAULT_TTL_SECONDS


class MCPOAuthFlowPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(self, nonce: str, source_id: str, user_id: str, pkce_verifier: str) -> None:
        now = datetime.now(timezone.utc)
        verifier_enc = encrypt_secret(pkce_verifier)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO mcp_oauth_flows (nonce, source_id, user_id, pkce_verifier_enc, created_at)
                       VALUES (:nonce, :source_id, :user_id, :pkce_verifier_enc, :now)"""
                ),
                {
                    "nonce": nonce,
                    "source_id": source_id,
                    "user_id": user_id,
                    "pkce_verifier_enc": verifier_enc,
                    "now": now,
                },
            )

    def consume(self, nonce: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a flow exactly once — see the DuckDB sibling's
        docstring for the delete-on-read contract. On Postgres this is ALSO
        safe under real concurrent transactions (not just concurrent
        asyncio tasks sharing one connection)."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """DELETE FROM mcp_oauth_flows WHERE nonce = :nonce
                           RETURNING source_id, user_id, pkce_verifier_enc, created_at"""
                    ),
                    {"nonce": nonce},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        pkce_verifier = decrypt_optional(row["pkce_verifier_enc"], context=f"mcp_oauth_flows.pkce_verifier[{nonce}]")
        if pkce_verifier is None:
            return None
        return {
            "nonce": nonce,
            "source_id": row["source_id"],
            "user_id": row["user_id"],
            "pkce_verifier": pkce_verifier,
            "created_at": row["created_at"],
        }

    def sweep_expired(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.text("DELETE FROM mcp_oauth_flows WHERE created_at < :threshold RETURNING nonce"),
                    {"threshold": threshold},
                )
                .mappings()
                .all()
            )
        return len(rows)
