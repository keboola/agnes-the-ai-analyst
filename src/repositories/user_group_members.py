"""Repository for user → group membership.

Each row binds one user to one group with a `source` label tracking who
created the row. The source matters because multiple writers populate this
table:

  - ``google_sync``  — OAuth callback rewrites the user's Google-derived
                       memberships on every login (DELETE+INSERT scoped to
                       this source).
  - ``admin``        — admin UI/CLI manual additions; survives Google sync.
                       Refused on groups bound to an external identity
                       provider (``user_groups.external_id IS NOT NULL``)
                       since membership for those groups is sourced
                       authoritatively from the provider.
  - ``system_seed``  — bootstrap-time seed for the SEED_ADMIN_EMAIL Admin
                       grant. Survives Google sync. Allowed on
                       ``external_id``-bound groups so the bootstrap path
                       still works before the first Google sync attaches an
                       external link to Admin.

The ``replace_google_sync_groups`` method is the bulk operation called from
the OAuth callback; ``add_member`` / ``remove_member`` cover admin actions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import duckdb


class ExternalGroupReadOnly(Exception):
    """Raised when an admin-source mutation targets a group bound to an
    external identity-provider group (``user_groups.external_id`` set).
    Membership for those groups is authoritative from the provider —
    Google sync's ``replace_google_sync_groups`` is the only path that
    writes them. SEED_ADMIN_EMAIL bootstrap (``source='system_seed'``) is
    intentionally exempt so a fresh deploy can grant the seed admin even
    before the first Google sync attaches an external link to Admin."""


class UserGroupMembersRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def list_groups_for_user(self, user_id: str) -> List[str]:
        """Group IDs this user belongs to (any source)."""
        rows = self.conn.execute(
            "SELECT group_id FROM user_group_members WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]

    def list_members_for_group(self, group_id: str) -> List[Dict[str, Any]]:
        """All users in a group, joined with users table for display data."""
        rows = self.conn.execute(
            """SELECT u.id, u.email, u.name, u.active,
                      m.source, m.added_at, m.added_by
               FROM user_group_members m
               JOIN users u ON u.id = m.user_id
               WHERE m.group_id = ?
               ORDER BY u.email""",
            [group_id],
        ).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def has_membership(self, user_id: str, group_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM user_group_members WHERE user_id = ? AND group_id = ?",
            [user_id, group_id],
        ).fetchone()
        return row is not None

    def has_any_google_sync_membership(self, user_id: str) -> bool:
        """True iff the user has at least one ``source='google_sync'`` row.

        Used by the OAuth callback to decide whether to pass-through a soft
        Google API failure: returning users with cached membership are let
        in (we don't lock everyone out during a transient Google outage);
        first-timers without any cached state are denied (we can't verify
        their eligibility).
        """
        row = self.conn.execute(
            "SELECT 1 FROM user_group_members "
            "WHERE user_id = ? AND source = 'google_sync' LIMIT 1",
            [user_id],
        ).fetchone()
        return row is not None

    def _guard_external(self, group_id: str, source: str) -> None:
        """Refuse admin-source writes to externally-bound groups.

        Lookup is one indexed query and runs only on admin-path mutations;
        google_sync (via ``replace_google_sync_groups``) and system_seed
        bootstrap bypass this entirely.
        """
        if source != "admin":
            return
        row = self.conn.execute(
            "SELECT external_id, name FROM user_groups WHERE id = ?", [group_id]
        ).fetchone()
        if row is None:
            return
        ext, name = row
        if ext is not None:
            raise ExternalGroupReadOnly(
                f"group {name!r} is bound to external identity provider "
                f"({ext}); membership is managed at the source"
            )

    def add_member(
        self,
        user_id: str,
        group_id: str,
        source: str,
        added_by: Optional[str] = None,
    ) -> None:
        """Insert a membership row. Idempotent on (user_id, group_id) PK.

        Re-adding an existing pair is a silent no-op — the source/added_by of
        the existing row stays. Use ``replace_google_sync_groups`` if you
        want google_sync rows to refresh wholesale.

        Raises ``ExternalGroupReadOnly`` when ``source='admin'`` targets a
        group bound to an external identity provider — admins must edit the
        membership at the source instead.
        """
        self._guard_external(group_id, source)
        try:
            self.conn.execute(
                """INSERT INTO user_group_members
                   (user_id, group_id, source, added_by)
                   VALUES (?, ?, ?, ?)""",
                [user_id, group_id, source, added_by],
            )
        except duckdb.ConstraintException:
            pass  # already a member; preserve original source

    def remove_member(
        self,
        user_id: str,
        group_id: str,
        require_source: Optional[str] = None,
    ) -> bool:
        """Delete a membership row. Returns True if a row was deleted.

        ``require_source`` blocks the delete unless the row matches that
        source — admin UI passes ``'admin'`` so it cannot accidentally undo
        a system seed or a Google sync (Google sync rolls itself back via
        ``replace_google_sync_groups``).

        Raises ``ExternalGroupReadOnly`` when ``require_source='admin'``
        targets a group bound to an external identity provider — even
        admin-source rows on a bound group should never exist (the guard in
        ``add_member`` blocks creating them), but we mirror the check here
        for callers that pass through ``require_source='admin'`` as a UI
        intent marker.
        """
        if require_source == "admin":
            self._guard_external(group_id, require_source)
        if require_source is not None:
            res = self.conn.execute(
                """DELETE FROM user_group_members
                   WHERE user_id = ? AND group_id = ? AND source = ?
                   RETURNING 1""",
                [user_id, group_id, require_source],
            ).fetchone()
        else:
            res = self.conn.execute(
                """DELETE FROM user_group_members
                   WHERE user_id = ? AND group_id = ?
                   RETURNING 1""",
                [user_id, group_id],
            ).fetchone()
        return res is not None

    def replace_google_sync_groups(
        self,
        user_id: str,
        group_ids: List[str],
        added_by: str = "system:google-sync",
    ) -> None:
        """Authoritative refresh of this user's google_sync memberships.

        DELETEs every row with ``source='google_sync'`` for this user, then
        INSERTs one row per ``group_ids``. Admin and system_seed rows are
        untouched. Called from the OAuth callback on every login so the
        membership reflects the current Cloud Identity state.
        """
        self.conn.execute(
            "DELETE FROM user_group_members WHERE user_id = ? AND source = 'google_sync'",
            [user_id],
        )
        for group_id in group_ids:
            try:
                self.conn.execute(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (?, ?, 'google_sync', ?)""",
                    [user_id, group_id, added_by],
                )
            except duckdb.ConstraintException:
                # Admin or system_seed row already present for this pair —
                # leave it alone, the user is already a member through a
                # higher-priority source.
                pass

    def remove_user_from_all_groups(self, user_id: str) -> int:
        """Hard delete every membership for a user. Used on user deletion.

        Returns the number of rows removed. Doesn't filter by source — the
        user is going away, every reference goes with them.
        """
        rows = self.conn.execute(
            "DELETE FROM user_group_members WHERE user_id = ? RETURNING 1",
            [user_id],
        ).fetchall()
        return len(rows)

    def count_members(self, group_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM user_group_members WHERE group_id = ?",
            [group_id],
        ).fetchone()
        return int(row[0]) if row else 0
