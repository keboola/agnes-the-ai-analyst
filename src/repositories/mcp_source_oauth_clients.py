"""Repository for `mcp_source_oauth_clients` (v109) — Agnes's own OAuth
client registration (RFC 7591 dynamic client registration, or a manually
configured client) at an upstream MCP source's authorization server.

One row per OAuth-``auth_method`` ``mcp_sources`` row. Distinct from the
inbound issuer's ``oauth_clients`` table (v82) — mirror-image concept,
opposite direction: this table is Agnes acting as an OAuth *client*, that
one is Agnes acting as an OAuth *authorization server*.

``client_secret`` and ``registration_access_token`` are optional (public
PKCE-only clients register with neither) and Fernet-encrypted at rest via
``app.secrets_vault`` — the same vault key as ``mcp_secrets`` /
``mcp_user_secrets``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb

from app.secrets_vault import decrypt_optional, encrypt_secret


class _Keep:
    """Sentinel for "leave this encrypted column exactly as stored"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "KEEP_STORED"


#: Pass as ``registration_access_token`` to preserve the stored ciphertext.
#:
#: Needed because a decrypted value cannot round-trip a column the current
#: vault key can no longer open: ``get()`` hands back ``None`` for both "no
#: token" and "token we can't read", so writing that ``None`` back destroys
#: still-valid ciphertext and permanently disables upstream deregistration.
#: Callers use it together with ``registration_access_token_present``
#: (Devin Review on #1124).
KEEP_STORED = _Keep()


class MCPSourceOAuthClientRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

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
        """Insert or replace the OAuth client registration for ``source_id``.

        Idempotent re-registration: a second call for the same ``source_id``
        (e.g. after DCR rotation) replaces the row wholesale. Pass
        :data:`KEEP_STORED` for ``registration_access_token`` to leave that
        one column untouched.
        """
        now = datetime.now(timezone.utc)
        secret_enc = encrypt_secret(client_secret) if client_secret else None
        keep_rat = isinstance(registration_access_token, _Keep)
        rat_enc = (
            None if keep_rat else (encrypt_secret(registration_access_token) if registration_access_token else None)
        )
        # Table-qualified on both backends. DuckDB accepts the bare name here
        # (verified), but Postgres rejects it as ambiguous — matching the two
        # removes the question of which dialect tolerates what.
        rat_update = (
            "mcp_source_oauth_clients.registration_access_token_enc"
            if keep_rat
            else "excluded.registration_access_token_enc"
        )
        self.conn.execute(
            f"""INSERT INTO mcp_source_oauth_clients
               (source_id, issuer, client_id, client_secret_enc, registration_access_token_enc,
                authorization_endpoint, token_endpoint, scopes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source_id) DO UPDATE SET
                   issuer                         = excluded.issuer,
                   client_id                      = excluded.client_id,
                   client_secret_enc              = excluded.client_secret_enc,
                   registration_access_token_enc  = {rat_update},
                   authorization_endpoint         = excluded.authorization_endpoint,
                   token_endpoint                 = excluded.token_endpoint,
                   scopes                         = excluded.scopes,
                   updated_at                     = excluded.updated_at""",
            [
                source_id,
                issuer,
                client_id,
                secret_enc,
                rat_enc,
                authorization_endpoint,
                token_endpoint,
                scopes,
                now,
                now,
            ],
        )

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Decrypted row for ``source_id``, or ``None`` if absent.

        ``client_secret`` / ``registration_access_token`` come back as
        decrypted plaintext (``None`` when the column is NULL or fails to
        decrypt — vault key rotated)."""
        row = self.conn.execute(
            """SELECT source_id, issuer, client_id, client_secret_enc,
                      registration_access_token_enc, authorization_endpoint,
                      token_endpoint, scopes, created_at, updated_at
               FROM mcp_source_oauth_clients WHERE source_id = ?""",
            [source_id],
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self.conn.description]
        d = dict(zip(cols, row))
        secret_enc = d.pop("client_secret_enc")
        rat_enc = d.pop("registration_access_token_enc")
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
        self.conn.execute("DELETE FROM mcp_source_oauth_clients WHERE source_id = ?", [source_id])
