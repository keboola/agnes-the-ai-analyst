"""Migration task implementations for DuckDB → Postgres.

The generic copy loop in ``__init__.py`` handles every table in
``Base.metadata.sorted_tables`` via :class:`GenericCopyTask`, which does:

  SELECT * FROM <source>  →  INSERT … ON CONFLICT DO NOTHING  →  SHA-256 validate

:data:`EXPLICIT_TASKS` overrides the generic path for tables that need
per-row work (type coercion, NULL backfill, FTS rebuild, etc.).  Currently
all tables are handled generically — JSON/JSONB coercion is covered by the
:data:`_JSON_COLUMNS` allowlist used inside ``_build_insert``.  If a future
table genuinely requires a custom step, add an override class here and
register it in :data:`EXPLICIT_TASKS`.

Adding a new table that does NOT need per-row work: register the model in
``src/models/`` — the generic loop picks it up automatically through
``Base.metadata.sorted_tables``.  The test
``tests/db_pg/test_data_migration.py::test_every_pg_model_has_a_migration_task``
ensures no model goes uncovered.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import duckdb
import sqlalchemy as sa
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON/JSONB column registry
# ---------------------------------------------------------------------------

# (table_name, column_name) pairs that must be CAST(... AS JSONB) in PG.
# DuckDB returns these as Python dict/list; psycopg needs them serialised
# to text before the cast.
_JSON_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("audit_log", "params"),
        ("audit_log", "params_before"),
        ("metric_definitions", "sql_variants"),
        ("metric_definitions", "validation"),
        ("bq_metadata_cache", "clustered_by"),
        ("bq_metadata_cache", "known_columns"),
        ("user_sync_settings", "tables"),
        ("table_profiles", "profile"),
        ("user_observability_views", "query_json"),
        ("usage_events", "friction_tags"),
        ("marketplace_plugins", "source_spec"),
        ("marketplace_plugins", "raw"),
        ("marketplace_plugins", "doc_links"),
        ("store_entities", "doc_paths"),
        ("store_entities", "version_history"),
        ("store_submissions", "inline_checks"),
        ("store_submissions", "llm_findings"),
        ("knowledge_items", "tags"),
        ("knowledge_items", "contributors"),
        ("knowledge_items", "entities"),
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolved_columns(
    table_name: str, duck_conn: duckdb.DuckDBPyConnection
) -> List[str]:
    """Return ordered column list for *table_name* from DuckDB information_schema."""
    rows = duck_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{table_name}' AND table_schema = 'main' "
        "ORDER BY ordinal_position"
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    # Fallback: PRAGMA (DuckDB extension tables may not appear in
    # information_schema on all versions)
    pragma = duck_conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return [r[1] for r in pragma]


def _build_insert(
    target_table: str,
    columns: Sequence[str],
    pk_columns: Sequence[str],
) -> str:
    """Return a parameterised INSERT … ON CONFLICT (pk…) DO NOTHING statement.

    Columns listed in :data:`_JSON_COLUMNS` are wrapped in
    ``CAST(:col AS JSONB)`` so DuckDB-native dict/list values are coerced
    correctly in Postgres.

    The ``pk_columns`` argument must be the table's primary-key column list so
    that the ``ON CONFLICT`` clause targets only that constraint.  Using a bare
    ``ON CONFLICT DO NOTHING`` would silently suppress conflicts on *any*
    unique constraint (e.g. a ``UNIQUE(email)`` index), masking data-corruption
    that would otherwise surface as a uniqueness error on the second row.
    """
    placeholders: List[str] = []
    for c in columns:
        if (target_table, c) in _JSON_COLUMNS:
            placeholders.append(f"CAST(:{c} AS JSONB)")
        else:
            placeholders.append(f":{c}")
    col_list = ", ".join(columns)
    val_list = ", ".join(placeholders)
    pk_target = ", ".join(pk_columns)
    return (
        f"INSERT INTO {target_table} ({col_list}) "
        f"VALUES ({val_list}) "
        f"ON CONFLICT ({pk_target}) DO NOTHING"
    )


def _normalize_for_pg(value: Any) -> Any:
    """Serialise DuckDB-native dict/list to JSON text for PG CAST."""
    import json as _json
    if isinstance(value, (dict, list)):
        return _json.dumps(value)
    return value


def _checksum(values: Sequence[Sequence[Any]]) -> str:
    """Stable SHA-256 digest over a sorted list of PK tuples."""
    h = hashlib.sha256()
    for v in sorted(tuple(str(x) for x in row) for row in values):
        for item in v:
            h.update(item.encode("utf-8"))
            h.update(b"|")
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GenericCopyTask
# ---------------------------------------------------------------------------

@dataclass
class GenericCopyTask:
    """SELECT * → INSERT … ON CONFLICT (pk…) DO NOTHING + SHA-256 validate.

    Default handler for any table that does not require per-row work.
    Reads every column from DuckDB, batch-inserts into PG with
    ``ON CONFLICT (pk_columns) DO NOTHING``, and records PK-set + row-count
    for post-copy validation.

    Attributes:
        table_name:  DuckDB source table name (identical to PG target).
        pk_columns:  PK columns used by :func:`validate_task`.  When
                     empty, validation falls back to the ``id`` column.
        batch_size:  Rows per INSERT batch.
    """
    table_name: str
    pk_columns: List[str] = field(default_factory=lambda: ["id"])
    batch_size: int = 500

    # Source == target for all current tables.
    @property
    def source_table(self) -> str:  # noqa: D401
        return self.table_name

    @property
    def target_table(self) -> str:  # noqa: D401
        return self.table_name

    def run(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        pg_engine: Engine,
        *,
        dry_run: bool = False,
    ) -> int:
        """Copy rows from DuckDB to PG.  Returns number of rows considered."""
        columns = _resolved_columns(self.table_name, duck_conn)
        log.info(
            "migrate %s (%d cols, dry_run=%s)", self.table_name, len(columns), dry_run
        )

        select_sql = f"SELECT {', '.join(columns)} FROM {self.source_table}"
        rows = duck_conn.execute(select_sql).fetchall()
        if not rows:
            log.info("  empty source; nothing to do")
            return 0

        insert_sql = _build_insert(self.target_table, columns, self.pk_columns)
        considered = 0
        batch: List[Dict[str, Any]] = []
        for r in rows:
            d = {k: _normalize_for_pg(v) for k, v in zip(columns, r)}
            batch.append(d)
            considered += 1
            if len(batch) >= self.batch_size and not dry_run:
                with pg_engine.begin() as conn:
                    conn.execute(sa.text(insert_sql), batch)
                batch.clear()
        if batch and not dry_run:
            with pg_engine.begin() as conn:
                conn.execute(sa.text(insert_sql), batch)

        log.info("  considered %d rows%s", considered, " (dry-run)" if dry_run else "")
        return considered

    def validate(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        pg_engine: Engine,
    ) -> Dict[str, Any]:
        """Compare PK-set checksums + row counts between DuckDB and PG."""
        pk_select = ", ".join(self.pk_columns)
        duck_rows = duck_conn.execute(
            f"SELECT {pk_select} FROM {self.source_table}"
        ).fetchall()
        duck_count = len(duck_rows)

        with pg_engine.connect() as conn:
            pg_rows = conn.execute(
                sa.text(f"SELECT {pk_select} FROM {self.target_table}")
            ).all()
        pg_count = len(pg_rows)

        return {
            "table": self.target_table,
            "duckdb_rows": duck_count,
            "pg_rows": pg_count,
            "checksum_match": (
                duck_count == pg_count
                and _checksum(duck_rows) == _checksum(pg_rows)
            ),
        }


# ---------------------------------------------------------------------------
# Explicit per-table overrides
# ---------------------------------------------------------------------------
#
# All 39 tables are currently handled generically.  JSON/JSONB coercion is
# covered by _JSON_COLUMNS above; there are no enum casts, NULL backfills,
# or FTS rebuilds required for the DuckDB → PG migration path:
#
#   - audit_log: _audit_log_transform was a no-op; JSONB is in _JSON_COLUMNS.
#   - usage_events: is_error has DEFAULT FALSE in DuckDB → no NULLs in prod.
#   - personal_access_tokens: token_hash is plain VARCHAR in both stores.
#   - knowledge_items: PG schema has no tsvector column (FTS is DuckDB-only).
#   - store_submissions: status is String, not a PG enum.
#
# If a future table genuinely requires a custom step, add a dataclass here
# (implementing .run() and .validate()) and register it below.

EXPLICIT_TASKS: Dict[str, GenericCopyTask] = {}
