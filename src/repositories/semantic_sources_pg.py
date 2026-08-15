"""Postgres-backed repository for ``semantic_sources``.

Mirrors ``src/repositories/semantic_sources.py`` (the DuckDB impl).
``config`` is a JSONB column; writes go through ``CAST(:config AS JSONB)``
with a ``json.dumps`` bind, reads come back as a native Python dict via
psycopg's adapter.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SemanticSourcesPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(row)
        v = d.get("config")
        if isinstance(v, str):
            try:
                d["config"] = json.loads(v) if v else None
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def create(
        self,
        *,
        id: str,
        kind: str,
        name: str,
        adapter: str,
        config: Dict[str, Any],
        enabled: bool = True,
    ) -> Dict[str, Any]:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO semantic_sources
                      (id, kind, name, adapter, config, enabled,
                       last_sync_at, last_sync_status, last_sync_error,
                       created_at, updated_at)
                    VALUES
                      (:id, :kind, :name, :adapter, CAST(:config AS JSONB), :enabled,
                       NULL, NULL, NULL, current_timestamp, current_timestamp)
                    """
                ),
                {
                    "id": id,
                    "kind": kind,
                    "name": name,
                    "adapter": adapter,
                    "config": json.dumps(config),
                    "enabled": enabled,
                },
            )
        return self.get(id)  # type: ignore[return-value]

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM semantic_sources WHERE id = :id"),
                    {"id": source_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_all(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM semantic_sources"
        if enabled_only:
            sql += " WHERE enabled = true"
        sql += " ORDER BY name"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql)).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def update(self, source_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(source_id)
        set_cols = []
        params: Dict[str, Any] = {"id": source_id}
        for col, val in fields.items():
            if col == "config":
                set_cols.append(f"{col} = CAST(:{col} AS JSONB)")
                params[col] = json.dumps(val)
            else:
                set_cols.append(f"{col} = :{col}")
                params[col] = val
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"UPDATE semantic_sources SET {', '.join(set_cols)}, updated_at = current_timestamp WHERE id = :id"
                ),
                params,
            )
        return self.get(source_id)

    def delete(self, source_id: str) -> bool:
        existed = self.get(source_id) is not None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM semantic_sources WHERE id = :id"),
                {"id": source_id},
            )
        return existed

    def record_sync(self, source_id: str, *, status: str, error: Optional[str]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE semantic_sources SET last_sync_at = current_timestamp, "
                    "last_sync_status = :status, last_sync_error = :error, "
                    "updated_at = current_timestamp WHERE id = :id"
                ),
                {"status": status, "error": error, "id": source_id},
            )
