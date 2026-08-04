"""Postgres-backed repository for `mcp_source_oauth_clients` (v109).

Mirrors ``src/repositories/mcp_source_oauth_clients.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.secrets_vault import decrypt_optional, encrypt_secret
from src.repositories.mcp_source_oauth_clients import KEEP_STORED, _Keep  # noqa: F401  # KEEP_STORED re-exported for PG callers


class MCPSourceOAuthClientPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert(
        self,
        source_id: str,
        *,
        issuer: str,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
        client_secret: Optional[str] = None,
        registration_access_token: Any = None,
        scopes: Optional[str] = None,
    ) -> None:
        """Mirrors the DuckDB sibling, including :data:`KEEP_STORED` for
        ``registration_access_token`` — see its docstring for why a decrypted
        value cannot round-trip an unreadable column."""
        now = datetime.now(timezone.utc)
        secret_enc = encrypt_secret(client_secret) if client_secret else None
        keep_rat = isinstance(registration_access_token, _Keep)
        rat_enc = (
            None if keep_rat else (encrypt_secret(registration_access_token) if registration_access_token else None)
        )
        # Table-qualified: a bare column name on the right of DO UPDATE SET is
        # ambiguous in Postgres (it could be the excluded row's).
        rat_update = (
            "mcp_source_oauth_clients.registration_access_token_enc"
            if keep_rat
            else "EXCLUDED.registration_access_token_enc"
        )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"""INSERT INTO mcp_source_oauth_clients
                           (source_id, issuer, client_id, client_secret_enc,
                            registration_access_token_enc, authorization_endpoint,
                            token_endpoint, scopes, created_at, updated_at)
                       VALUES (:source_id, :issuer, :client_id, :client_secret_enc,
                               :registration_access_token_enc, :authorization_endpoint,
                               :token_endpoint, :scopes, :now, :now)
                       ON CONFLICT (source_id) DO UPDATE SET
                           issuer                         = EXCLUDED.issuer,
                           client_id                      = EXCLUDED.client_id,
                           client_secret_enc              = EXCLUDED.client_secret_enc,
                           registration_access_token_enc  = {rat_update},
                           authorization_endpoint         = EXCLUDED.authorization_endpoint,
                           token_endpoint                 = EXCLUDED.token_endpoint,
                           scopes                         = EXCLUDED.scopes,
                           updated_at                     = EXCLUDED.updated_at"""
                ),
                {
                    "source_id": source_id,
                    "issuer": issuer,
                    "client_id": client_id,
                    "client_secret_enc": secret_enc,
                    "registration_access_token_enc": rat_enc,
                    "authorization_endpoint": authorization_endpoint,
                    "token_endpoint": token_endpoint,
                    "scopes": scopes,
                    "now": now,
                },
            )

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM mcp_source_oauth_clients WHERE source_id = :source_id"),
                    {"source_id": source_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        d = dict(row)
        secret_enc = d.pop("client_secret_enc", None)
        rat_enc = d.pop("registration_access_token_enc", None)
        # Ciphertext PRESENCE, independent of whether the current vault key can
        # still open it. `client_secret` alone cannot tell "no secret" from
        # "secret we can no longer read" (Devin Review on #1124).
        d["client_secret_present"] = secret_enc is not None
        d["registration_access_token_present"] = rat_enc is not None
        d["client_secret"] = decrypt_optional(
            secret_enc, context=f"mcp_source_oauth_clients.client_secret[{source_id}]"
        )
        d["registration_access_token"] = decrypt_optional(
            rat_enc, context=f"mcp_source_oauth_clients.registration_access_token[{source_id}]"
        )
        return d

    def delete(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM mcp_source_oauth_clients WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
