"""Repository for `mcp_user_oauth_tokens` (v109) — per-``(source_id,
user_id)`` OAuth access/refresh token pair for outbound OAuth-authenticated
MCP sources.

Kept separate from ``mcp_user_secrets``: different lifecycle (server-side
refresh mutates rows here; ``mcp_user_secrets`` is write-only from the
user) and different deletion semantics (best-effort revoke-at-AS on
disconnect, not covered here — this repo only persists/reads).

``access_token`` is mandatory and always encrypted; an undecryptable row
(vault key rotated) is treated as absent by ``get()`` — same fail-closed
contract as ``app.secrets_vault.PerUserSecretsRepository``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb

from app.secrets_vault import decrypt_optional, encrypt_secret


class MCPUserOAuthTokenRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

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
        self.conn.execute(
            """INSERT INTO mcp_user_oauth_tokens
               (source_id, user_id, access_token_enc, refresh_token_enc, expires_at, scopes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source_id, user_id) DO UPDATE SET
                   access_token_enc  = excluded.access_token_enc,
                   refresh_token_enc = excluded.refresh_token_enc,
                   expires_at        = excluded.expires_at,
                   scopes            = excluded.scopes,
                   updated_at        = excluded.updated_at""",
            [source_id, user_id, access_enc, refresh_enc, expires_at, scopes, now, now],
        )

    def get(self, source_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Decrypted row for ``(source_id, user_id)``, or ``None`` when absent
        OR when ``access_token`` fails to decrypt (vault key rotated) — the
        latter is treated exactly like "not connected", matching the
        fail-closed rule for per-user credentials."""
        row = self.conn.execute(
            """SELECT source_id, user_id, access_token_enc, refresh_token_enc,
                      expires_at, scopes, created_at, updated_at
               FROM mcp_user_oauth_tokens WHERE source_id = ? AND user_id = ?""",
            [source_id, user_id],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        d = dict(zip(cols, row))
        access_enc = d.pop("access_token_enc")
        refresh_enc = d.pop("refresh_token_enc")
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
        self.conn.execute(
            "DELETE FROM mcp_user_oauth_tokens WHERE source_id = ? AND user_id = ?",
            [source_id, user_id],
        )

    def delete_for_source(self, source_id: str) -> int:
        """Drop every user's tokens for ``source_id`` (source-delete cascade).
        Returns the deleted-row count."""
        rows = self.conn.execute(
            "DELETE FROM mcp_user_oauth_tokens WHERE source_id = ? RETURNING user_id",
            [source_id],
        ).fetchall()
        return len(rows)

    def has(self, source_id: str, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mcp_user_oauth_tokens WHERE source_id = ? AND user_id = ? LIMIT 1",
            [source_id, user_id],
        ).fetchone()
        return row is not None
