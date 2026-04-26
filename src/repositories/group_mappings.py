"""Repository for external→internal group mappings.

Each row binds a Cloud Identity group ID (as it appears in
``session.google_groups[*].id``) to an internal role. Many-to-many: a single
external group may grant several internal roles, and a single internal role
may be granted by several external groups. The resolver in
``app.auth.role_resolver`` reads this table at sign-in.
"""

from typing import Any, Dict, List, Optional

import duckdb


class GroupMappingsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, mapping_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM group_mappings WHERE id = ?", [mapping_id]
        ).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> List[Dict[str, Any]]:
        """All mappings, joined with the role's `key` for display purposes."""
        results = self.conn.execute(
            """SELECT m.*, r.key AS internal_role_key, r.display_name AS internal_role_display_name
               FROM group_mappings m
               JOIN internal_roles r ON r.id = m.internal_role_id
               ORDER BY m.external_group_id, r.key"""
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_by_external_group(self, external_group_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            """SELECT m.*, r.key AS internal_role_key
               FROM group_mappings m
               JOIN internal_roles r ON r.id = m.internal_role_id
               WHERE m.external_group_id = ?
               ORDER BY r.key""",
            [external_group_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def list_by_role(self, internal_role_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM group_mappings WHERE internal_role_id = ? "
            "ORDER BY external_group_id",
            [internal_role_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def create(
        self,
        id: str,
        external_group_id: str,
        internal_role_id: str,
        assigned_by: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO group_mappings
               (id, external_group_id, internal_role_id, assigned_by)
               VALUES (?, ?, ?, ?)""",
            [id, external_group_id, internal_role_id, assigned_by],
        )

    def delete(self, mapping_id: str) -> None:
        self.conn.execute("DELETE FROM group_mappings WHERE id = ?", [mapping_id])

    def resolve_role_keys(self, external_group_ids: List[str]) -> List[str]:
        """Map external group IDs to the set of internal role keys they grant.

        Returns a sorted, de-duplicated list — empty when ``external_group_ids``
        is empty or none of them are mapped. The resolver in
        ``app.auth.role_resolver`` calls this on every sign-in.
        """
        if not external_group_ids:
            return []
        placeholders = ",".join(["?"] * len(external_group_ids))
        rows = self.conn.execute(
            f"""SELECT DISTINCT r.key
                FROM group_mappings m
                JOIN internal_roles r ON r.id = m.internal_role_id
                WHERE m.external_group_id IN ({placeholders})
                ORDER BY r.key""",
            external_group_ids,
        ).fetchall()
        return [row[0] for row in rows]
