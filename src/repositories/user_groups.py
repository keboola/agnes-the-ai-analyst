"""Repository for the ``user_groups`` table.

A ``user_group`` is a named bucket admins create (e.g. ``data-team``,
``Engineering``) plus the two seeded ``is_system=TRUE`` groups ``Admin``
and ``Everyone``. Membership lives in
:mod:`src.repositories.user_group_members`; resource grants in
:mod:`src.repositories.resource_grants`.

System groups are write-protected — :exc:`SystemGroupProtected` is raised
on attempts to rename or delete them so the canonical ``Admin`` /
``Everyone`` names referenced from code (``app.auth.access``) cannot
disappear out from under the authorization layer.
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

    def ensure(
        self, name: str, description: Optional[str] = None
    ) -> Dict[str, Any]:
        """Idempotent get-or-create for claim-driven groups.

        Existing row is returned unchanged (preserves `is_system` and
        description — a later Google-sync call must not override an admin's
        manual description edit).
        """
        existing = self.get_by_name(name)
        if existing:
            return existing
        return self.create(
            name=name,
            description=description or "Auto-created from Google Workspace claim",
            created_by="system:google-sync",
        )

    def ensure_system(self, name: str, description: str) -> Dict[str, Any]:
        """Idempotently ensure a system group exists.

        If a group with the given name exists (manually created by an admin),
        promote it to system (is_system=TRUE). Otherwise create a new one.
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
        # Block rename of system groups — the canonical names "Admin" /
        # "Everyone" are referenced from `app.auth.access` and the
        # marketplace filter and must not move. Description edits are
        # cosmetic and allowed (admins curate them in /admin/access).
        existing = self.get(group_id)
        if (
            existing
            and existing.get("is_system")
            and name is not None
            and name != existing["name"]
        ):
            raise SystemGroupProtected(
                f"group {existing.get('name')!r} is a system group and cannot be renamed"
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
        self.conn.execute("DELETE FROM user_groups WHERE id = ?", [group_id])
