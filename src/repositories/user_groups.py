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


# Sentinel distinguishing "caller passed None" from "caller didn't pass". Used
# in update() to detect any attempt to write external_id (including None),
# which is forbidden post-creation.
_UNSET = object()


class SystemGroupProtected(Exception):
    """Raised when a mutation is attempted on a system user group (is_system=TRUE)."""


class ExternalIdConflict(Exception):
    """Raised when resolve_or_create_for_external would attach an external_id to
    a row that already carries a different external_id. The Google sync logs
    and skips that group rather than overwriting the existing binding — admin
    must rename one of the colliding groups manually."""


class ExternalIdImmutable(Exception):
    """Raised when ``update()`` is called with an ``external_id`` argument.
    The link between an Agnes group and its external identity-provider group
    is set once at creation (or via attach_external_id during promote) and
    cannot be edited afterward — that would silently re-route members.
    Renaming the display ``name`` of a non-system bound group is allowed."""


class UserGroupsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _SELECT_COLS = "id, name, description, is_system, external_id, created_at, created_by"

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

    def get_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """Look up a group by the external identity-provider id (e.g. a Google
        Workspace group email). Lookup is case-insensitive on the stored
        value — callers normalize on insert too."""
        row = self.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM user_groups "
            "WHERE LOWER(external_id) = LOWER(?)",
            [external_id],
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in self.conn.description]
        return dict(zip(columns, row))

    def attach_external_id(self, group_id: str, external_id: str) -> None:
        """Bind an existing group to an external identity-provider id.

        Used by Google sync's "promote" path: when the derived display name
        matches an existing group with NULL ``external_id`` (e.g. an admin
        manually created ``Finance`` before Google sync ever saw
        ``grp_*_finance@``), we attach the link to the existing row instead
        of creating a duplicate. Idempotent on re-attach with the same value.
        Raises ``ExternalIdConflict`` if the row already carries a different
        non-NULL ``external_id`` — that would silently re-route membership.
        """
        existing = self.conn.execute(
            "SELECT external_id FROM user_groups WHERE id = ?", [group_id]
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown group_id: {group_id}")
        current = existing[0]
        normalized = external_id.lower()
        if current is not None and current.lower() != normalized:
            raise ExternalIdConflict(
                f"group {group_id} already bound to {current!r}; "
                f"refusing to rebind to {external_id!r}"
            )
        if current is None:
            self.conn.execute(
                "UPDATE user_groups SET external_id = ? WHERE id = ?",
                [normalized, group_id],
            )

    def resolve_or_create_for_external(
        self, email: str, prefix: str
    ) -> Dict[str, Any]:
        """Idempotent group resolution from a Workspace group email.

        Three-step lookup, mirrors the promote semantics of
        ``_seed_system_groups``:

          1. Match by ``external_id`` → return as-is.
          2. Match by derived display name (``email`` minus ``prefix``,
             capitalized first letter). If found and ``external_id`` is NULL,
             attach the email and return. If found and ``external_id`` is set
             to a different value, raise ``ExternalIdConflict``.
          3. Create a fresh row with the derived name and the email as
             ``external_id``.

        Caller is the OAuth callback in ``app.auth.providers.google``. The
        returned dict matches the ``_SELECT_COLS`` shape from this repo.
        """
        normalized_email = email.lower().strip()
        existing_by_ext = self.get_by_external_id(normalized_email)
        if existing_by_ext is not None:
            return existing_by_ext

        local_part = normalized_email.split("@", 1)[0]
        stripped = local_part.removeprefix(prefix.lower()) if prefix else local_part
        derived_name = stripped.capitalize() if stripped else local_part

        existing_by_name = self.get_by_name(derived_name)
        if existing_by_name is not None:
            current_ext = existing_by_name.get("external_id")
            if current_ext is None:
                self.attach_external_id(existing_by_name["id"], normalized_email)
                return self.get(existing_by_name["id"])  # type: ignore[return-value]
            if current_ext.lower() != normalized_email:
                raise ExternalIdConflict(
                    f"group {derived_name!r} already bound to "
                    f"{current_ext!r}; cannot also bind to {normalized_email!r}"
                )
            return existing_by_name

        group_id = uuid4().hex
        self.conn.execute(
            "INSERT INTO user_groups "
            "(id, name, description, is_system, external_id, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                group_id,
                derived_name,
                f"Auto-created from Google Workspace group {normalized_email}",
                False,
                normalized_email,
                datetime.now(timezone.utc),
                "system:google-sync",
            ],
        )
        return self.get(group_id)  # type: ignore[return-value]

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
        external_id: Any = _UNSET,
    ) -> None:
        # external_id is set once at creation (or via attach_external_id during
        # promote); editing it here would silently re-route membership. The
        # display name + description remain editable on non-system bound groups
        # so admins can rename "Finance" → "Finance team" without touching the
        # Google link.
        if external_id is not _UNSET:
            raise ExternalIdImmutable(
                "external_id is set at creation and cannot be edited; "
                "use attach_external_id on a NULL row, or rename the group "
                "name instead"
            )
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
