"""Repository for internal roles (app-defined capabilities).

Internal roles are registered by Agnes modules at import-time via
``app.auth.role_resolver.register_internal_role`` and synced into this table
on startup. Admins map external Cloud Identity groups onto these roles via
``GroupMappingsRepository`` — they don't create roles in the UI.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class InternalRolesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def get_by_id(self, role_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM internal_roles WHERE id = ?", [role_id]
        ).fetchone()
        return self._row_to_dict(result)

    def get_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM internal_roles WHERE key = ?", [key]
        ).fetchone()
        return self._row_to_dict(result)

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM internal_roles ORDER BY key"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def create(
        self,
        id: str,
        key: str,
        display_name: str,
        description: Optional[str] = None,
        owner_module: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO internal_roles
               (id, key, display_name, description, owner_module, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [id, key, display_name, description, owner_module, now, now],
        )

    def update(self, id: str, **kwargs) -> None:
        # `key` is intentionally NOT in the allowlist — it's the immutable
        # identifier referenced from code; a rename would silently break
        # every group mapping pointing at it. Re-register under a new key
        # and have an admin re-map.
        allowed = {"display_name", "description", "owner_module"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [id]
        self.conn.execute(
            f"UPDATE internal_roles SET {set_clause} WHERE id = ?", values
        )

    def delete(self, role_id: str) -> None:
        # Caller should delete dependent group_mappings first (no ON DELETE
        # CASCADE on the FK) — surfaces dangling references instead of
        # silently dropping mappings.
        self.conn.execute("DELETE FROM internal_roles WHERE id = ?", [role_id])
