"""Repository for ``user_journey_state`` (v92).

Per-user onboarding "journey" state — the backend foundation for the
chat-driven onboarding flow. Mirrors the prototype's ``JOURNEY_DEFAULT``
shape: a handful of booleans tracking which onboarding milestones a user
has hit, plus a running count of successful answers.

Modeled on ``src/repositories/user_stack_subscriptions.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import duckdb

#: Canonical default state — returned verbatim when a user has no row yet.
JOURNEY_DEFAULT: Dict[str, Any] = {
    "first_asked": False,
    "stack_setup_done": False,
    "explored_stack": False,
    "catalog_discovered": False,
    "use_anywhere": False,
    "onboarded": False,
    "successful_answers": 0,
}

_BOOL_FIELDS = (
    "first_asked",
    "stack_setup_done",
    "explored_stack",
    "catalog_discovered",
    "use_anywhere",
    "onboarded",
)
_INT_FIELDS = ("successful_answers",)
_ALL_FIELDS = _BOOL_FIELDS + _INT_FIELDS


class UserJourneyRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self, user_id: str) -> Dict[str, Any]:
        """Return the user's journey state, or the defaults if no row exists."""
        row = self.conn.execute(
            "SELECT first_asked, stack_setup_done, explored_stack, "
            "catalog_discovered, use_anywhere, onboarded, successful_answers "
            "FROM user_journey_state WHERE user_id = ?",
            [user_id],
        ).fetchone()
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
        """Partial upsert — only the passed fields change; unknown keys are
        rejected. Returns the resulting full state."""
        unknown = set(fields) - set(_ALL_FIELDS)
        if unknown:
            raise ValueError(f"Unknown journey field(s): {sorted(unknown)}")

        current = self.get(user_id)
        current.update(fields)

        self.conn.execute(
            "INSERT INTO user_journey_state "
            "(user_id, first_asked, stack_setup_done, explored_stack, "
            "catalog_discovered, use_anywhere, onboarded, successful_answers, "
            "updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "first_asked = EXCLUDED.first_asked, "
            "stack_setup_done = EXCLUDED.stack_setup_done, "
            "explored_stack = EXCLUDED.explored_stack, "
            "catalog_discovered = EXCLUDED.catalog_discovered, "
            "use_anywhere = EXCLUDED.use_anywhere, "
            "onboarded = EXCLUDED.onboarded, "
            "successful_answers = EXCLUDED.successful_answers, "
            "updated_at = now()",
            [
                user_id,
                current["first_asked"],
                current["stack_setup_done"],
                current["explored_stack"],
                current["catalog_discovered"],
                current["use_anywhere"],
                current["onboarded"],
                current["successful_answers"],
            ],
        )
        return current

    def reset(self, user_id: str) -> None:
        """Drop the user's journey row, reverting them to defaults."""
        self.conn.execute("DELETE FROM user_journey_state WHERE user_id = ?", [user_id])
