"""Snowflake materialized extractor.

Runs admin-registered Snowflake SQL through DuckDB's ``snowflake`` community
extension and writes the result to a parquet under ``data/<table_id>.parquet``,
then registers the master view in ``extract.duckdb``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

import duckdb

from connectors.bigquery.extractor import MaterializeBudgetError
from connectors.snowflake.attach import attach_snowflake, build_remote_attach_url
from src.duckdb_conn import _open_duckdb
from src.identifier_validation import validate_identifier
from src.orchestrator_security import is_attach_host_allowed
from src.parquet_publish import atomic_publish_finalize, atomic_publish_temp_path
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

_SF_ALIAS = "sf"
_SF_EXTENSION = "snowflake"

# Snowflake identifiers (schema/table) and our view/table_id names.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# One in-process mutex per table_id, reused across calls.
_TABLE_LOCKS: dict[str, threading.Lock] = {}
_TABLE_LOCKS_MUTEX = threading.Lock()


def _get_table_lock(table_id: str) -> threading.Lock:
    with _TABLE_LOCKS_MUTEX:
        if table_id not in _TABLE_LOCKS:
            _TABLE_LOCKS[table_id] = threading.Lock()
        return _TABLE_LOCKS[table_id]





def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def split_bucket(bucket: str, default_database: str) -> tuple[str, str]:
    """Split ``bucket`` into ``(database, schema)``.

    A plain value is the schema in ``default_database``; a dotted value is
    ``database.schema``. No traversal characters are permitted.
    """
    if not bucket:
        if not default_database:
            raise ValueError("snowflake: no database to resolve against")
        return default_database, ""
    if ".." in bucket or "/" in bucket or "\\" in bucket:
        raise ValueError(f"unsafe snowflake bucket: {bucket!r}")
    if "." in bucket:
        database, _, schema = bucket.partition(".")
        return database, schema
    return default_database, bucket


def full_table_sql(schema: str, table: str) -> str:
    """Return ``SELECT * FROM sf."<schema>"."<table>"`` after validation."""
    for label, segment in (("schema", schema), ("table", table)):
        if not segment or not _SAFE_SEGMENT_RE.match(segment):
            raise ValueError(f"unsafe or empty snowflake {label}: {segment!r}")
    return f"SELECT * FROM {_quote_ident(_SF_ALIAS)}.{_quote_ident(schema)}.{_quote_ident(table)}"


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS _meta (
            table_name   VARCHAR NOT NULL,
            description  VARCHAR,
            rows         BIGINT,
            size_bytes   BIGINT,
            extracted_at TIMESTAMP,
            query_mode   VARCHAR DEFAULT 'local'
        )"""
    )


def _persist_materialized_inner_view(
    extract_db_path: Path,
    table_id: str,
    parquet_path: Path,
    rows: int,
    size_bytes: int,
) -> None:
    """Register the parquet and ``_meta`` row in the local ``extract.duckdb``."""
    safe_path = str(parquet_path).replace("'", "''")
    extract_db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _open_duckdb(str(extract_db_path), read_only=False)
    try:
        _ensure_meta_table(conn)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'materialized')",
                [table_id, "", rows, size_bytes],
            )
            conn.execute(f"CREATE OR REPLACE VIEW {quote_ident(table_id)} AS SELECT * FROM read_parquet('{safe_path}')")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


def materialize_query(
    table_id: str,
    *,
    output_dir: str,
    source_query: Optional[str] = None,
    database: Optional[str] = None,
    bucket: Optional[str] = None,
    source_table: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
    max_bytes: Optional[int] = None,
) -> dict[str, Any]:
    """Run ``source_query`` against Snowflake and write the result to parquet.

    Args:
        table_id: Registry ``name``; becomes parquet filename and view name.
        output_dir: Connector root, e.g. ``/data/extracts/snowflake``.
        source_query: Optional DuckDB-flavor SQL to run through the Snowflake
            extension. If omitted, ``bucket`` and ``source_table`` must be
            provided and a ``SELECT * FROM sf."<schema>"."<table>"`` query is
            generated.
        database: Default Snowflake database for the ATTACH.
        bucket: Snowflake schema (or ``database.schema`` to override ``database``).
        source_table: Snowflake table name.
        settings: Snowflake connection dict (account, user, password, database,
            warehouse, role).
        max_bytes: Optional cap on result size; a larger parquet is rejected.

    Returns:
        ``{"rows": int, "size_bytes": int, "query_mode": "materialized", "hash": str}``
    """
    if not validate_identifier(table_id, "materialize table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")
    if not settings:
        raise ValueError("snowflake settings required")

    required = ("account", "user", "database", "warehouse")
    missing = [k for k in required if not settings.get(k)]
    if missing:
        raise ValueError(f"snowflake settings incomplete: missing {', '.join(missing)}")

    credential = settings.get("password") or settings.get("private_key")
    if not credential:
        raise ValueError("snowflake settings incomplete: missing password or private_key")

    url = build_remote_attach_url(
        settings["account"],
        settings["database"],
        settings["warehouse"],
        settings["user"],
        settings.get("role") or "",
    )
    if not is_attach_host_allowed(url):
        raise ValueError(
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
            "refusing to send credential for materialize"
        )

    sql = (source_query or "").strip()
    if not sql:
        if not (bucket and source_table):
            raise ValueError("snowflake materialized requires source_query or bucket+source_table")
        row_database, schema = split_bucket(bucket, database or settings.get("database") or "")
        if not row_database:
            raise ValueError("snowflake: no database to resolve against")
        sql = full_table_sql(schema, source_table)

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = data_dir / f"{table_id}.parquet"
    # Per-process temp + chmod 0644 + os.replace, via the shared publish
    # protocol (#1359). A bare `os.replace` off a fixed `{table_id}.parquet.tmp`
    # was two bugs at once: the shared name let two writers replace each other's
    # in-flight file while the loser's cleanup deleted the winner's temp (#1274),
    # and skipping the chmod let the DuckDB COPY's own umask decide the published
    # mode — 0600 under a restrictive umask, so `agnes pull` could no longer read
    # it (#203). The two-step form (temp path here, `atomic_publish_finalize`
    # below) rather than the `with atomic_publish(...)` block, because the write
    # spans the COPY, a `finally` that tears down the DuckDB session, and a
    # budget check that deletes the temp instead of publishing it.
    tmp_path = atomic_publish_temp_path(parquet_path)
    tmp_db = out_path / f".tmp_materialize_{table_id}.duckdb"

    lock = _get_table_lock(table_id)
    if not lock.acquire(blocking=False):
        from connectors.bigquery.extractor import MaterializeInFlightError

        raise MaterializeInFlightError(table_id, layer="process")

    conn: Optional[duckdb.DuckDBPyConnection] = None
    try:
        if tmp_path.exists():
            tmp_path.unlink()

        conn = _open_duckdb(str(tmp_db), read_only=False)
        conn.execute(f"INSTALL {_SF_EXTENSION} FROM community")
        conn.execute(f"LOAD {_SF_EXTENSION}")

        credential = settings.get("password") or settings.get("private_key")
        attach_snowflake(
            conn,
            alias=_SF_ALIAS,
            url=url,
            token=credential,
            passphrase=settings.get("private_key_passphrase"),
        )

        safe_tmp = str(tmp_path).replace("'", "''")
        copy_sql = f"COPY ({sql}) TO '{safe_tmp}' (FORMAT PARQUET)"
        result = conn.execute(copy_sql)
        try:
            rows = result.fetchone()[0]
        except Exception:
            rows = None
    except BaseException:
        # Write-half cleanup is the caller's job under the publish protocol
        # (`atomic_publish_finalize` only owns the commit half). Needed now
        # that the temp name carries the pid: a failed COPY used to leave a
        # partial `{table_id}.parquet.tmp` that the NEXT run's pre-clean
        # removed, but a per-process name is never the next run's name, so
        # without this every failure strands one more file. Only ever this
        # process's own temp — never a glob — so a concurrent writer's
        # in-flight publish is untouched.
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            if tmp_db.exists():
                tmp_db.unlink()
        except Exception:
            pass
        lock.release()

    if not tmp_path.exists():
        raise RuntimeError(f"snowflake materialize produced no parquet for {table_id}")

    if max_bytes is not None and max_bytes > 0 and tmp_path.stat().st_size > max_bytes:
        size = tmp_path.stat().st_size
        tmp_path.unlink()
        raise MaterializeBudgetError(
            f"Snowflake result for table {table_id!r} exceeded the {max_bytes:,}-byte materialize cap",
            table_id=table_id,
            current=size,
            limit=max_bytes,
        )

    atomic_publish_finalize(tmp_path, parquet_path)

    size_bytes = parquet_path.stat().st_size

    if rows is None:
        try:
            import pyarrow.parquet as pq

            rows = pq.read_metadata(str(parquet_path)).num_rows
        except Exception:
            rows = 0
    rows = int(rows or 0)

    h = hashlib.md5()
    with open(parquet_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    parquet_hash = h.hexdigest()

    _persist_materialized_inner_view(out_path / "extract.duckdb", table_id, parquet_path, rows, size_bytes)

    if rows == 0:
        logger.warning("Snowflake materialized %s produced 0 rows", table_id)

    return {
        "rows": rows,
        "size_bytes": size_bytes,
        "query_mode": "materialized",
        "hash": parquet_hash,
    }
