"""Postgres-backed BigQuery metadata cache repository.

Mirrors ``src/repositories/bq_metadata_cache.py``. JSON columns are
JSONB in PG; psycopg returns them as Python dict/list directly, so the
``_decode_string_list`` helper is reused only for legacy-string
tolerance (which doesn't occur on fresh PG installs, but kept for
shared call-site behaviour).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def _decode_string_list(stored: Any) -> Optional[list[str]]:
    """Decode a JSON-array-of-strings column back into a Python list.

    JSONB columns come back as native Python lists; the JSON-string
    branch only matters for legacy rows imported from the DuckDB era
    where the column was TEXT.
    """
    if stored is None:
        return None
    if isinstance(stored, list):
        return [str(x) for x in stored]
    if isinstance(stored, str):
        try:
            parsed = json.loads(stored)
        except json.JSONDecodeError:
            return None
        return [str(x) for x in parsed] if isinstance(parsed, list) else None
    return None


class BqMetadataCachePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, table_id: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM bq_metadata_cache WHERE table_id = :t"),
                {"t": table_id},
            ).mappings().first()
        if not row:
            return None
        out = dict(row)
        out["clustered_by"] = _decode_string_list(out.get("clustered_by"))
        out["known_columns"] = _decode_string_list(out.get("known_columns"))
        return out

    def list_all(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM bq_metadata_cache ORDER BY table_id")
            ).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["clustered_by"] = _decode_string_list(d.get("clustered_by"))
            d["known_columns"] = _decode_string_list(d.get("known_columns"))
            out.append(d)
        return out

    def upsert_success(
        self,
        table_id: str,
        *,
        rows: Optional[int],
        size_bytes: Optional[int],
        partition_by: Optional[str],
        clustered_by: Optional[list[str]],
        entity_type: Optional[str] = None,
        known_columns: Optional[list[str]] = None,
    ) -> None:
        import json
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO bq_metadata_cache
                        (table_id, rows, size_bytes, partition_by, clustered_by,
                         entity_type, known_columns,
                         refreshed_at, error_at, error_msg)
                    VALUES (:tid, :rows, :sb, :pb, CAST(:cb AS JSONB),
                            :et, CAST(:kc AS JSONB),
                            :now, NULL, NULL)
                    ON CONFLICT (table_id) DO UPDATE SET
                        rows          = EXCLUDED.rows,
                        size_bytes    = EXCLUDED.size_bytes,
                        partition_by  = EXCLUDED.partition_by,
                        clustered_by  = EXCLUDED.clustered_by,
                        entity_type   = EXCLUDED.entity_type,
                        known_columns = EXCLUDED.known_columns,
                        refreshed_at  = EXCLUDED.refreshed_at,
                        error_at      = NULL,
                        error_msg     = NULL"""
                ),
                {
                    "tid": table_id, "rows": rows, "sb": size_bytes,
                    "pb": partition_by,
                    "cb": json.dumps(list(clustered_by)) if clustered_by is not None else None,
                    "et": entity_type,
                    "kc": json.dumps(list(known_columns)) if known_columns is not None else None,
                    "now": now,
                },
            )

    def mark_error(self, table_id: str, error_msg: str) -> None:
        now = datetime.now(timezone.utc)
        truncated = (error_msg or "")[:512]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO bq_metadata_cache
                        (table_id, rows, size_bytes, partition_by, clustered_by,
                         entity_type, known_columns,
                         refreshed_at, error_at, error_msg)
                    VALUES (:tid, NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now, :em)
                    ON CONFLICT (table_id) DO UPDATE SET
                        error_at  = EXCLUDED.error_at,
                        error_msg = EXCLUDED.error_msg"""
                ),
                {"tid": table_id, "now": now, "em": truncated},
            )

    def delete(self, table_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM bq_metadata_cache WHERE table_id = :t"),
                {"t": table_id},
            )
