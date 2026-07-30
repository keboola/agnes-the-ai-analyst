"""Repository for `mcp_oauth_flows` (v109) — in-flight outbound OAuth
authorize-flow state (PKCE verifier + nonce).

DB-backed (rather than an in-process/session store) so multi-replica
Postgres deployments need no sticky sessions and single-replica DuckDB
works identically. Rows are single-use: ``consume()`` atomically deletes
the row on read (a second call for the same nonce returns ``None``) —
this is the state/PKCE anti-replay contract from the spec's security
checklist, not merely a convenience.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import duckdb

from app.secrets_vault import decrypt_optional, encrypt_secret

#: Default flow lifetime — matches the spec's "rows expire after 10 min".
DEFAULT_TTL_SECONDS = 600


class MCPOAuthFlowRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(self, nonce: str, source_id: str, user_id: str, pkce_verifier: str) -> None:
        now = datetime.now(timezone.utc)
        verifier_enc = encrypt_secret(pkce_verifier)
        self.conn.execute(
            """INSERT INTO mcp_oauth_flows (nonce, source_id, user_id, pkce_verifier_enc, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [nonce, source_id, user_id, verifier_enc, now],
        )

    def consume(self, nonce: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a flow exactly once.

        ``DELETE ... RETURNING`` means a second concurrent call for the
        same nonce matches zero rows and gets ``None`` — no separate
        SELECT-then-DELETE race window. If the PKCE verifier fails to
        decrypt (vault key rotated), the row is still gone (single-use
        honored) and ``None`` is returned — an unusable flow is treated
        exactly like a missing one.
        """
        row = self.conn.execute(
            """DELETE FROM mcp_oauth_flows WHERE nonce = ?
               RETURNING source_id, user_id, pkce_verifier_enc, created_at""",
            [nonce],
        ).fetchone()
        if row is None:
            return None
        source_id, user_id, verifier_enc, created_at = row
        pkce_verifier = decrypt_optional(verifier_enc, context=f"mcp_oauth_flows.pkce_verifier[{nonce}]")
        if pkce_verifier is None:
            return None
        return {
            "nonce": nonce,
            "source_id": source_id,
            "user_id": user_id,
            "pkce_verifier": pkce_verifier,
            "created_at": created_at,
        }

    def sweep_expired(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Delete every flow older than ``ttl_seconds``. Returns the
        deleted-row count — callable opportunistically (e.g. from
        ``create()``'s caller) or from a maintenance job."""
        threshold = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        rows = self.conn.execute(
            "DELETE FROM mcp_oauth_flows WHERE created_at < ? RETURNING nonce",
            [threshold],
        ).fetchall()
        return len(rows)
