"""Repository for the persistent BigQuery metadata cache.

Backs the v40 ``bq_metadata_cache`` table. Reads are called from the
hot path (``/api/v2/catalog``); writes only from the scheduler-driven
refresh job in ``app/api/bq_metadata_refresh.py`` and from operator-
triggered single-row refreshes via ``/api/v2/metadata-cache/refresh``.

clustered_by is stored as a JSON array of column-name strings and
returned to callers as a list (decoded here, never raw JSON).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb


def _decode_clustered_by(stored: Any) -> Optional[list[str]]:
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


def _row_to_dict(conn: duckdb.DuckDBPyConnection, row: tuple) -> dict[str, Any]:
    columns = [desc[0] for desc in conn.description]
    out: dict[str, Any] = dict(zip(columns, row))
    out["clustered_by"] = _decode_clustered_by(out.get("clustered_by"))
    return out


class BqMetadataCacheRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self, table_id: str) -> Optional[dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM bq_metadata_cache WHERE table_id = ?",
            [table_id],
        ).fetchone()
        if not result:
            return None
        return _row_to_dict(self.conn, result)

    def list_all(self) -> list[dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM bq_metadata_cache ORDER BY table_id"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        out: list[dict[str, Any]] = []
        for r in results:
            row = dict(zip(columns, r))
            row["clustered_by"] = _decode_clustered_by(row.get("clustered_by"))
            out.append(row)
        return out

    def upsert_success(
        self,
        table_id: str,
        *,
        rows: Optional[int],
        size_bytes: Optional[int],
        partition_by: Optional[str],
        clustered_by: Optional[list[str]],
    ) -> None:
        """Record a successful refresh. Clears any prior error_at/error_msg."""
        now = datetime.now(timezone.utc)
        clustered_json = (
            json.dumps(list(clustered_by)) if clustered_by is not None else None
        )
        self.conn.execute(
            """INSERT INTO bq_metadata_cache
                (table_id, rows, size_bytes, partition_by, clustered_by,
                 refreshed_at, error_at, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT (table_id) DO UPDATE SET
                rows         = excluded.rows,
                size_bytes   = excluded.size_bytes,
                partition_by = excluded.partition_by,
                clustered_by = excluded.clustered_by,
                refreshed_at = excluded.refreshed_at,
                error_at     = NULL,
                error_msg    = NULL""",
            [table_id, rows, size_bytes, partition_by, clustered_json, now],
        )

    def mark_error(self, table_id: str, error_msg: str) -> None:
        """Record a failed refresh. Preserves the prior success row (if any)
        so analyst Claude keeps using last-known-good rows + size_bytes while
        the next scheduled retry attempts to recover."""
        now = datetime.now(timezone.utc)
        truncated = (error_msg or "")[:512]  # bound storage
        self.conn.execute(
            """INSERT INTO bq_metadata_cache
                (table_id, rows, size_bytes, partition_by, clustered_by,
                 refreshed_at, error_at, error_msg)
            VALUES (?, NULL, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                error_at  = excluded.error_at,
                error_msg = excluded.error_msg""",
            [table_id, now, truncated],
        )

    def delete(self, table_id: str) -> None:
        """Drop a row — used by admin endpoints when a table is unregistered."""
        self.conn.execute(
            "DELETE FROM bq_metadata_cache WHERE table_id = ?", [table_id]
        )
