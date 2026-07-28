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


def _decode_string_list(stored: Any) -> Optional[list[str]]:
    """Decode a JSON-array-of-strings column back into a Python list.

    Shared by clustered_by and known_columns — both store
    ``["col_a", "col_b"]``-shaped JSON. Tolerates lists (already decoded
    by DuckDB) and JSON strings (round-tripped from disk).
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


# Backwards-compat alias used in tests written against the old name.
_decode_clustered_by = _decode_string_list


def _row_to_dict(conn: duckdb.DuckDBPyConnection, row: tuple) -> dict[str, Any]:
    columns = [desc[0] for desc in conn.description]
    out: dict[str, Any] = dict(zip(columns, row))
    out["clustered_by"] = _decode_string_list(out.get("clustered_by"))
    out["known_columns"] = _decode_string_list(out.get("known_columns"))
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
            row["clustered_by"] = _decode_string_list(row.get("clustered_by"))
            row["known_columns"] = _decode_string_list(row.get("known_columns"))
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
        entity_type: Optional[str] = None,
        known_columns: Optional[list[str]] = None,
    ) -> None:
        """Record a successful refresh. Clears any prior error_at/error_msg.

        ``entity_type`` is the BigQuery ``INFORMATION_SCHEMA.TABLES.table_type``
        (``BASE TABLE`` / ``VIEW`` / ``MATERIALIZED VIEW`` / …). Catalog uses
        it to (a) hide rows/size_bytes for views (where __TABLES__ returns
        0 and the value is misleading) and (b) inject a "VIEW: LIMIT doesn't
        push" hint into cost-guard errors.

        ``known_columns`` is the list of column names from the refresh's
        ``fetch_bq_columns_full`` call — stored so the catalog endpoint can
        filter its generic ``where_examples`` templates against the table's
        real schema instead of advertising columns the table doesn't have.
        """
        now = datetime.now(timezone.utc)
        clustered_json = (
            json.dumps(list(clustered_by)) if clustered_by is not None else None
        )
        known_columns_json = (
            json.dumps(list(known_columns)) if known_columns is not None else None
        )
        self.conn.execute(
            """INSERT INTO bq_metadata_cache
                (table_id, rows, size_bytes, partition_by, clustered_by,
                 entity_type, known_columns,
                 refreshed_at, error_at, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT (table_id) DO UPDATE SET
                rows          = excluded.rows,
                size_bytes    = excluded.size_bytes,
                partition_by  = excluded.partition_by,
                clustered_by  = excluded.clustered_by,
                entity_type   = excluded.entity_type,
                known_columns = excluded.known_columns,
                refreshed_at  = excluded.refreshed_at,
                error_at      = NULL,
                error_msg     = NULL""",
            [
                table_id, rows, size_bytes, partition_by, clustered_json,
                entity_type, known_columns_json, now,
            ],
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
                 entity_type, known_columns,
                 refreshed_at, error_at, error_msg)
            VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
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
