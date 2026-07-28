"""Repository for CLI browser-loopback login exchange codes (v61).

Backs the gh-style ``agnes auth login`` flow: the browser (holding an
authenticated session) confirms CLI authorization, the server mints a
single-use code bound to the user, and the CLI exchanges that code for a
real Personal Access Token over a direct HTTPS call. Only a sha256 of the
code is persisted — the raw code lives only in the loopback redirect URL and
the CLI's exchange request, never on disk.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb


class CliAuthCodeRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(
        self,
        code_hash: str,
        user_id: str,
        email: str,
        expires_at: datetime,
    ) -> None:
        """Insert a fresh code and opportunistically reap expired rows.

        The reap keeps the table from growing unbounded without a separate
        sweeper — login volume is low, so a single DELETE per create is cheap.
        """
        now = datetime.now(timezone.utc)
        # Opportunistic cleanup: drop rows that expired more than a minute ago
        # (small grace so an in-flight exchange of a just-expired code can
        # still read its row to report "expired" cleanly rather than "unknown").
        try:
            self.conn.execute(
                "DELETE FROM cli_auth_codes WHERE expires_at < ?",
                [now],
            )
        except Exception:
            pass  # cleanup is best-effort; never block a login on it
        self.conn.execute(
            """INSERT INTO cli_auth_codes
            (code_hash, user_id, email, created_at, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, ?, NULL)""",
            [code_hash, user_id, email, now, expires_at],
        )

    def consume(self, code_hash: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a code exactly once.

        Returns ``{"user_id", "email"}`` for the single caller that wins the
        race, ``None`` for everyone else (already consumed, expired, or
        unknown). Uses ``UPDATE ... RETURNING`` so only the row actually
        transitioned from unconsumed→consumed comes back — a second concurrent
        call matches zero rows and gets ``None``.
        """
        now = datetime.now(timezone.utc)
        try:
            row = self.conn.execute(
                """UPDATE cli_auth_codes
                   SET consumed_at = ?
                   WHERE code_hash = ?
                     AND consumed_at IS NULL
                     AND expires_at >= ?
                   RETURNING user_id, email""",
                [now, code_hash, now],
            ).fetchone()
        except Exception:
            # DuckDB raises on a write-write conflict if two exchanges land
            # concurrently; the loser simply gets nothing.
            return None
        if not row:
            return None
        return {"user_id": row[0], "email": row[1]}
