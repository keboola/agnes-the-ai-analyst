"""Repository for Agnes Cowork setup tokens (v60)."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class SetupTokenRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def create(
        self,
        id: str,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        self.conn.execute(
            """INSERT INTO setup_tokens
               (id, user_id, token_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [id, user_id, token_hash, expires_at, datetime.now(timezone.utc)],
        )

    def get_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM setup_tokens WHERE token_hash = ?",
            [token_hash],
        ).fetchone()
        return self._row_to_dict(result)

    def mark_used(self, token_id: str) -> bool:
        """Atomically mark a token as used. Returns True if the update hit a row.

        Uses RETURNING to detect whether the WHERE clause matched — if used_at
        was already non-NULL, the UPDATE condition fails and no row is returned.
        """
        result = self.conn.execute(
            "UPDATE setup_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL RETURNING id",
            [datetime.now(timezone.utc), token_id],
        ).fetchone()
        return result is not None

    def list_active_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Active = not used + not expired."""
        now = datetime.now(timezone.utc)
        rows = self.conn.execute(
            """SELECT * FROM setup_tokens
               WHERE user_id = ?
                 AND used_at IS NULL
                 AND expires_at > ?
               ORDER BY created_at DESC""",
            [user_id, now],
        ).fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def count_active_for_user(self, user_id: str) -> int:
        now = datetime.now(timezone.utc)
        return self.conn.execute(
            """SELECT COUNT(*) FROM setup_tokens
               WHERE user_id = ?
                 AND used_at IS NULL
                 AND expires_at > ?""",
            [user_id, now],
        ).fetchone()[0]

    def delete(self, token_id: str) -> None:
        self.conn.execute("DELETE FROM setup_tokens WHERE id = ?", [token_id])

    def delete_expired(self) -> int:
        """Remove tokens older than 24 h past their expiry. Returns deleted count."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        rows = self.conn.execute(
            "DELETE FROM setup_tokens WHERE expires_at < ? RETURNING id", [cutoff]
        ).fetchall()
        return len(rows)
