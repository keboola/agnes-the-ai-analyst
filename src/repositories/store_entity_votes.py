"""Repository for per-user thumbs up/down ratings on store entities (#398).

Composite PK ``(entity_id, user_id)`` — one vote per (entity, user). Mirrors
the ``knowledge_votes`` repo: ``vote`` upserts on conflict (a re-vote flips the
value), ``unvote`` clears the row, ``get_aggregate`` returns the up/down tallies
plus the caller's own vote.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb


class StoreEntityVotesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def vote(self, entity_id: str, user_id: str, vote: int) -> None:
        """Upsert the caller's vote (``1`` = up, ``-1`` = down). Re-voting
        replaces the prior value in place — one row per (entity, user)."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO store_entity_votes (entity_id, user_id, vote, voted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (entity_id, user_id) DO UPDATE SET
                vote = excluded.vote, voted_at = excluded.voted_at""",
            [entity_id, user_id, vote, now],
        )

    def unvote(self, entity_id: str, user_id: str) -> None:
        """Clear the caller's vote (the ``vote=0`` path). Idempotent."""
        self.conn.execute(
            "DELETE FROM store_entity_votes WHERE entity_id = ? AND user_id = ?",
            [entity_id, user_id],
        )

    def get_aggregate(
        self, entity_id: str, user_id: Optional[str] = None
    ) -> Dict[str, int]:
        """Return ``{up, down, my_vote}`` for an entity.

        ``up`` / ``down`` are the global tallies; ``my_vote`` is the caller's
        own vote (``1`` / ``-1``) or ``0`` if ``user_id`` is omitted or the
        caller has not voted.
        """
        row = self.conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) AS up,
                COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) AS down
            FROM store_entity_votes WHERE entity_id = ?""",
            [entity_id],
        ).fetchone()
        my_vote = 0
        if user_id is not None:
            mine = self.conn.execute(
                "SELECT vote FROM store_entity_votes "
                "WHERE entity_id = ? AND user_id = ?",
                [entity_id, user_id],
            ).fetchone()
            if mine and mine[0] is not None:
                my_vote = int(mine[0])
        return {"up": int(row[0]), "down": int(row[1]), "my_vote": my_vote}

    def delete_all_for_entity(self, entity_id: str) -> int:
        """Used by the entity-delete code path. Returns rows deleted."""
        before = self.conn.execute(
            "SELECT COUNT(*) FROM store_entity_votes WHERE entity_id = ?",
            [entity_id],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM store_entity_votes WHERE entity_id = ?",
            [entity_id],
        )
        return int(before)
