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

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError
from app.instance_config import get_value
from src.sql_safe import (
    validate_identifier as _validate_identifier,
    validate_project_id as _validate_project_id,
)
from src.identifier_validation import validate_identifier, validate_quoted_identifier

logger = logging.getLogger(__name__)


def _detect_table_type(
    conn: duckdb.DuckDBPyConnection,
    project: str,
    dataset: str,
    table: str,
) -> str | None:
    """Return BQ entity type for `project.dataset.table`.

    Uses `bigquery_query()` table function which routes through the BQ jobs
    API — works on tables, views, and materialized views alike. Returns the
    value of INFORMATION_SCHEMA.TABLES.table_type ('BASE TABLE', 'VIEW',
    'MATERIALIZED_VIEW') or None if not found.
    """
    bq_sql = (
        f"SELECT table_type FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
        f"WHERE table_name = ? LIMIT 1"
    )
    # Parameter-bind project (1st arg of bigquery_query), the inner BQ SQL
    # (2nd arg), and the table-name predicate. This avoids the nested-quote
    # bug where inline `'{table}'` would close the outer `bigquery_query('...')`
    # string. Note: bigquery_query forwards extra positional args as BQ query
    # parameters, bound positionally to the `?` placeholders inside `bq_sql`.
    duck_sql = "SELECT * FROM bigquery_query(?, ?, ?)"
    row = conn.execute(duck_sql, [project, bq_sql, table]).fetchone()
    return row[0] if row else None


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
    """Write _remote_attach. token_env is empty for BQ — orchestrator
    detects extension='bigquery' and refreshes the token from GCE metadata
    on its own."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
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

    Authenticates via the GCE metadata server. For each registered table,
    detects whether the BQ entity is a BASE TABLE or VIEW, and emits a
    DuckDB view that uses the appropriate path:
      - BASE TABLE → direct ATTACH ref (Storage Read API, fast for full scans)
      - VIEW       → bigquery_query() table function (jobs API, supports views)

    Args:
        output_dir: Path to write extract.duckdb
        project_id: GCP project ID for billing/job execution
        table_configs: List of table config dicts from table_registry

    Returns:
        Dict with stats: {tables_registered: int, errors: list}
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    stats: Dict[str, Any] = {"tables_registered": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Validate project_id before any work — no point opening DB or fetching a
    # token if the value is structurally bogus and would only break SQL later.
    if not _validate_project_id(project_id):
        msg = f"unsafe BQ project_id: {project_id!r}"
        logger.error(msg)
        stats["errors"].append({"table": "<config>", "error": msg})
        return stats

    # Fetch token before opening DB so failure aborts cleanly without partial file
    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        logger.error("BQ metadata auth failed: %s", e)
        stats["errors"].append({"table": "<auth>", "error": str(e)})
        return stats

    conn = duckdb.connect(str(tmp_db_path))
    try:
        # Install and load BigQuery extension
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            # session-scoped DuckDB secret with the metadata token
            escaped_token = token.replace("'", "''")
            conn.execute(
                f"CREATE SECRET bq_session (TYPE bigquery, ACCESS_TOKEN '{escaped_token}')"
            )
            conn.execute(
                f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)"
            )
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
            dataset = tc.get("bucket", "")
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
            if not validate_identifier(table_name, "BigQuery table_name"):
                stats["errors"].append({"table": table_name, "error": f"unsafe table_name: {table_name!r}"})
                continue
            if not validate_quoted_identifier(dataset, "BigQuery dataset"):
                stats["errors"].append({"table": table_name, "error": f"unsafe dataset: {dataset!r}"})
                continue
            if not validate_quoted_identifier(source_table, "BigQuery source_table"):
                stats["errors"].append({"table": table_name, "error": f"unsafe source_table: {source_table!r}"})
                continue

            try:
                entity_type = _detect_table_type(conn, project_id, dataset, source_table)
                if entity_type is None:
                    raise RuntimeError(
                        f"BQ entity {project_id}.{dataset}.{source_table} not found"
                    )

                legacy_wrap_views = bool(
                    get_value("data_source", "bigquery", "legacy_wrap_views", default=False)
                )

                if entity_type == "BASE TABLE":
                    # Storage Read API — fast for full scans
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f'SELECT * FROM bq."{dataset}"."{source_table}"'
                    )
                    conn.execute(view_sql)
                elif legacy_wrap_views:
                    # Backwards compatibility — for one release cycle only.
                    if entity_type not in ("VIEW", "MATERIALIZED_VIEW"):
                        logger.warning(
                            "Unknown BQ entity type %r for %s.%s.%s — using bigquery_query() path",
                            entity_type, project_id, dataset, source_table,
                        )
                    # VIEW or MATERIALIZED_VIEW — use jobs API
                    bq_inner = f"SELECT * FROM `{project_id}.{dataset}.{source_table}`"
                    bq_inner_escaped = bq_inner.replace("'", "''")
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
                    )
                    conn.execute(view_sql)
                else:
                    # Default: VIEW / MATERIALIZED_VIEW are recorded in _meta but no master
                    # view created. Analyst must use `da fetch` (v2 primitives) to materialise
                    # a snapshot locally.
                    logger.info(
                        "Skipping wrap view for %s entity %s.%s.%s — use `da fetch`",
                        entity_type, project_id, dataset, source_table,
                    )

                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_registered"] += 1
                logger.info(
                    "Registered remote view: %s -> %s.%s.%s (%s)",
                    table_name, project_id, dataset, source_table, entity_type,
                )
            except Exception as e:
                logger.error("Failed to register %s: %s", table_name, e)
                stats["errors"].append({"table": table_name, "error": str(e)})

        conn.execute("DETACH bq")
    finally:
        conn.close()

    # Atomic swap (preserve from existing implementation)
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
    import connectors.bigquery.extractor as _self
    from config.loader import load_instance_config
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    config = load_instance_config()
    bq_config = config.get("data_source", {}).get("bigquery", {})
    project_id = bq_config.get("project", "")

    if not project_id:
        logger.error(
            "data_source.bigquery.project missing from instance.yaml — "
            "cannot run extractor"
        )
        raise SystemExit(2)

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
        # Look up init_extract via the cached module (sys.modules) instead of
        # the fresh runpy namespace, so tests can monkey-patch it.
        result = _self.init_extract(
            str(data_dir / "extracts" / "bigquery"), project_id, tables
        )
        logger.info("BigQuery extract init complete: %s", result)
