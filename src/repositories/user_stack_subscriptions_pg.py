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

    def subscribe(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
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

    def unsubscribe(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
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

    def is_subscribed(
        self, user_id: str, resource_type: str, resource_id: str
    ) -> bool:
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

    def list_users_subscribed_to(
        self, resource_type: str, resource_id: str
    ) -> List[str]:
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
