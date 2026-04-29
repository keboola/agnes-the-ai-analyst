"""BigQuery extractor — produces extract.duckdb with remote views via DuckDB BigQuery extension.

No data is downloaded. All queries go directly to BigQuery via DuckDB extension ATTACH.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import duckdb

from src.identifier_validation import validate_identifier, validate_quoted_identifier

logger = logging.getLogger(__name__)


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
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            conn.execute(f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)")
            logger.info("Attached BigQuery project: %s", project_id)
        except Exception as attach_err:
            logger.error("Failed to attach BigQuery project %s: %s", project_id, attach_err)
            stats["errors"].append(
                {"table": "*", "error": f"BigQuery ATTACH failed: {attach_err}"}
            )
            # No tables can be registered without a working connection
            for tc in table_configs:
                stats["errors"].append(
                    {"table": tc["name"], "error": "skipped: BigQuery ATTACH failed"}
                )
            return stats

        _create_meta_table(conn)
        _create_remote_attach_table(conn, project_id)

        for tc in table_configs:
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
