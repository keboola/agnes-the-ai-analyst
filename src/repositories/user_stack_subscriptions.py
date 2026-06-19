"""Repository for ``user_stack_subscriptions`` (v49).

Generic per-user opt-in for resource_grants flagged ``requirement='available'``.
Currently scoped to ``data_package`` + ``memory_domain`` resource types —
Marketplace pluginy stay on the existing ``user_plugin_optouts`` shape per D1.

Mirrors ``src/repositories/user_curated_subscriptions.py`` but generic over
``resource_type`` (the marketplace one is hardcoded to plugins). The
``StackResolver`` service composes this with ``resource_grants`` to compute
the user's effective stack — see ``app/services/stack_resolver.py``.
"""
from __future__ import annotations

from typing import List

import duckdb


class UserStackSubscriptionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def subscribe(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
        """Insert one row. Returns True iff the row is new.

        Idempotent — the table's composite PK + ON CONFLICT DO NOTHING
        absorbs duplicate calls. ``resource_type`` is one of
        ``'data_package'`` / ``'memory_domain'`` (string verbatim — the
        ``ResourceType`` enum is the source of truth for valid values).
        """
        before = self.conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ? AND resource_id = ?",
            [user_id, resource_type, resource_id],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO user_stack_subscriptions"
            "(user_id, resource_type, resource_id) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [user_id, resource_type, resource_id],
        )
        return True

    def subscribe_group_members(
        self, group_id: str, resource_type: str, resource_id: str
    ) -> int:
        """Subscribe every current member of ``group_id`` to (resource_type,
        resource_id). Returns the number of newly-created rows.

        Soft-downgrade fan-out (v49): when a grant moves required → available
        the resource must stay in each member's stack, so we materialize a
        subscription row per member. Idempotent via ON CONFLICT DO NOTHING.
        Reproduces the old ``INSERT ... SELECT FROM user_group_members`` that
        used to run in the request handler on a raw DuckDB connection (#518).
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_stack_subscriptions "
            "WHERE resource_type = ? AND resource_id = ?",
            [resource_type, resource_id],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO user_stack_subscriptions"
            "(user_id, resource_type, resource_id) "
            "SELECT m.user_id, ?, ? FROM user_group_members m "
            "WHERE m.group_id = ? ON CONFLICT DO NOTHING",
            [resource_type, resource_id, group_id],
        )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM user_stack_subscriptions "
            "WHERE resource_type = ? AND resource_id = ?",
            [resource_type, resource_id],
        ).fetchone()[0]
        return int(after - before)

    def unsubscribe(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
        """Drop one row. Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ? AND resource_id = ?",
            [user_id, resource_type, resource_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ? AND resource_id = ?",
            [user_id, resource_type, resource_id],
        )
        return True

    def is_subscribed(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_stack_subscriptions "
                "WHERE user_id = ? AND resource_type = ? AND resource_id = ?",
                [user_id, resource_type, resource_id],
            ).fetchone()
        )

    def list_for_user(self, user_id: str, resource_type: str) -> List[str]:
        """Resource ids the user is subscribed to within a single type."""
        rows = self.conn.execute(
            "SELECT resource_id FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ? "
            "ORDER BY subscribed_at DESC",
            [user_id, resource_type],
        ).fetchall()
        return [r[0] for r in rows]

    def list_users_subscribed_to(
        self, resource_type: str, resource_id: str
    ) -> List[str]:
        """All users subscribed to a given (type, id). Distinct, no ordering
        guarantee beyond the underlying B-tree order on user_id."""
        rows = self.conn.execute(
            "SELECT DISTINCT user_id FROM user_stack_subscriptions "
            "WHERE resource_type = ? AND resource_id = ?",
            [resource_type, resource_id],
        ).fetchall()
        return [r[0] for r in rows]
