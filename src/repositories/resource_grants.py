"""Repository for ``resource_grants`` — group → (resource_type, resource_id).

Each row is a single grant: members of ``group_id`` are allowed access to
``resource_id`` of kind ``resource_type``. The format of ``resource_id`` is
owned by the module that registered the resource type (see
``app.resource_types``); the repository treats it as an opaque string.

The resolver in ``app.auth.access`` reads this table on every authorization
check that isn't satisfied by Admin short-circuit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class ResourceGrantsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _SELECT_COLS = (
        "id, group_id, resource_type, resource_id, "
        "assigned_at, assigned_by"
    )

    def list_all(
        self,
        resource_type: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List grants joined with the group's name for display purposes."""
        where = []
        params: List[Any] = []
        if resource_type:
            where.append("g.resource_type = ?")
            params.append(resource_type)
        if group_id:
            where.append("g.group_id = ?")
            params.append(group_id)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            f"""SELECT g.id, g.group_id, ug.name AS group_name,
                       g.resource_type, g.resource_id,
                       g.assigned_at, g.assigned_by
                FROM resource_grants g
                JOIN user_groups ug ON ug.id = g.group_id
                {where_sql}
                ORDER BY ug.name, g.resource_type, g.resource_id""",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def list_for_groups(
        self,
        group_ids: List[str],
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """All grants held by any of the given groups, optionally type-scoped.

        Used by the marketplace filter to materialize a user's allowed plugins
        in one round trip — caller passes the user's full group set.
        """
        if not group_ids:
            return []
        placeholders = ",".join(["?"] * len(group_ids))
        type_clause = "AND resource_type = ?" if resource_type else ""
        params: List[Any] = [*group_ids]
        if resource_type:
            params.append(resource_type)
        rows = self.conn.execute(
            f"""SELECT {self._SELECT_COLS}
                FROM resource_grants
                WHERE group_id IN ({placeholders}) {type_clause}
                ORDER BY resource_type, resource_id""",
            params,
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def get(self, grant_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM resource_grants WHERE id = ?",
            [grant_id],
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.description]
        return dict(zip(cols, row))

    def has_grant(
        self,
        group_ids: List[str],
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Single-purpose existence check used by ``can_access``.

        Returns True iff any of the given groups has a grant for the
        (resource_type, resource_id) pair. One DB hit, indexed on the
        UNIQUE (group_id, resource_type, resource_id) constraint.
        """
        if not group_ids:
            return False
        placeholders = ",".join(["?"] * len(group_ids))
        row = self.conn.execute(
            f"""SELECT 1 FROM resource_grants
                WHERE group_id IN ({placeholders})
                  AND resource_type = ?
                  AND resource_id = ?
                LIMIT 1""",
            [*group_ids, resource_type, resource_id],
        ).fetchone()
        return row is not None

    def create(
        self,
        group_id: str,
        resource_type: str,
        resource_id: str,
        assigned_by: Optional[str] = None,
    ) -> str:
        """Insert a new grant. Returns the assigned id.

        Raises ``duckdb.ConstraintException`` on duplicate
        (group_id, resource_type, resource_id) — caller surfaces as 409.
        """
        grant_id = str(uuid4())
        self.conn.execute(
            """INSERT INTO resource_grants
               (id, group_id, resource_type, resource_id, assigned_by)
               VALUES (?, ?, ?, ?, ?)""",
            [grant_id, group_id, resource_type, resource_id, assigned_by],
        )
        return grant_id

    def delete(self, grant_id: str) -> bool:
        """Remove a grant by id. Returns True iff a row was removed."""
        res = self.conn.execute(
            "DELETE FROM resource_grants WHERE id = ? RETURNING 1",
            [grant_id],
        ).fetchone()
        return res is not None

    def delete_by_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        """Remove every grant for a resource. Used when an entity is deleted
        upstream (e.g. a marketplace plugin disappears) so dangling grants
        don't accumulate. Returns the number of rows removed.
        """
        rows = self.conn.execute(
            """DELETE FROM resource_grants
               WHERE resource_type = ? AND resource_id = ?
               RETURNING 1""",
            [resource_type, resource_id],
        ).fetchall()
        return len(rows)

    def count_for_group(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM resource_grants WHERE group_id = ?",
            [group_id],
        ).fetchone()
        return int(row[0]) if row else 0
