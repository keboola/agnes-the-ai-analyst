"""Postgres-backed observability views repository.

Mirrors ``src/repositories/observability_views.py``.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ObservabilityViewsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, name, query_json, created_at "
                    "FROM user_observability_views "
                    "WHERE user_id = :u ORDER BY created_at DESC"
                ),
                {"u": user_id},
            ).all()
        out: list[dict[str, Any]] = []
        for r in rows:
            query = r[2]
            if isinstance(query, str):
                try:
                    query = json.loads(query)
                except (ValueError, TypeError):
                    pass
            out.append({
                "id": r[0],
                "name": r[1],
                "query": query,
                "created_at": r[3].isoformat() if r[3] else None,
            })
        return out

    def count_for_user(self, user_id: str) -> int:
        """Number of saved views for a user — backs the per-user view cap."""
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM user_observability_views WHERE user_id = :u"),
                    {"u": user_id},
                ).scalar()
                or 0
            )

    def name_exists(self, user_id: str, name: str) -> bool:
        """True iff this (user_id, name) view already exists (upsert target)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT 1 FROM user_observability_views WHERE user_id = :u AND name = :n"),
                {"u": user_id, "n": name},
            ).first()
        return row is not None

    def create(self, user_id: str, name: str, query: dict[str, Any]) -> dict[str, Any]:
        view_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_observability_views (id, user_id, name, query_json) "
                    "VALUES (:id, :u, :n, CAST(:q AS JSONB)) "
                    "ON CONFLICT (user_id, name) DO UPDATE SET "
                    "  query_json = EXCLUDED.query_json"
                ),
                {"id": view_id, "u": user_id, "n": name, "q": json.dumps(query)},
            )
            row = conn.execute(
                sa.text(
                    "SELECT id, name, query_json, created_at "
                    "FROM user_observability_views WHERE user_id = :u AND name = :n"
                ),
                {"u": user_id, "n": name},
            ).first()
        q = row[2]
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except (ValueError, TypeError):
                pass
        return {
            "id": row[0],
            "name": row[1],
            "query": q,
            "created_at": row[3].isoformat() if row[3] else None,
        }

    def delete(self, user_id: str, view_id: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "DELETE FROM user_observability_views "
                    "WHERE id = :id AND user_id = :u RETURNING 1"
                ),
                {"id": view_id, "u": user_id},
            ).first()
        return row is not None
