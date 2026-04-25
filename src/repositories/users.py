"""Repository for user management."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


class UserRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
        return self._row_to_dict(result)

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM users ORDER BY email").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def create(
        self,
        id: str,
        email: str,
        name: str,
        role: str = "analyst",
        password_hash: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO users (id, email, name, role, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, email, name, role, password_hash, now, now],
        )

    def update(self, id: str, **kwargs) -> None:
        allowed = {
            "email", "name", "role", "password_hash", "setup_token",
            "setup_token_created", "reset_token", "reset_token_created",
            "active", "deactivated_at", "deactivated_by", "groups",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def count_admins(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        if active_only:
            sql += " AND COALESCE(active, TRUE) = TRUE"
        result = self.conn.execute(sql).fetchone()
        return int(result[0]) if result else 0

    def delete(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM users WHERE id = ?", [user_id])
