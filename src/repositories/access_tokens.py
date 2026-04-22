"""Repository for personal access tokens (#12)."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class AccessTokenRepository:
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
        name: str,
        token_hash: str,
        prefix: str,
        expires_at: Optional[datetime] = None,
        scopes: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO personal_access_tokens
            (id, user_id, name, token_hash, prefix, scopes, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, user_id, name, token_hash, prefix, scopes,
             datetime.now(timezone.utc), expires_at],
        )

    def get_by_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM personal_access_tokens WHERE id = ?", [token_id]
        ).fetchone()
        return self._row_to_dict(result)

    def list_for_user(self, user_id: str, include_revoked: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM personal_access_tokens WHERE user_id = ?"
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        rows = self.conn.execute(sql, [user_id]).fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM personal_access_tokens ORDER BY created_at DESC"
        ).fetchall()
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def revoke(self, token_id: str) -> None:
        self.conn.execute(
            "UPDATE personal_access_tokens SET revoked_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), token_id],
        )

    def delete(self, token_id: str) -> None:
        self.conn.execute("DELETE FROM personal_access_tokens WHERE id = ?", [token_id])

    def mark_used(self, token_id: str, ip: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE personal_access_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
            [datetime.now(timezone.utc), ip, token_id],
        )
