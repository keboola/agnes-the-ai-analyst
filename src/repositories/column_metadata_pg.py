"""Postgres-backed column metadata repository.

Mirrors ``src/repositories/column_metadata.py``. ``import_proposal`` is
intentionally NOT ported here — that helper is a backend-agnostic I/O
wrapper around ``save()`` and belongs in a shared file outside the
repository class. Callers should call ``save()`` directly until the
helper is moved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ColumnMetadataPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def save(
        self,
        table_id: str,
        column_name: str,
        basetype: Optional[str] = None,
        description: Optional[str] = None,
        confidence: str = "manual",
        source: str = "manual",
    ) -> dict:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO column_metadata
                       (table_id, column_name, basetype, description,
                        confidence, source, updated_at)
                       VALUES (:t, :c, :bt, :desc, :conf, :src, :now)
                       ON CONFLICT (table_id, column_name) DO UPDATE SET
                         basetype = EXCLUDED.basetype,
                         description = EXCLUDED.description,
                         confidence = EXCLUDED.confidence,
                         source = EXCLUDED.source,
                         updated_at = EXCLUDED.updated_at"""
                ),
                {
                    "t": table_id, "c": column_name, "bt": basetype,
                    "desc": description, "conf": confidence, "src": source,
                    "now": now,
                },
            )
        return self.get(table_id, column_name)  # type: ignore[return-value]

    def get(self, table_id: str, column_name: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT * FROM column_metadata WHERE table_id = :t AND column_name = :c"
                ),
                {"t": table_id, "c": column_name},
            ).mappings().first()
        return dict(row) if row else None

    def list_for_table(self, table_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM column_metadata WHERE table_id = :t ORDER BY column_name"
                ),
                {"t": table_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def delete(self, table_id: str, column_name: str) -> bool:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "DELETE FROM column_metadata "
                    "WHERE table_id = :t AND column_name = :c RETURNING 1"
                ),
                {"t": table_id, "c": column_name},
            ).first()
        return row is not None
