"""Repository for per-user curated marketplace subscriptions (Model B opt-in).

Backed by the historically-named ``user_plugin_optouts`` table. Pre-v28 a row
represented an opt-OUT against an admin-granted plugin; v28 inverts the
semantic — row PRESENCE now means the user is subscribed. The DDL rename was
intentionally skipped to avoid migration churn on running operator instances;
the v28 migration wipes the rows so the inverted reading starts clean.

Used by ``src/marketplace_filter.py:resolve_user_marketplace`` to compute the
served plugin set as ``(rbac_grants ∩ subscriptions) ∪ store_installs``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

import duckdb


class UserCuratedSubscriptionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def subscribe(
        self, user_id: str, marketplace_id: str, plugin_name: str
    ) -> bool:
        """Idempotent. Returns True iff a new row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_plugin_optouts "
            "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO user_plugin_optouts "
            "(user_id, marketplace_id, plugin_name) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING",
            [user_id, marketplace_id, plugin_name],
        )
        return True

    def unsubscribe(
        self, user_id: str, marketplace_id: str, plugin_name: str
    ) -> bool:
        """Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_plugin_optouts "
            "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM user_plugin_optouts "
            "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
            [user_id, marketplace_id, plugin_name],
        )
        return True

    def is_subscribed(
        self, user_id: str, marketplace_id: str, plugin_name: str
    ) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
                [user_id, marketplace_id, plugin_name],
            ).fetchone()
        )

    def subscribed_set(self, user_id: str) -> Set[Tuple[str, str]]:
        """Return the user's subscriptions as a ``{(marketplace_id, plugin_name)}``
        set — the shape ``resolve_user_marketplace`` filters against.
        """
        rows = self.conn.execute(
            "SELECT marketplace_id, plugin_name FROM user_plugin_optouts "
            "WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return {(r[0], r[1]) for r in rows}

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Return the user's subscriptions ordered newest-first."""
        rows = self.conn.execute(
            "SELECT marketplace_id, plugin_name, opted_out_at "
            "FROM user_plugin_optouts WHERE user_id = ? "
            "ORDER BY opted_out_at DESC",
            [user_id],
        ).fetchall()
        return [
            {
                "marketplace_id": r[0],
                "plugin_name": r[1],
                "subscribed_at": r[2],
            }
            for r in rows
        ]

    def delete_for_plugin(
        self, marketplace_id: str, plugin_name: str
    ) -> int:
        """Drop all users' subscriptions for a given plugin.

        Called when a plugin's RBAC grant is revoked or the parent marketplace
        is deleted. Returns count of rows deleted (audit telemetry).
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts "
            "WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM user_plugin_optouts "
            "WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        )
        return int(before)

    def delete_for_marketplace(self, marketplace_id: str) -> int:
        """Drop all subscriptions for every plugin in a marketplace.

        Called from ``DELETE /api/marketplaces/{id}`` cleanup path.
        Returns count of rows deleted.
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts WHERE marketplace_id = ?",
            [marketplace_id],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM user_plugin_optouts WHERE marketplace_id = ?",
            [marketplace_id],
        )
        return int(before)

    def fanout_system_for_plugin(
        self, marketplace_id: str, plugin_name: str,
    ) -> int:
        """Subscribe every existing user to ``(marketplace_id, plugin_name)``.

        Counterpart to ``fanout_system_for_user`` — this side picks one
        plugin and walks every user, that side picks one user and walks
        every system plugin. Both go through the same
        ``user_plugin_optouts`` PK + ``ON CONFLICT DO NOTHING`` so they
        compose freely with the user/group-create hooks.

        Returns the count of NEW subscriptions written (delta of
        before/after row counts) so the admin endpoint can report
        ``affected_users`` honestly — re-running on an already-marked
        plugin returns 0 instead of misleadingly reporting "every user".
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts "
            "WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO user_plugin_optouts
               (user_id, marketplace_id, plugin_name)
               SELECT id, ?, ? FROM users
               ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING""",
            [marketplace_id, plugin_name],
        )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM user_plugin_optouts "
            "WHERE marketplace_id = ? AND plugin_name = ?",
            [marketplace_id, plugin_name],
        ).fetchone()[0]
        return max(0, int(after) - int(before))

    def fanout_system_for_user(self, user_id: str) -> None:
        """Subscribe ``user_id`` to every active system marketplace_plugin.

        Only plugins with ``is_system=TRUE`` and ``admin_disabled=FALSE`` are
        selected — a disabled plugin stays hidden from new-user fanout even if
        a row somehow still carries the system flag. Symmetric with
        ``ResourceGrants.fanout_system_for_group``.

        Idempotent — the table's PRIMARY KEY ``(user_id, marketplace_id,
        plugin_name)`` plus ``ON CONFLICT … DO NOTHING`` keeps existing
        subscriptions untouched.

        Called from the user-create hooks (Google OAuth, magic-link,
        admin-create, scheduler token) so a new user lands in the mandatory
        tier without an admin reconcile — it subscribes *one* user to *every*
        active system plugin. (The admin ``mark_system`` endpoint fans a single
        plugin out to all users via ``fanout_system_for_plugin``, not this
        helper.)
        """
        self.conn.execute(
            """INSERT INTO user_plugin_optouts
               (user_id, marketplace_id, plugin_name)
               SELECT ?, marketplace_id, name
               FROM marketplace_plugins WHERE is_system = TRUE AND admin_disabled = FALSE
               ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING""",
            [user_id],
        )
