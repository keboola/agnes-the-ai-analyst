"""Repository for user management."""

import json
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
        """Create a user and grant the matching core.* role.

        v9: ``role`` argument is preserved for API compatibility but no longer
        the source of truth — we also insert a ``user_role_grants`` row
        pointing at ``core.{role}``. The legacy column write is kept (and
        will be NULL-ed by the v9 backfill on existing DBs) so that during
        the transition window mixed-version code paths still see consistent
        state.
        """
        import uuid as _uuid
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO users (id, email, name, role, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, email, name, role, password_hash, now, now],
        )
        self._grant_core_role(id, role)

    def _grant_core_role(self, user_id: str, role_name: str) -> None:
        """Insert/replace the user's core.* user_role_grants row.

        Idempotent — strips any existing core.* grants for this user before
        inserting the new one, so callers can re-issue without worrying
        about duplicate-row errors. Called from create() and update() when
        the role changes.
        """
        import uuid as _uuid
        # Drop any existing core.* grants for this user.
        self.conn.execute(
            """DELETE FROM user_role_grants
               WHERE user_id = ?
               AND internal_role_id IN (
                   SELECT id FROM internal_roles WHERE is_core = true
               )""",
            [user_id],
        )
        target_key = f"core.{role_name}"
        target = self.conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [target_key],
        ).fetchone()
        if not target:
            # Unknown role string — no grant. has_role() will return False
            # via the resolver, which preserves the pre-v9 fallback to VIEWER.
            return
        self.conn.execute(
            """INSERT INTO user_role_grants
               (id, user_id, internal_role_id, granted_by, source)
               VALUES (?, ?, ?, 'user_repo.create', 'direct')""",
            [str(_uuid.uuid4()), user_id, target[0]],
        )

    def update(self, id: str, **kwargs) -> None:
        allowed = {
            "email", "name", "role", "password_hash", "setup_token",
            "setup_token_created", "reset_token", "reset_token_created",
            "active", "deactivated_at", "deactivated_by",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        # v9: when role changes, propagate to user_role_grants too.
        new_role = updates.get("role")
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        if new_role is not None:
            self._grant_core_role(id, new_role)

    def set_groups(self, user_id: str, groups: List[str]) -> None:
        """Overwrite users.groups with the given list. Workspace is source of truth.

        Kept out of update(**kwargs) on purpose — groups are rewritten only
        from the authenticated login path, never via generic admin update.
        """
        self.conn.execute(
            "UPDATE users SET groups = ?, updated_at = ? WHERE id = ?",
            [json.dumps(list(groups)), datetime.now(timezone.utc), user_id],
        )

    def count_admins(self, active_only: bool = True) -> int:
        """Count users holding the core.admin role.

        v9: counts via ``user_role_grants`` join on ``internal_roles.key =
        'core.admin'``. The legacy ``users.role = 'admin'`` is NULL after
        backfill, so the old SQL would always return 0.
        """
        sql = """
            SELECT COUNT(DISTINCT u.id)
            FROM users u
            JOIN user_role_grants g ON g.user_id = u.id
            JOIN internal_roles r ON g.internal_role_id = r.id
            WHERE r.key = 'core.admin'
        """
        if active_only:
            sql += " AND COALESCE(u.active, TRUE) = TRUE"
        result = self.conn.execute(sql).fetchone()
        return int(result[0]) if result else 0

    def delete(self, user_id: str) -> None:
        """Delete user + cascade their role grants.

        v9: ``user_role_grants.user_id`` has a FK to ``users(id)``; DuckDB
        will reject the user delete unless the grants are removed first.
        """
        self.conn.execute(
            "DELETE FROM user_role_grants WHERE user_id = ?", [user_id],
        )
        self.conn.execute("DELETE FROM users WHERE id = ?", [user_id])
