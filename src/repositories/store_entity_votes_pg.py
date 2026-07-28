"""Postgres-backed store_entity_votes repository (#398).

Mirrors ``src/repositories/store_entity_votes.py`` — per-user thumbs up/down
on store entities, one row per (entity, user).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class StoreEntityVotesPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def vote(self, entity_id: str, user_id: str, vote: int) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO store_entity_votes (entity_id, user_id, vote, voted_at)
                       VALUES (:e, :u, :v, :now)
                       ON CONFLICT (entity_id, user_id) DO UPDATE SET
                         vote = EXCLUDED.vote, voted_at = EXCLUDED.voted_at"""
                ),
                {"e": entity_id, "u": user_id, "v": vote, "now": now},
            )

    def unvote(self, entity_id: str, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM store_entity_votes "
                    "WHERE entity_id = :e AND user_id = :u"
                ),
                {"e": entity_id, "u": user_id},
            )

    def get_aggregate(
        self, entity_id: str, user_id: Optional[str] = None
    ) -> Dict[str, int]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT
                        COALESCE(SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END), 0) AS up,
                        COALESCE(SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END), 0) AS down
                       FROM store_entity_votes WHERE entity_id = :e"""
                ),
                {"e": entity_id},
            ).first()
            my_vote = 0
            if user_id is not None:
                mine = conn.execute(
                    sa.text(
                        "SELECT vote FROM store_entity_votes "
                        "WHERE entity_id = :e AND user_id = :u"
                    ),
                    {"e": entity_id, "u": user_id},
                ).first()
                if mine is not None and mine[0] is not None:
                    my_vote = int(mine[0])
        return {"up": int(row[0]), "down": int(row[1]), "my_vote": my_vote}

    def delete_all_for_entity(self, entity_id: str) -> int:
        with self._engine.begin() as conn:
            before = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM store_entity_votes WHERE entity_id = :e"
                ),
                {"e": entity_id},
            ).scalar()
            conn.execute(
                sa.text("DELETE FROM store_entity_votes WHERE entity_id = :e"),
                {"e": entity_id},
            )
        return int(before or 0)
