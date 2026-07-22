"""Postgres-backed repository for ``user_journey_state``.

Mirrors ``src/repositories/user_journey.py`` (the DuckDB impl) on the
``UserJourneyRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_user_journey_contract.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.user_journey import JOURNEY_DEFAULT, _ALL_FIELDS


class UserJourneyPgRepository:
    """Postgres twin of ``UserJourneyRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, user_id: str) -> Dict[str, Any]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT first_asked, stack_setup_done, explored_stack, "
                    "catalog_discovered, use_anywhere, onboarded, "
                    "successful_answers "
                    "FROM user_journey_state WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            ).first()
        if row is None:
            return dict(JOURNEY_DEFAULT)
        return {
            "first_asked": bool(row[0]),
            "stack_setup_done": bool(row[1]),
            "explored_stack": bool(row[2]),
            "catalog_discovered": bool(row[3]),
            "use_anywhere": bool(row[4]),
            "onboarded": bool(row[5]),
            "successful_answers": int(row[6]),
        }

    def update(self, user_id: str, **fields: Any) -> Dict[str, Any]:
        unknown = set(fields) - set(_ALL_FIELDS)
        if unknown:
            raise ValueError(f"Unknown journey field(s): {sorted(unknown)}")

        current = self.get(user_id)
        current.update(fields)

        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO user_journey_state
                      (user_id, first_asked, stack_setup_done, explored_stack,
                       catalog_discovered, use_anywhere, onboarded,
                       successful_answers, updated_at)
                    VALUES
                      (:user_id, :first_asked, :stack_setup_done, :explored_stack,
                       :catalog_discovered, :use_anywhere, :onboarded,
                       :successful_answers, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      first_asked = EXCLUDED.first_asked,
                      stack_setup_done = EXCLUDED.stack_setup_done,
                      explored_stack = EXCLUDED.explored_stack,
                      catalog_discovered = EXCLUDED.catalog_discovered,
                      use_anywhere = EXCLUDED.use_anywhere,
                      onboarded = EXCLUDED.onboarded,
                      successful_answers = EXCLUDED.successful_answers,
                      updated_at = now()
                    """
                ),
                {"user_id": user_id, **current},
            )
        return current

    def reset(self, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_journey_state WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
