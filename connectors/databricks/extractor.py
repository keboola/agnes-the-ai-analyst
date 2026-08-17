"""Databricks extractor — SQL-warehouse materialization into local parquet.

This module owns ``query_mode='materialized'``: the scheduler's materialized
pass (``app/api/sync.py``) runs each registered row's SQL on the configured
Databricks SQL warehouse via the Statement Execution API and writes the result
to ``$DATA_DIR/extracts/databricks/data/<table_id>.parquet``, registering it in
``extracts/databricks/extract.duckdb`` (``_meta`` + inner view) so the
orchestrator's standard rebuild publishes a master view — the same
distribution path (manifest + ``agnes pull``) every other materialized row
rides.

The connector's other two modes live elsewhere:

- ``query_mode='remote'`` — nothing syncs; the analyst's statement ships to
  the warehouse per query (``connectors/databricks/remote.py``, reached from
  ``app/api/query.py``). Optionally also attachable into DuckDB via
  ``connectors/databricks/{attach,extract_init}.py``.
- ``query_mode='local'`` — rejected at register time. There is no Databricks
  extractor subprocess; a full-table pull is expressed as a materialized row
  instead (the register route server-generates ``SELECT * FROM
  <catalog>.<schema>.<table>`` when ``source_query`` is omitted).

Cost guardrail: the Statement Execution API has no dry-run primitive (unlike
BigQuery), so ``max_bytes`` caps the *result* size instead of the scanned
bytes — the API stops producing past the cap and flags the manifest
``truncated``, which this module converts into ``MaterializeBudgetError``
(shared vocabulary with the BQ path; ``_run_materialized_pass`` already
renders its structured fields). A truncated result is never written as data.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

# Shared materialized-pass vocabulary: app/api/sync.py catches these classes
# around every connector branch, so raising the same types keeps the
# structured cap logging + skip semantics uniform across BQ and Databricks.
from connectors.bigquery.extractor import MaterializeBudgetError
from connectors.databricks.client import ArrowResult, DatabricksStatementClient
from src.identifier_validation import validate_identifier
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

# Conservative grammar for catalog/schema/table segments the server-generated
# full-table SQL interpolates (inside backticks). Databricks accepts wilder
# names via backtick quoting, but a registry row can always carry an explicit
# ``source_query`` for those — the derived path stays strict so a hostile or
# typo'd segment fails fast instead of escaping the quoting.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _escape_sql_string_literal(s: str) -> str:
    """Double single quotes for embedding inside a '...' SQL literal."""
    return s.replace("'", "''")


def _pin_session_utc(conn):
    """Same inline pin as the other extractors — see
    ``src.duckdb_conn._open_duckdb`` for the rationale."""
    try:
        conn.execute("SET GLOBAL TimeZone='UTC'")
    except duckdb.Error:
        pass
    return conn


def full_table_sql(catalog: str, schema: str, table: str) -> str:
    """Databricks-native full-table dump SQL from validated segments."""
    for label, segment in (("catalog", catalog), ("schema", schema), ("table", table)):
        if not segment or not _SAFE_SEGMENT_RE.match(segment):
            raise ValueError(
                f"unsafe databricks {label} segment: {segment!r} (use an explicit source_query for exotic names)"
            )
    return f"SELECT * FROM `{catalog}`.`{schema}`.`{table}`"


def split_bucket(bucket: str, default_catalog: str) -> tuple[str, str]:
    """Resolve a registry ``bucket`` to ``(catalog, schema)``.

    ``bucket`` is the schema within the configured default catalog; a dotted
    ``catalog.schema`` bucket overrides the catalog per row.
    """
    if "." in bucket:
        head, _, tail = bucket.partition(".")
        return head, tail
    return default_catalog, bucket


# ---------------------------------------------------------------------------
# Arrow → parquet
# ---------------------------------------------------------------------------

_EMPTY_SCHEMA_TYPE_MAP = {
    "BOOLEAN": "bool_",
    "BYTE": "int8",
    "SHORT": "int16",
    "INT": "int32",
    "LONG": "int64",
    "FLOAT": "float32",
    "DOUBLE": "float64",
    "DATE": "date32",
    "STRING": "string",
}


def _empty_arrow_schema(schema_columns: List[Dict[str, Any]]):
    """Build a pyarrow schema from the statement manifest so a legitimately
    empty result still lands as a typed, viewable parquet. Types outside the
    small map fall back to string — for a 0-row file the payload type only
    has to be readable, not perfectly faithful."""
    import pyarrow as pa

    fields = []
    for col in schema_columns:
        name = col.get("name") or f"col_{col.get('position', len(fields))}"
        type_name = (col.get("type_name") or "").upper()
        if type_name == "TIMESTAMP":
            pa_type = pa.timestamp("us", tz="UTC")
        else:
            pa_type = getattr(pa, _EMPTY_SCHEMA_TYPE_MAP.get(type_name, "string"))()
        fields.append(pa.field(name, pa_type))
    if not fields:
        fields = [pa.field("empty_result", pa.string())]
    return pa.schema(fields)


def _write_batches_to_parquet(result: ArrowResult, tmp_path: Path) -> int:
    """Stream the result's record batches into ``tmp_path``; returns rows."""
    import pyarrow.parquet as pq

    rows = 0
    writer = None
    try:
        for batch in result.iter_batches():
            if writer is None:
                writer = pq.ParquetWriter(str(tmp_path), batch.schema)
            writer.write_batch(batch)
            rows += batch.num_rows
        if writer is None:
            # Zero chunks — write a typed empty parquet from the manifest
            # schema so the inner view still resolves.
            import pyarrow as pa

            schema = _empty_arrow_schema(result.schema_columns)
            writer = pq.ParquetWriter(str(tmp_path), schema)
            writer.write_table(pa.Table.from_batches([], schema=schema))
    finally:
        if writer is not None:
            writer.close()
    return rows


# ---------------------------------------------------------------------------
# extract.duckdb registration
# ---------------------------------------------------------------------------


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent ``_meta`` per the extract.duckdb contract."""
    conn.execute("""CREATE TABLE IF NOT EXISTS _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'materialized'
    )""")


def _register_materialized_parquet(
    extract_db_path: Path,
    table_id: str,
    parquet_path: Path,
    rows: int,
    size_bytes: int,
) -> None:
    """Write the parquet's ``_meta`` row + inner view into ``extract.duckdb``
    so the orchestrator's master-view rebuild picks it up.

    Unlike the BigQuery sibling (which must not create the file, because the
    BQ extractor subprocess owns it and remote rows live inside), Databricks
    has no extractor subprocess — the materialized pass is the only writer —
    so a missing ``extract.duckdb`` is created here on first materialize.

    Fail-soft: the parquet is the canonical artifact; a registration failure
    logs loudly and the next materialize pass retries.
    """
    safe_path = _escape_sql_string_literal(str(parquet_path))
    try:
        extract_db_path.parent.mkdir(parents=True, exist_ok=True)
        with _pin_session_utc(duckdb.connect(str(extract_db_path), read_only=False)) as conn:
            _ensure_meta_table(conn)
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
                conn.execute(
                    "INSERT INTO _meta VALUES (?, '', ?, ?, CURRENT_TIMESTAMP, 'materialized')",
                    [table_id, rows, size_bytes],
                )
                conn.execute(
                    f"CREATE OR REPLACE VIEW {quote_ident(table_id)} AS SELECT * FROM read_parquet('{safe_path}')"
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
    except Exception as e:
        logger.warning(
            "databricks materialize: failed to register %s in %s — parquet at %s "
            "is fine, master view will appear after the next materialize pass. Error: %s",
            table_id,
            extract_db_path,
            parquet_path,
            e,
        )


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def materialize_query(
    table_id: str,
    *,
    client: DatabricksStatementClient,
    output_dir: str,
    source_query: Optional[str] = None,
    catalog: Optional[str] = None,
    bucket: Optional[str] = None,
    source_table: Optional[str] = None,
    max_bytes: Optional[int] = None,
    statement_timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run a registry row's SQL on the Databricks SQL warehouse and write the
    result to ``<output_dir>/data/<table_id>.parquet`` atomically.

    ``source_query`` is the admin-registered Databricks-native SQL (this is
    where semantic-layer queries live: ``SELECT dim, MEASURE(m) FROM
    <metric_view> GROUP BY dim`` — only a warehouse can evaluate
    ``MEASURE()``). When omitted, a full-table dump is derived from
    ``bucket``/``source_table`` (+ the configured default ``catalog``);
    the register route normally pre-generates that SQL, so the derivation
    here is a fallback for legacy rows.

    Concurrency: no per-table lock layer, matching the Keboola materialize
    path (the BigQuery one carries an in-process + advisory-file lock because
    an admin *registration* can trigger an immediate materialize there, racing
    a scheduler tick; no Databricks path does that). Two materializes of the
    same table cannot overlap system-wide: the only caller is
    ``_run_materialized_pass``, reached exclusively through the
    ``data-refresh`` job, and every enqueue of that job — scheduler tick,
    admin trigger, or a ``tables=[…]`` / ``?source=`` scoped trigger — shares
    the idempotency key ``"sync"``, which ``JobsRepository.enqueue`` dedups
    while one is queued/running. (``_run_sync``'s ``_sync_lock`` only adds
    in-process exclusion on top; on a role-split deployment the job dedup is
    what actually holds.) The atomic tmp → ``os.replace`` swap keeps readers
    consistent regardless, and ``extract.duckdb`` registration is fail-soft —
    the parquet is durable before it is attempted, and the orchestrator's
    filesystem-fallback scan republishes the row on a later rebuild.

    Returns ``{"rows", "size_bytes", "hash", "query_mode": "materialized"}``.

    Raises:
        ValueError: unsafe ``table_id`` / derived segments, or no way to
            build the SQL (no source_query and no bucket+source_table).
        MaterializeBudgetError: the API flagged the result truncated at
            ``max_bytes`` — nothing is written.
        DatabricksApiError / DatabricksStatementTimeoutError: statement or
            transport failure (caller aggregates per row).
    """
    if not validate_identifier(table_id, "materialize table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")

    sql = (source_query or "").strip()
    if not sql:
        if not (bucket and source_table):
            raise ValueError(
                f"databricks materialized row {table_id!r} has no source_query and no bucket+source_table to derive one"
            )
        row_catalog, row_schema = split_bucket(bucket, catalog or "")
        sql = full_table_sql(row_catalog, row_schema, source_table)

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir / f"{table_id}.parquet"
    tmp_path = data_dir / f"{table_id}.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    effective_timeout = statement_timeout_s if statement_timeout_s and statement_timeout_s > 0 else 900.0
    result = client.execute_to_arrow_batches(
        sql,
        byte_limit=max_bytes if max_bytes and max_bytes > 0 else None,
        timeout_s=effective_timeout,
    )
    if result.truncated:
        raise MaterializeBudgetError(
            f"Databricks result for table {table_id!r} exceeded the "
            f"{max_bytes:,}-byte materialize cap (result truncated by the API; nothing written)",
            table_id=table_id,
            current=result.total_byte_count or (max_bytes or 0),
            limit=max_bytes or 0,
        )

    try:
        rows = _write_batches_to_parquet(result, tmp_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    h = hashlib.md5()
    with open(tmp_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    parquet_hash = h.hexdigest()
    size_bytes = tmp_path.stat().st_size
    os.replace(tmp_path, parquet_path)

    _register_materialized_parquet(
        extract_db_path=out_path / "extract.duckdb",
        table_id=table_id,
        parquet_path=parquet_path,
        rows=rows,
        size_bytes=size_bytes,
    )

    if rows == 0:
        logger.warning(
            "Materialized %s produced 0 rows — verify the SQL filter is intentional. Parquet written: %s",
            table_id,
            parquet_path,
        )

    return {
        "rows": int(rows),
        "size_bytes": size_bytes,
        "query_mode": "materialized",
        "hash": parquet_hash,
    }
