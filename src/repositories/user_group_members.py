"""Repository for user → group membership.

Each row binds one user to one group with a `source` label tracking who
created the row. The source matters because multiple writers populate this
table:

  - ``google_sync``  — OAuth callback rewrites the user's Google-derived
                       memberships on every login (DELETE+INSERT scoped to
                       this source).
  - ``admin``        — admin UI/CLI manual additions; survives Google sync.
  - ``system_seed``  — deploy-time seeds (Admin grant for SEED_ADMIN_EMAIL);
                       survives Google sync and refuses removal via the
                       admin path. The auto-Everyone seed for every new
                       user was removed when Google-prefix mapping landed
                       — explicit grants only.

The ``replace_google_sync_groups`` method is the bulk operation called from
the OAuth callback; ``add_member`` / ``remove_member`` cover admin actions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import duckdb


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
        """
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
        """
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

        Wrapped in a single transaction so concurrent readers never observe
        the post-DELETE / pre-INSERT window where the user has *no*
        google_sync groups. ``get_system_db()`` hands every caller a cursor
        on one shared connection, so a non-atomic rebuild leaks an empty
        intermediate state to anything reading membership mid-refresh — e.g.
        the marketplace git endpoint resolving a user's served plugin set,
        which would transiently drop every plugin granted via a google_sync
        group until the re-INSERTs commit. Mirrors the PG repo's
        ``self._engine.begin()`` atomicity (cross-engine parity).
        """
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "DELETE FROM user_group_members "
                "WHERE user_id = ? AND source = 'google_sync'",
                [user_id],
            )
            for group_id in group_ids:
                # ON CONFLICT DO NOTHING: an Admin / system_seed row may
                # already own this (user_id, group_id) pair — the user is
                # a member through a higher-priority source, leave it. Using
                # the conflict clause instead of catching ConstraintException
                # keeps the surrounding transaction alive (a raised
                # constraint error would otherwise abort it). Matches PG.
                self.conn.execute(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (?, ?, 'google_sync', ?)
                       ON CONFLICT (user_id, group_id) DO NOTHING""",
                    [user_id, group_id, added_by],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

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

    def delete_all_for_group(self, group_id: str) -> int:
        """Drop every membership row pointing at ``group_id``.

        Used by group-delete cascade in ``app/api/access.py`` so a group
        row's removal doesn't leave dangling membership rows.
        """
        rows = self.conn.execute(
            "DELETE FROM user_group_members WHERE group_id = ? RETURNING 1",
            [group_id],
        ).fetchall()
        return len(rows)

    def list_groups_with_meta_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return groups the user is in joined with the groups table.

        Each row: ``{group_id, name, is_system, created_by, source}``.
        Powers the user-detail endpoints in ``app.api.users`` that need
        the membership graph + group metadata in a single round-trip.
        """
        rows = self.conn.execute(
            """SELECT g.id, g.name, g.is_system, g.created_by, m.source
               FROM user_group_members m
               JOIN user_groups g ON g.id = m.group_id
               WHERE m.user_id = ?
               ORDER BY g.is_system DESC, g.name""",
            [user_id],
        ).fetchall()
        return [
            {
                "group_id": r[0],
                "name": r[1],
                "is_system": bool(r[2]),
                "created_by": r[3],
                "source": r[4],
            }
            for r in rows
        ]

    def has_any_google_sync_membership(self, user_id: str) -> bool:
        """Whether the user has any prior `source='google_sync'` row.

        Used by the OAuth callback to distinguish a brand-new login (where
        an empty fetch from Cloud Identity might mean the user genuinely
        has no Workspace groups) from a returning user with a previously
        cached membership snapshot. Returning users get a pass-through on
        empty fetch (transient API failures must not lock them out); a
        fresh-login no-cache empty fetch is treated identically by the
        current callback (pass-through), so this helper is presently
        diagnostic — kept here so a future tightening of the gate can
        flip the branch without a new query path.
        """
        row = self.conn.execute(
            "SELECT 1 FROM user_group_members "
            "WHERE user_id = ? AND source = 'google_sync' LIMIT 1",
            [user_id],
        ).fetchone()
        return row is not None
