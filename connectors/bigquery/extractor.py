"""BigQuery extractor — produces extract.duckdb with remote views via DuckDB BigQuery extension.

No data is downloaded. All queries go directly to BigQuery via DuckDB extension ATTACH.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import duckdb

from src.identifier_validation import validate_identifier, validate_quoted_identifier

logger = logging.getLogger(__name__)


class MaterializeBudgetError(RuntimeError):
    """Raised when the BigQuery dry-run estimate exceeds the configured cap.

    The materialize trigger logs this and skips the row; the next scheduled
    tick re-tries (in case the underlying table size dropped or the operator
    raised the cap).
    """


def _dry_run_bytes(sql: str, project_id: str) -> int:
    """Estimate bytes scanned by `sql` against `project_id` via BigQuery
    dry-run. Returns 0 when the dry-run cannot be performed (library missing,
    auth failure, etc.) — caller treats 0 as "unknown" and does NOT trigger
    the cap (fail-open). Operators who want hard-fail should monitor for
    repeated 0-byte estimates as a signal the guardrail is degraded.
    """
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import QueryJobConfig
    except ImportError:
        return 0

    try:
        client = bigquery.Client(project=project_id)
        cfg = QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=cfg)
        return int(job.total_bytes_processed or 0)
    except Exception:
        return 0


def _create_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'remote'
    )""")


def _create_remote_attach_table(
    conn: duckdb.DuckDBPyConnection, project_id: str
) -> None:
    """Write _remote_attach so orchestrator can re-ATTACH the BigQuery extension."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    # BigQuery uses GOOGLE_APPLICATION_CREDENTIALS env var for auth automatically.
    # token_env is empty — orchestrator ATTACHes without TOKEN param.
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["bq", "bigquery", f"project={project_id}", ""],
    )


def init_extract(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
    *,
    skip_attach: bool = False,
) -> Dict[str, Any]:
    """Create extract.duckdb with remote views into BigQuery.

    Args:
        output_dir: Path to write extract.duckdb
        project_id: GCP project ID
        table_configs: List of table config dicts from table_registry

    Returns:
        Dict with stats: {tables_registered: int, errors: list}
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Write to temp file then rename — avoids lock conflict with orchestrator
    # which may hold a read lock on the existing extract.duckdb
    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()
    conn = duckdb.connect(str(tmp_db_path))

    stats = {"tables_registered": 0, "errors": []}
    now = datetime.now(timezone.utc)

    try:
        # Install and load BigQuery extension
        if not skip_attach:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            conn.execute(f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)")
            logger.info("Attached BigQuery project: %s", project_id)

        _create_meta_table(conn)
        _create_remote_attach_table(conn, project_id)

        for tc in table_configs:
            if tc.get("query_mode") == "materialized":
                # Materialized rows are handled by the sync trigger pass — they
                # write parquet files into /data/extracts/bigquery/data/, which
                # the orchestrator picks up via standard local-parquet discovery.
                # Don't create a remote view here (would shadow the parquet
                # via a cross-source name collision).
                continue
            table_name = tc["name"]
            dataset = tc.get("bucket", "")  # BigQuery dataset
            source_table = tc.get("source_table", table_name)

            # #81 Group D — refuse rows with unsafe identifiers. Same
            # rationale as the keboola extractor: registry is admin-controlled
            # but anyone with write access can otherwise inject SQL via the
            # CREATE VIEW interpolation below. Skip-and-continue.
            # `table_name` is the DuckDB view name in the master
            # analytics DB and the orchestrator uses the STRICT
            # validator there — accept the same constraint upstream
            # so a name with `-` or `.` fails fast in extraction
            # rather than getting silently dropped at rebuild time.
            # `dataset` and `source_table` are upstream-typed (BQ
            # naming) so use the relaxed validator for those.
            if not (validate_identifier(table_name, "BigQuery table_name") and
                    validate_quoted_identifier(dataset, "BigQuery dataset") and
                    validate_quoted_identifier(source_table, "BigQuery source_table")):
                stats["errors"].append(
                    {"table": table_name, "error": "unsafe identifier"}
                )
                continue

            try:
                conn.execute(
                    f'CREATE OR REPLACE VIEW "{table_name}" AS '
                    f'SELECT * FROM bq."{dataset}"."{source_table}"'
                )
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_registered"] += 1
                logger.info(
                    "Registered remote view: %s -> bq.%s.%s",
                    table_name, dataset, source_table,
                )
            except Exception as e:
                logger.error("Failed to register %s: %s", table_name, e)
                stats["errors"].append({"table": table_name, "error": str(e)})

        if not skip_attach:
            conn.execute("DETACH bq")
    finally:
        conn.close()

    # Atomic swap with WAL cleanup
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))

    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


def materialize_query(
    table_id: str,
    sql: str,
    project_id: str,
    output_dir: str,
    *,
    max_bytes: Optional[int] = None,
    skip_attach: bool = False,
) -> Dict[str, Any]:
    """Run an SQL query through the DuckDB BigQuery extension and write the
    result to a parquet file at `output_dir/data/{table_id}.parquet`.

    Atomic: writes to `<file>.tmp` first, swaps in via os.replace, deletes the
    tmp on failure so a half-written parquet never appears under the canonical
    name.

    Args:
        table_id: Logical id from table_registry; becomes the parquet filename.
            Must pass `validate_identifier()` so it cannot inject path traversal.
        sql: SELECT statement (no trailing semicolon). May reference
            `bq."dataset"."table"` after the BigQuery ATTACH.
        project_id: GCP project ID for the ATTACH.
        output_dir: connector root, e.g. `/data/extracts/bigquery`. Parquet
            lands in `<output_dir>/data/<table_id>.parquet`.
        max_bytes: Optional cap on BigQuery bytes scanned. When set, a
            dry-run runs first; if the estimate exceeds the cap, raises
            MaterializeBudgetError without touching disk. None disables
            the guardrail (default — preserves backwards compat).
        skip_attach: Test-only — skip INSTALL/LOAD/ATTACH so a stubbed schema
            can stand in. Never True in production.

    Returns:
        {"rows": int, "size_bytes": int, "query_mode": "materialized"}

    Raises:
        ValueError: if `table_id` is unsafe.
        MaterializeBudgetError: if `max_bytes` is set and dry-run exceeds it.
        duckdb.Error: if the COPY fails (e.g. bad SQL, missing table).
    """
    if not validate_identifier(table_id, "table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")

    if max_bytes is not None:
        estimated = _dry_run_bytes(sql, project_id)
        if estimated > max_bytes:
            raise MaterializeBudgetError(
                f"dry-run estimate {estimated:,} bytes exceeds cap "
                f"{max_bytes:,} for table {table_id!r}"
            )

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = data_dir / f"{table_id}.parquet"
    tmp_path = data_dir / f"{table_id}.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    # Throwaway in-memory connection so we never lock extract.duckdb.
    conn = duckdb.connect(":memory:")
    try:
        if not skip_attach:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            conn.execute(
                f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)"
            )

        safe_path = str(tmp_path).replace("'", "''")
        conn.execute(f"COPY ({sql}) TO '{safe_path}' (FORMAT PARQUET)")

        rows = conn.execute(
            f"SELECT count(*) FROM read_parquet('{safe_path}')"
        ).fetchone()[0]
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        conn.close()

    size_bytes = tmp_path.stat().st_size
    os.replace(tmp_path, parquet_path)

    return {"rows": int(rows), "size_bytes": size_bytes, "query_mode": "materialized"}


if __name__ == "__main__":
    """Standalone: reads config from instance.yaml + table_registry, creates extract."""
    from config.loader import load_instance_config
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    config = load_instance_config()
    bq_config = config.get("bigquery", {})
    project_id = bq_config.get("project_id", "")

    sys_conn = get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("bigquery")
    finally:
        sys_conn.close()

    if not tables:
        logger.warning("No BigQuery tables registered in table_registry")
    else:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        result = init_extract(
            str(data_dir / "extracts" / "bigquery"), project_id, tables
        )
        logger.info("BigQuery extract init complete: %s", result)
