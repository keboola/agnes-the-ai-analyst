"""Repository for direct user → internal role grants (v9).

Complementary to ``group_mappings``: ``group_mappings`` drives session-cached
resolution at sign-in for OAuth users, ``user_role_grants`` drives DB-backed
resolution for PAT/headless callers and persists across sessions. The v8→v9
backfill seeds one row per existing user with ``source='auto-seed'``;
admin-issued grants use ``source='direct'``.

The resolver in ``app.auth.role_resolver.resolve_internal_roles`` reads this
table whenever ``user_id`` is supplied — see ``require_internal_role`` for
the PAT fallback path.
"""

from typing import Any, Dict, List, Optional

import duckdb


class UserRoleGrantsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get(self, grant_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM user_role_grants WHERE id = ?", [grant_id]
        ).fetchone()
        return self._row_to_dict(result)

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """All grants for a user, joined with internal_roles for display.

        Includes both ``source='direct'`` (admin-issued) and
        ``source='auto-seed'`` (v9 backfill) rows — callers that care about
        provenance read the ``source`` column. Sorted by role key for
        deterministic output.
        """
        results = self.conn.execute(
            """SELECT g.*, r.key AS role_key, r.display_name AS role_display_name,
                      r.is_core AS role_is_core
               FROM user_role_grants g
               JOIN internal_roles r ON g.internal_role_id = r.id
               WHERE g.user_id = ?
               ORDER BY r.key""",
            [user_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_by_role(self, role_id: str) -> List[Dict[str, Any]]:
        """All grants pointing at a specific internal role.

        Used by admin tooling to enumerate the holders of a given capability —
        e.g. "who has ``core.admin``?". Joined with users for display.
        """
        results = self.conn.execute(
            """SELECT g.*, u.email AS user_email, u.name AS user_name
               FROM user_role_grants g
               JOIN users u ON g.user_id = u.id
               WHERE g.internal_role_id = ?
               ORDER BY u.email""",
            [role_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def create(
        self,
        id: str,
        user_id: str,
        internal_role_id: str,
        granted_by: Optional[str] = None,
        source: str = "direct",
    ) -> None:
        """Insert a grant. Caller handles UNIQUE-constraint violation.

        ``source`` defaults to ``'direct'`` for admin-issued grants; the v9
        backfill (in src/db.py) is the only caller that passes
        ``'auto-seed'``. Constraint violations propagate as
        ``duckdb.ConstraintException`` so callers can map to HTTP 409 in
        REST handlers.
        """
        self.conn.execute(
            """INSERT INTO user_role_grants
               (id, user_id, internal_role_id, granted_by, source)
               VALUES (?, ?, ?, ?, ?)""",
            [id, user_id, internal_role_id, granted_by, source],
        )

    def delete(self, grant_id: str) -> None:
        self.conn.execute(
            "DELETE FROM user_role_grants WHERE id = ?", [grant_id]
        )

    def delete_for_user(self, user_id: str) -> None:
        """Remove every grant a user holds. Used by user deletion paths.

        UserRepository.delete() does this implicitly; exposed here so
        admin tooling that wants to wipe a user's role assignments without
        deleting the row itself (e.g. before a manual re-grant) doesn't have
        to drop into raw SQL.
        """
        self.conn.execute(
            "DELETE FROM user_role_grants WHERE user_id = ?", [user_id]
        )
