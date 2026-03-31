"""Keboola extractor — produces extract.duckdb + data/*.parquet using DuckDB Keboola extension."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import duckdb

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
        query_mode VARCHAR DEFAULT 'local'
    )""")


def _try_attach_extension(conn: duckdb.DuckDBPyConnection, keboola_url: str, keboola_token: str) -> bool:
    """Try to install and attach the Keboola DuckDB extension. Returns True on success."""
    try:
        conn.execute("INSTALL keboola FROM community; LOAD keboola;")
        conn.execute(f"ATTACH '{keboola_url}' AS kbc (TYPE keboola, TOKEN '{keboola_token}')")
        logger.info("Using DuckDB Keboola extension")
        return True
    except Exception as e:
        logger.warning("Keboola extension unavailable (%s), falling back to legacy client", e)
        return False


def run(output_dir: str, table_configs: List[Dict[str, Any]], keboola_url: str, keboola_token: str) -> Dict[str, Any]:
    """Extract tables from Keboola into output_dir using DuckDB extension.

    Args:
        output_dir: Path to write extract.duckdb + data/
        table_configs: List of table config dicts from table_registry
        keboola_url: Keboola stack URL
        keboola_token: Keboola Storage API token

    Returns:
        Dict with extraction stats: {tables_extracted: int, tables_failed: int, errors: list}
    """
    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    conn = duckdb.connect(str(db_path))

    stats = {"tables_extracted": 0, "tables_failed": 0, "errors": []}
    now = datetime.now(timezone.utc)

    try:
        # Try DuckDB Keboola extension
        use_extension = _try_attach_extension(conn, keboola_url, keboola_token)

        _create_meta_table(conn)

        for tc in table_configs:
            table_name = tc["name"]
            query_mode = tc.get("query_mode", "local")

            if query_mode == "remote":
                # Register in _meta but don't download
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_extracted"] += 1
                continue

            try:
                pq_path = str(data_dir / f"{table_name}.parquet")

                if use_extension:
                    _extract_via_extension(conn, tc, pq_path)
                else:
                    _extract_via_legacy(tc, pq_path, keboola_url, keboola_token)

                # Get row count and file size
                rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
                size = os.path.getsize(pq_path)

                # Create view and register in _meta
                conn.execute(
                    f'CREATE OR REPLACE VIEW "{table_name}" AS SELECT * FROM read_parquet(\'{pq_path}\')'
                )
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                    [table_name, tc.get("description", ""), rows, size, now],
                )
                stats["tables_extracted"] += 1
                logger.info("Extracted %s: %d rows, %d bytes", table_name, rows, size)

            except Exception as e:
                logger.error("Failed to extract %s: %s", table_name, e)
                stats["tables_failed"] += 1
                stats["errors"].append({"table": table_name, "error": str(e)})

        # Detach Keboola if extension was used
        if use_extension:
            try:
                conn.execute("DETACH kbc")
            except Exception:
                pass

    finally:
        conn.close()

    return stats


def _extract_via_extension(
    conn: duckdb.DuckDBPyConnection, tc: Dict[str, Any], pq_path: str
) -> None:
    """Extract a table using the DuckDB Keboola extension."""
    bucket = tc.get("bucket", "")
    source_table = tc.get("source_table", tc["name"])
    conn.execute(
        f'COPY (SELECT * FROM kbc."{bucket}"."{source_table}") TO \'{pq_path}\' (FORMAT PARQUET)'
    )


def _extract_via_legacy(
    tc: Dict[str, Any], pq_path: str, keboola_url: str, keboola_token: str
) -> None:
    """Fallback: extract using legacy Keboola client (kbcstorage SDK)."""
    from connectors.keboola.client import KeboolaClient
    client = KeboolaClient(token=keboola_token, url=keboola_url)

    # Export to CSV temp file, then convert to parquet via DuckDB
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    try:
        table_id = tc.get("id", tc["name"])
        client.export_table(table_id, Path(csv_path))

        # Convert CSV to Parquet using DuckDB
        conv_conn = duckdb.connect()
        conv_conn.execute(f"COPY (SELECT * FROM read_csv_auto('{csv_path}')) TO '{pq_path}' (FORMAT PARQUET)")
        conv_conn.close()
    finally:
        if os.path.exists(csv_path):
            os.unlink(csv_path)


if __name__ == "__main__":
    """Standalone: reads config from instance.yaml + table_registry, runs extraction."""
    from config.loader import load_instance_config
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    config = load_instance_config()
    kbc_config = config.get("keboola", {})
    url = kbc_config.get("url", "")
    token = os.environ.get(kbc_config.get("token_env", "KEBOOLA_STORAGE_TOKEN"), "")

    sys_conn = get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("keboola")
    finally:
        sys_conn.close()

    if not tables:
        logger.warning("No Keboola tables registered in table_registry")
    else:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        result = run(str(data_dir / "extracts" / "keboola"), tables, url, token)
        logger.info("Extraction complete: %s", result)
