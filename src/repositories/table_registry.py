"""Repository for table registry."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict, Union

import duckdb


def _encode_primary_key(pk: Union[None, str, List[str]]) -> Optional[str]:
    """Serialize primary_key (list-or-string) to a canonical VARCHAR form.

    Frontend + API send lists (composite PKs are real — session-grain MSA
    tables key on `(session_id, event_date)` etc.). The schema column is
    VARCHAR for backwards compat, so we JSON-encode the list on write.
    Accepts a string for legacy CLI callers.
    """
    if pk is None or pk == "":
        return None
    if isinstance(pk, list):
        return json.dumps(pk) if pk else None
    if isinstance(pk, str):
        return json.dumps([pk])
    return json.dumps([str(pk)])


def _decode_primary_key(stored: Any) -> Optional[List[str]]:
    """Decode a registry-stored primary_key into the API-canonical list-of-str
    form. Tolerates four legacy representations:

    - None / empty string → None
    - JSON-array string `'["a","b"]'` (current canonical)
    - Comma-separated string `'a,b'` (legacy CLI input)
    - Python repr literal `"['a', 'b']"` (legacy bug — see #111)
    - Plain string `'a'` (legacy single-PK CLI input)
    """
    if stored is None or stored == "":
        return None
    if isinstance(stored, list):
        return [str(x) for x in stored if x]
    if not isinstance(stored, str):
        return [str(stored)]
    s = stored.strip()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x) for x in v if x]
        except json.JSONDecodeError:
            # Python repr legacy: `"['a', 'b']"` (single-quoted)
            try:
                import ast
                v = ast.literal_eval(s)
                if isinstance(v, list):
                    return [str(x) for x in v if x]
            except Exception:
                pass
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [s]


class TableRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def register(
        self, id: str, name: str, folder: Optional[str] = None,
        sync_strategy: Optional[str] = None,
        primary_key: Union[None, str, List[str]] = None,
        description: Optional[str] = None, registered_by: Optional[str] = None,
        source_type: Optional[str] = None, bucket: Optional[str] = None,
        source_table: Optional[str] = None,
        source_query: Optional[str] = None,
        query_mode: str = "local",
        sync_schedule: Optional[str] = None, profile_after_sync: bool = True,
        registered_at: Optional[datetime] = None,
    ) -> None:
        # `registered_at` defaults to "now" for fresh inserts. Updaters that
        # want to preserve the original registration time across edits pass
        # the existing value explicitly — otherwise PUT /api/admin/registry/{id}
        # would silently reset the timestamp on every edit (issue #130).
        ts = registered_at or datetime.now(timezone.utc)
        encoded_pk = _encode_primary_key(primary_key)
        self.conn.execute(
            """INSERT INTO table_registry (id, name, folder, sync_strategy,
                primary_key, description, registered_by, registered_at,
                source_type, bucket, source_table, source_query, query_mode,
                sync_schedule, profile_after_sync)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy, primary_key = excluded.primary_key,
                description = excluded.description, registered_at = excluded.registered_at,
                source_type = excluded.source_type, bucket = excluded.bucket,
                source_table = excluded.source_table, source_query = excluded.source_query,
                query_mode = excluded.query_mode,
                sync_schedule = excluded.sync_schedule,
                profile_after_sync = excluded.profile_after_sync""",
            [id, name, folder, sync_strategy, encoded_pk, description, registered_by, ts,
             source_type, bucket, source_table, source_query, query_mode,
             sync_schedule, profile_after_sync],
        )

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Apply JSON-decoding to fields stored as canonical VARCHAR."""
        if "primary_key" in row_dict:
            row_dict["primary_key"] = _decode_primary_key(row_dict["primary_key"])
        return row_dict

    def unregister(self, table_id: str) -> None:
        self.conn.execute("DELETE FROM table_registry WHERE id = ?", [table_id])

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM table_registry WHERE id = ?", [table_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode_row(dict(zip(columns, result)))

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM table_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]

    def list_by_source(self, source_type: str) -> List[Dict[str, Any]]:
        """List tables for a given source type (keboola, bigquery, jira, etc.)."""
        results = self.conn.execute(
            "SELECT * FROM table_registry WHERE source_type = ? ORDER BY name",
            [source_type],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]

    def find_by_bq_path(
        self, bucket: str, source_table: str,
    ) -> Optional[Dict[str, Any]]:
        """Look up a BigQuery row by `(bucket, source_table)`.

        Used by /api/query's RBAC patch to decide whether a direct
        `bq."<dataset>"."<source_table>"` reference in user SQL points at a
        registered row. If no row matches, the caller has bypassed the
        registry — the request is rejected before execute.

        Match is case-insensitive on `bucket` and `source_table`. NULL values
        in either column are excluded so a legacy NULL-bucket row never
        masks a legitimate non-NULL lookup.

        When 2+ rows match (no UNIQUE constraint on the
        (source_type, bucket, source_table) triple — admins can register a
        BQ table twice with different ids/names), return the oldest by
        `registered_at` so callers see deterministic resolution.
        """
        result = self.conn.execute(
            """SELECT * FROM table_registry
            WHERE source_type = 'bigquery'
              AND bucket IS NOT NULL
              AND source_table IS NOT NULL
              AND lower(bucket) = lower(?)
              AND lower(source_table) = lower(?)
            ORDER BY registered_at ASC
            LIMIT 1""",
            [bucket, source_table],
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode_row(dict(zip(columns, result)))

    def list_local(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tables with query_mode='local' (data downloaded to parquet)."""
        if source_type:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' AND source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' ORDER BY name",
            ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]
