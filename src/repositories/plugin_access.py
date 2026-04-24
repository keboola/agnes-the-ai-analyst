"""Repositories for user groups and plugin-access grants.

`user_groups` is a lightweight label registry — just `(id, name)` —
intended to be consumed by a future code path that will materialise a
per-group Claude Code marketplace endpoint. There is no user-to-group
membership table yet; that lives outside the scope of this admin UI.

`plugin_access` is a many-to-many join keyed by
`(group_id, marketplace_id, plugin_name)`. Each row is an explicit grant
saying "group X may install plugin Y from marketplace Z".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class SystemGroupProtected(Exception):
    """Raised when a mutation is attempted on a system user group (is_system=TRUE)."""


class UserGroupsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _SELECT_COLS = "id, name, description, is_system, created_at, created_by"

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM user_groups ORDER BY name"
        ).fetchall()
        columns = [d[0] for d in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM user_groups WHERE id = ?",
            [group_id],
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in self.conn.description]
        return dict(zip(columns, row))

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM user_groups WHERE name = ?",
            [name],
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in self.conn.description]
        return dict(zip(columns, row))

    def create(
        self,
        name: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        is_system: bool = False,
    ) -> Dict[str, Any]:
        group_id = uuid4().hex
        self.conn.execute(
            "INSERT INTO user_groups (id, name, description, is_system, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [group_id, name, description, is_system, datetime.now(timezone.utc), created_by],
        )
        return self.get(group_id)  # type: ignore[return-value]

    def ensure_system(self, name: str, description: str) -> Dict[str, Any]:
        """Idempotentně zajistí existenci systémové skupiny.

        Pokud skupina s daným jménem existuje (manuálně vytvořená adminem),
        povýší ji na systémovou (is_system=TRUE). Jinak vytvoří novou.
        """
        existing = self.get_by_name(name)
        if existing:
            if not existing.get("is_system"):
                self.conn.execute(
                    "UPDATE user_groups SET is_system = TRUE WHERE id = ?",
                    [existing["id"]],
                )
                existing = self.get(existing["id"])  # type: ignore[assignment]
            return existing  # type: ignore[return-value]
        return self.create(name=name, description=description, is_system=True)

    def update(
        self,
        group_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        # Block mutation of system groups — name/description are seeded and
        # callers must not be able to rename "Admin" / "Everyone" out from
        # under the marketplace filter.
        existing = self.get(group_id)
        if existing and existing.get("is_system"):
            raise SystemGroupProtected(
                f"group {existing.get('name')!r} is a system group and cannot be modified"
            )
        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return
        params.append(group_id)
        self.conn.execute(
            f"UPDATE user_groups SET {', '.join(sets)} WHERE id = ?", params
        )

    def delete(self, group_id: str) -> None:
        existing = self.get(group_id)
        if existing and existing.get("is_system"):
            raise SystemGroupProtected(
                f"group {existing.get('name')!r} is a system group and cannot be deleted"
            )
        # plugin_access rows belong to the group and must go with it.
        self.conn.execute(
            "DELETE FROM plugin_access WHERE group_id = ?", [group_id]
        )
        self.conn.execute("DELETE FROM user_groups WHERE id = ?", [group_id])


class PluginAccessRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT group_id, marketplace_id, plugin_name, granted_at, granted_by "
            "FROM plugin_access ORDER BY group_id, marketplace_id, plugin_name"
        ).fetchall()
        columns = [d[0] for d in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def list_for_group(self, group_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT group_id, marketplace_id, plugin_name, granted_at, granted_by "
            "FROM plugin_access WHERE group_id = ? "
            "ORDER BY marketplace_id, plugin_name",
            [group_id],
        ).fetchall()
        columns = [d[0] for d in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def grant(
        self,
        group_id: str,
        marketplace_id: str,
        plugin_name: str,
        granted_by: Optional[str] = None,
    ) -> None:
        """Idempotent: existing grants are left as-is (old granted_at preserved)."""
        self.conn.execute(
            """INSERT INTO plugin_access
                (group_id, marketplace_id, plugin_name, granted_at, granted_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (group_id, marketplace_id, plugin_name) DO NOTHING""",
            [
                group_id,
                marketplace_id,
                plugin_name,
                datetime.now(timezone.utc),
                granted_by,
            ],
        )

    def revoke(self, group_id: str, marketplace_id: str, plugin_name: str) -> None:
        self.conn.execute(
            "DELETE FROM plugin_access "
            "WHERE group_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [group_id, marketplace_id, plugin_name],
        )
