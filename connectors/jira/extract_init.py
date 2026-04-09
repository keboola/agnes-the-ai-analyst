"""Initialize Jira extract.duckdb with _meta table and views for all entity types.

Called once on first webhook or manually via CLI. Creates the extract.duckdb
contract structure for the Jira connector.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

JIRA_TABLES = ["issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"]


def init_extract(output_dir: str | Path) -> None:
    """Create /data/extracts/jira/extract.duckdb with _meta and views.

    Views point to monthly parquet partitions in data/{table}/*.parquet.
    Safe to call multiple times — recreates _meta and views.
    """
    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    conn = duckdb.connect(str(db_path))

    try:
        # Create _meta table
        conn.execute("DROP TABLE IF EXISTS _meta")
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR NOT NULL,
            description VARCHAR,
            rows BIGINT,
            size_bytes BIGINT,
            extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")

        now = datetime.now(timezone.utc)
        for table_name in JIRA_TABLES:
            table_dir = data_dir / table_name
            table_dir.mkdir(exist_ok=True)

            # Create view only if parquet files exist (DuckDB glob fails on empty dirs)
            rows = 0
            size_bytes = 0
            parquets = list(table_dir.glob("*.parquet"))
            if parquets:
                glob_path = str(table_dir / "*.parquet")
                conn.execute(
                    f'CREATE OR REPLACE VIEW "{table_name}" AS '
                    f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true, hive_partitioning=false)"
                )
                try:
                    rows = conn.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0]
                    size_bytes = sum(f.stat().st_size for f in parquets)
                except Exception:
                    pass

            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                [table_name, f"Jira {table_name}", rows, size_bytes, now],
            )

        logger.info("Initialized Jira extract.duckdb at %s with %d tables", db_path, len(JIRA_TABLES))
    finally:
        conn.close()


def update_meta(output_dir: str | Path, table_name: str) -> None:
    """Update _meta entry for a table after parquet write.

    Called after incremental_transform writes/updates a parquet file.
    """
    output_path = Path(output_dir)
    db_path = output_path / "extract.duckdb"

    if not db_path.exists():
        init_extract(output_dir)
        return

    conn = duckdb.connect(str(db_path))
    try:
        table_dir = output_path / "data" / table_name
        parquets = list(table_dir.glob("*.parquet"))

        rows = 0
        size_bytes = 0
        if parquets:
            try:
                glob_path = str(table_dir / "*.parquet")
                # Recreate view to pick up new/changed parquet files
                conn.execute(
                    f'CREATE OR REPLACE VIEW "{table_name}" AS '
                    f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true, hive_partitioning=false)"
                )
                rows = conn.execute(f"SELECT count(*) FROM read_parquet('{glob_path}', union_by_name=true)").fetchone()[0]
                size_bytes = sum(f.stat().st_size for f in parquets)
            except Exception as e:
                logger.warning("Could not count rows for %s: %s", table_name, e)

        now = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE _meta SET rows = ?, size_bytes = ?, extracted_at = ? WHERE table_name = ?",
            [rows, size_bytes, now, table_name],
        )
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def get_default_output_dir() -> Path:
    """Get the default Jira extract output directory."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return data_dir / "extracts" / "jira"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_extract(get_default_output_dir())
