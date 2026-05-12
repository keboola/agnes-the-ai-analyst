"""Repository for per-user saved views on /admin/activity.

A view is a `(user_id, name)` pair pointing at a JSON blob the UI fully
controls — see _v42_to_v43 in src/db.py for schema. The repo deliberately
treats query_json as opaque; the UI evolves faster than this layer.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import duckdb


class ObservabilityViewsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, name, query_json, created_at "
            "FROM user_observability_views "
            "WHERE user_id = ? ORDER BY created_at DESC",
            [user_id],
        ).fetchall()
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

    def create(self, user_id: str, name: str, query: dict[str, Any]) -> dict[str, Any]:
        view_id = str(uuid.uuid4())
        # ON CONFLICT (user_id, name) DO UPDATE so re-saving the same name
        # overwrites — matches the UX where the user picks a name once and
        # iterates on the query underneath.
        self.conn.execute(
            "INSERT INTO user_observability_views (id, user_id, name, query_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, name) DO UPDATE SET "
            "query_json = EXCLUDED.query_json",
            [view_id, user_id, name, json.dumps(query)],
        )
        # Re-read to return the canonical row (id may differ on upsert).
        row = self.conn.execute(
            "SELECT id, name, query_json, created_at "
            "FROM user_observability_views WHERE user_id = ? AND name = ?",
            [user_id, name],
        ).fetchone()
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
        # DuckDB's Python binding doesn't expose a reliable rowcount on
        # DELETE; check existence first so we can give the API layer a real
        # 404 vs. silent-no-op signal.
        exists = self.conn.execute(
            "SELECT 1 FROM user_observability_views WHERE id = ? AND user_id = ?",
            [view_id, user_id],
        ).fetchone()
        if not exists:
            return False
        self.conn.execute(
            "DELETE FROM user_observability_views WHERE id = ? AND user_id = ?",
            [view_id, user_id],
        )
        return True
