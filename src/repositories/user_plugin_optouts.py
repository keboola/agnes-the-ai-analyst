"""Repository for per-user opt-outs against admin-granted plugins.

Default behavior: a user receives every plugin admin has granted to any of
their groups. An opt-out row is the user's "I don't want this one" override —
``src/marketplace_filter.py:resolve_user_marketplace`` removes matching
entries from the served marketplace.

When admin removes the underlying grant, all opt-outs for that
``(marketplace_id, plugin_name)`` are dropped (see
``app/api/access.py``) so a re-grant starts clean. Per spec the user's
prior choice is **not** preserved across grant remove + re-add.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

import duckdb


class UserPluginOptoutsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def set(
        self,
        user_id: str,
        marketplace_id: str,
        plugin_name: str,
        opted_out: bool,
    ) -> None:
        """Idempotent toggle. ``opted_out=True`` upserts the row;
        ``opted_out=False`` deletes it.
        """
        if opted_out:
            self.conn.execute(
                "INSERT INTO user_plugin_optouts "
                "(user_id, marketplace_id, plugin_name) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, marketplace_id, plugin_name) DO NOTHING",
                [user_id, marketplace_id, plugin_name],
            )
        else:
            self.conn.execute(
                "DELETE FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
                [user_id, marketplace_id, plugin_name],
            )

    def is_opted_out(
        self, user_id: str, marketplace_id: str, plugin_name: str
    ) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = ? AND plugin_name = ?",
                [user_id, marketplace_id, plugin_name],
            ).fetchone()
        )

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
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
                "opted_out_at": r[2],
            }
            for r in rows
        ]

    def opted_out_set(self, user_id: str) -> Set[Tuple[str, str]]:
        """Return the user's opt-outs as a ``{(marketplace_id, plugin_name)}``
        set — the shape ``resolve_user_marketplace`` filters against.
        """
        rows = self.conn.execute(
            "SELECT marketplace_id, plugin_name FROM user_plugin_optouts "
            "WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return {(r[0], r[1]) for r in rows}

    def delete_for_plugin(
        self, marketplace_id: str, plugin_name: str
    ) -> int:
        """Drop all users' opt-outs for a given plugin.

        Called from the admin grant-delete code path so re-granting the same
        plugin restarts everyone at the default (enabled). Returns the count
        of rows deleted — used for audit telemetry.
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
