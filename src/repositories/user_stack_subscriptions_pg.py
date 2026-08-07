"""Postgres-backed repository for ``user_stack_subscriptions``.

Mirrors ``src/repositories/user_stack_subscriptions.py`` (the DuckDB impl)
on the ``UserStackSubscriptionsRepository`` public surface. Cross-engine
parity will be covered by ``tests/db_pg/test_user_stack_subscriptions_contract.py``
(Task 1D.5).

Implementation differences vs. DuckDB:

- ``subscribe`` / ``unsubscribe`` use ``rowcount`` rather than a pre-SELECT
  race window: ``INSERT ... ON CONFLICT (user_id, resource_type, resource_id)
  DO NOTHING`` for subscribe, plain DELETE for unsubscribe.
- No JSON columns / no soft-delete on this entity — pure association table.
"""

from __future__ import annotations

from typing import List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class UserStackSubscriptionsPgRepository:
    """Postgres twin of ``UserStackSubscriptionsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def subscribe(self, user_id: str, resource_type: str, resource_id: str) -> bool:
        """Insert one row. Returns True iff the row is new.

        Idempotent — the table's composite PK + ``ON CONFLICT DO NOTHING``
        absorbs duplicate calls. ``resource_type`` is one of
        ``'data_package'`` / ``'memory_domain'`` (string verbatim — the
        ``ResourceType`` enum is the source of truth for valid values).
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    """
                    INSERT INTO user_stack_subscriptions
                      (user_id, resource_type, resource_id)
                    VALUES (:user_id, :resource_type, :resource_id)
                    ON CONFLICT (user_id, resource_type, resource_id)
                    DO NOTHING
                    """
                ),
                {
                    "user_id": user_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
            return (result.rowcount or 0) > 0

    def subscribe_group_members(self, group_id: str, resource_type: str, resource_id: str) -> int:
        """Subscribe every current member of ``group_id`` to (resource_type,
        resource_id). Returns the number of newly-created rows.

        Postgres twin of the DuckDB ``subscribe_group_members`` — soft-downgrade
        fan-out (v49). Idempotent via ON CONFLICT DO NOTHING. Before/after count
        (rather than ``rowcount``) keeps the return value identical to the
        DuckDB sibling for the contract test.
        """
        with self._engine.begin() as conn:
            before = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_stack_subscriptions WHERE resource_type = :rt AND resource_id = :ri"
                ),
                {"rt": resource_type, "ri": resource_id},
            ).scalar_one()
            conn.execute(
                sa.text(
                    """
                    INSERT INTO user_stack_subscriptions
                      (user_id, resource_type, resource_id)
                    SELECT m.user_id, :rt, :ri
                      FROM user_group_members m
                     WHERE m.group_id = :gid
                    ON CONFLICT (user_id, resource_type, resource_id)
                    DO NOTHING
                    """
                ),
                {"rt": resource_type, "ri": resource_id, "gid": group_id},
            )
            after = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM user_stack_subscriptions WHERE resource_type = :rt AND resource_id = :ri"
                ),
                {"rt": resource_type, "ri": resource_id},
            ).scalar_one()
        return int(after - before)

    def unsubscribe(self, user_id: str, resource_type: str, resource_id: str) -> bool:
        """Drop one row. Returns True iff a row was deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "DELETE FROM user_stack_subscriptions "
                    "WHERE user_id = :user_id "
                    "  AND resource_type = :resource_type "
                    "  AND resource_id = :resource_id"
                ),
                {
                    "user_id": user_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
            return (result.rowcount or 0) > 0

    def is_subscribed(self, user_id: str, resource_type: str, resource_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM user_stack_subscriptions "
                    "WHERE user_id = :user_id "
                    "  AND resource_type = :resource_type "
                    "  AND resource_id = :resource_id"
                ),
                {
                    "user_id": user_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ).first()
        return row is not None

    def list_for_user(self, user_id: str, resource_type: str) -> List[str]:
        """Resource ids the user is subscribed to within a single type.

        Newest-subscription-first ordering, matching the DuckDB sibling
        (``ORDER BY subscribed_at DESC``).
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT resource_id FROM user_stack_subscriptions "
                    "WHERE user_id = :user_id "
                    "  AND resource_type = :resource_type "
                    "ORDER BY subscribed_at DESC"
                ),
                {"user_id": user_id, "resource_type": resource_type},
            ).all()
        return [r[0] for r in rows]

    def list_for_user_with_dates(self, user_id: str) -> List[dict]:
        """Every subscription of the user, across resource types, with its
        ``subscribed_at`` timestamp — the "Added" metadata the My Stack
        inventory table renders. Newest first (matches the DuckDB sibling).
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT resource_type, resource_id, subscribed_at "
                    "FROM user_stack_subscriptions "
                    "WHERE user_id = :user_id "
                    "ORDER BY subscribed_at DESC"
                ),
                {"user_id": user_id},
            ).all()
        return [{"resource_type": r[0], "resource_id": r[1], "subscribed_at": r[2]} for r in rows]

    def list_users_subscribed_to(self, resource_type: str, resource_id: str) -> List[str]:
        """All users subscribed to a given (type, id).

        Distinct user_ids; ordering follows the DuckDB sibling (no
        explicit ORDER BY — natural index order on user_id).
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT DISTINCT user_id FROM user_stack_subscriptions "
                    "WHERE resource_type = :resource_type "
                    "  AND resource_id = :resource_id"
                ),
                {"resource_type": resource_type, "resource_id": resource_id},
            ).all()
        return [r[0] for r in rows]
