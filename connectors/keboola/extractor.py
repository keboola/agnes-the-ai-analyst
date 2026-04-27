"""Keboola extractor — produces extract.duckdb + data/*.parquet using DuckDB Keboola extension."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import duckdb

from src.identifier_validation import (
    is_safe_identifier,
    is_safe_quoted_identifier,
    validate_identifier,
    validate_quoted_identifier,
)

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


def _create_remote_attach_table(
    conn: duckdb.DuckDBPyConnection, keboola_url: str
) -> None:
    """Write _remote_attach so orchestrator can re-ATTACH the Keboola extension."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["kbc", "keboola", keboola_url, "KEBOOLA_STORAGE_TOKEN"],
    )


def _try_attach_extension(conn: duckdb.DuckDBPyConnection, keboola_url: str, keboola_token: str) -> bool:
    """Try to install and attach the Keboola DuckDB extension. Returns True on success."""
    try:
        conn.execute("INSTALL keboola FROM community; LOAD keboola;")
        escaped_token = keboola_token.replace("'", "''")
        conn.execute(f"ATTACH '{keboola_url}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
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

    # Write to temp file then rename — avoids lock conflict with orchestrator
    # which may hold a read lock on the existing extract.duckdb
    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()
    conn = duckdb.connect(str(tmp_db_path))

    stats = {"tables_extracted": 0, "tables_failed": 0, "errors": []}
    now = datetime.now(timezone.utc)

    try:
        # Try DuckDB Keboola extension
        use_extension = _try_attach_extension(conn, keboola_url, keboola_token)

        _create_meta_table(conn)

        has_remote = any(tc.get("query_mode") == "remote" for tc in table_configs)
        if has_remote and use_extension:
            _create_remote_attach_table(conn, keboola_url)

        for tc in table_configs:
            table_name = tc["name"]
            query_mode = tc.get("query_mode", "local")

            # #81 Group D — refuse rows whose identifiers don't pass the
            # whitelist. The registry is admin-controlled but anyone with
            # write access can otherwise inject SQL via the CREATE VIEW /
            # COPY / SELECT interpolation below. Skip-and-continue rather
            # than crashing the whole extraction; valid rows still process.
            if not validate_identifier(table_name, "Keboola table_name"):
                stats["tables_failed"] += 1
                stats["errors"].append(
                    {"table": table_name, "error": "unsafe identifier"}
                )
                continue

            if query_mode == "remote":
                # Create view pointing to kbc extension (requires re-ATTACH at query time)
                bucket = tc.get("bucket", "")
                source_table = tc.get("source_table", table_name)
                if not (validate_quoted_identifier(bucket, "Keboola bucket") and
                        validate_quoted_identifier(source_table, "Keboola source_table")):
                    stats["tables_failed"] += 1
                    stats["errors"].append(
                        {"table": table_name, "error": "unsafe bucket/source_table"}
                    )
                    continue
                if use_extension and bucket:
                    conn.execute(
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f'SELECT * FROM kbc."{bucket}"."{source_table}"'
                    )
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

                # Get row count and file size. pq_path is built from the
                # validated table_name above, but escape the parquet path
                # literal for defense-in-depth.
                safe_pq_lit = pq_path.replace("'", "''")
                rows = conn.execute(
                    f"SELECT count(*) FROM read_parquet('{safe_pq_lit}')"
                ).fetchone()[0]
                size = os.path.getsize(pq_path)

                # Create view and register in _meta
                conn.execute(
                    f'CREATE OR REPLACE VIEW "{table_name}" AS '
                    f'SELECT * FROM read_parquet(\'{safe_pq_lit}\')'
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
        conn.execute("CHECKPOINT")
        conn.close()

    # Atomic replace: swap temp DB into place, cleaning up any WAL files
    import shutil
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))

    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


def _extract_via_extension(
    conn: duckdb.DuckDBPyConnection, tc: Dict[str, Any], pq_path: str
) -> None:
    """Extract a table using the DuckDB Keboola extension."""
    bucket = tc.get("bucket", "")
    source_table = tc.get("source_table", tc["name"])
    # #81 Group D — defense-in-depth. The caller already validates these;
    # refuse here too in case a future caller forgets. Use the relaxed
    # quoted-identifier check that accepts Keboola's `in.c-foo` form.
    if not (is_safe_quoted_identifier(bucket) and is_safe_quoted_identifier(source_table)):
        raise ValueError(
            f"unsafe bucket/source_table: {bucket!r}/{source_table!r}"
        )
    safe_pq_lit = pq_path.replace("'", "''")
    conn.execute(
        f'COPY (SELECT * FROM kbc."{bucket}"."{source_table}") '
        f'TO \'{safe_pq_lit}\' (FORMAT PARQUET)'
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
        # Construct full Keboola table ID: bucket.source_table (e.g., in.c-finance.circle)
        bucket = tc.get("bucket", "")
        source_table = tc.get("source_table", tc["name"])
        table_id = f"{bucket}.{source_table}" if bucket else tc.get("id", tc["name"])
        client.export_table(table_id, Path(csv_path))

        # Convert CSV to Parquet using DuckDB — all_varchar avoids type inference errors
        # (e.g. columns with mostly numeric values but some strings like "Non-Manager")
        conv_conn = duckdb.connect()
        conv_conn.execute(f"COPY (SELECT * FROM read_csv('{csv_path}', all_varchar=true)) TO '{pq_path}' (FORMAT PARQUET)")
        conv_conn.close()
    finally:
        if os.path.exists(csv_path):
            os.unlink(csv_path)


if __name__ == "__main__":
    """Standalone: reads config from env + table_registry, runs extraction.

    Used by sync trigger subprocess. Reads KEBOOLA_STORAGE_TOKEN and
    KEBOOLA_STACK_URL from environment, table list from DuckDB registry.
    """
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s: %(message)s")

    # Read Keboola credentials — env first, then instance.yaml fallback
    url = os.environ.get("KEBOOLA_STACK_URL", "")
    token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

    if not url or not token:
        try:
            from config.loader import load_instance_config
            config = load_instance_config()
            kbc_config = config.get("keboola", {})
            url = url or kbc_config.get("url", "")
            token_env = kbc_config.get("token_env", "KEBOOLA_STORAGE_TOKEN")
            token = token or os.environ.get(token_env, "")
        except Exception:
            pass

    if not url or not token:
        logger.error("Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN")
        exit(1)

    # Read table list from registry
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    sys_conn = get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("keboola")
    finally:
        sys_conn.close()

    if not tables:
        logger.warning("No Keboola tables registered in table_registry")
        exit(0)

    logger.info("Extracting %d tables from %s", len(tables), url)
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    result = run(str(data_dir / "extracts" / "keboola"), tables, url, token)
    logger.info("Extraction complete: %s", result)

    failed = result.get("tables_failed", 0)
    exit(1 if failed == len(tables) else 0)  # exit 1 only if ALL tables failed
