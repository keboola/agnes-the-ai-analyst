"""Initialize Jira extract.duckdb with _meta table and views for all entity types.

Called once on first webhook or manually via CLI. Creates the extract.duckdb
contract structure for the Jira connector.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from src.duckdb_conn import _open_duckdb
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

JIRA_TABLES = ["issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"]

# Current-state dimension tables: a single `data.parquet` directly under the table
# directory, with no `month=` partitioning. The tables above are append-mostly event
# streams partitioned by the month of the parent issue; an organization has one
# current name and one current set of detail values, so partitioning it by month
# would either invent a history it does not have or scatter one row per month.
# Views over these are built without `hive_partitioning` (there is no partition key
# to project) but keep `union_by_name` so adding a detail column stays non-breaking.
JIRA_FLAT_TABLES = ["organizations"]


def _is_flat_table(table_name: str) -> bool:
    """Is this table a current-state dimension rather than a month-partitioned stream?"""
    return table_name in JIRA_FLAT_TABLES


def _flat_view_sql(table_name: str, table_dir: Path) -> str:
    """``CREATE OR REPLACE VIEW`` over an unpartitioned table directory."""
    glob_path = str(table_dir / "*.parquet")
    return (
        f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS "
        f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true)"
    )


def init_extract(output_dir: str | Path) -> None:
    """Create /data/extracts/jira/extract.duckdb with _meta and views.

    Views point to monthly parquet partitions in data/{table}/*.parquet.
    Safe to call multiple times — recreates _meta and views.
    """
    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    conn = _open_duckdb(str(db_path))

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

            # Migrate any remaining flat YYYY-MM.parquet files to hive layout
            # before building the view so the glob always points at hive dirs.
            try:
                from .incremental_transform import migrate_flat_to_hive

                migrated = migrate_flat_to_hive(table_dir)
                if migrated:
                    logger.info(
                        "Migrated %d flat parquet(s) to hive layout for %s: %s",
                        len(migrated),
                        table_name,
                        migrated,
                    )
            except Exception as mig_err:
                logger.warning("Could not migrate flat parquets for %s: %s", table_name, mig_err)

            # Create view only if hive partition dirs exist
            # (DuckDB glob fails on empty dirs / non-existent paths).
            rows = 0
            size_bytes = 0
            hive_parquets = list(table_dir.glob("month=*/data.parquet"))
            if hive_parquets:
                glob_path = str(table_dir / "month=*" / "*.parquet")
                conn.execute(
                    f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS "
                    f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true, hive_partitioning=true)"
                )
                try:
                    rows = conn.execute(f"SELECT count(*) FROM {quote_ident(table_name)}").fetchone()[0]
                    size_bytes = sum(f.stat().st_size for f in hive_parquets)
                except Exception:
                    pass

            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                [table_name, f"Jira {table_name}", rows, size_bytes, now],
            )

        for table_name in JIRA_FLAT_TABLES:
            table_dir = data_dir / table_name
            table_dir.mkdir(exist_ok=True)

            rows = 0
            size_bytes = 0
            flat_parquets = list(table_dir.glob("*.parquet"))
            if flat_parquets:
                conn.execute(_flat_view_sql(table_name, table_dir))
                try:
                    rows = conn.execute(f"SELECT count(*) FROM {quote_ident(table_name)}").fetchone()[0]
                    size_bytes = sum(f.stat().st_size for f in flat_parquets)
                except Exception:
                    pass

            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                [table_name, f"Jira {table_name}", rows, size_bytes, now],
            )

        logger.info(
            "Initialized Jira extract.duckdb at %s with %d tables",
            db_path,
            len(JIRA_TABLES) + len(JIRA_FLAT_TABLES),
        )
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

    conn = _open_duckdb(str(db_path))
    try:
        table_dir = output_path / "data" / table_name

        if _is_flat_table(table_name):
            # Unpartitioned dimension: same view-rebuild + row/size refresh as below,
            # minus the hive glob. Without this branch a flat table's _meta row would
            # keep whatever init_extract wrote (0 rows before the first refresh) and
            # its view would never be recreated, so the table would read as empty.
            rows = 0
            size_bytes = 0
            flat_parquets = list(table_dir.glob("*.parquet"))
            if flat_parquets:
                try:
                    conn.execute(_flat_view_sql(table_name, table_dir))
                    rows = conn.execute(f"SELECT count(*) FROM {quote_ident(table_name)}").fetchone()[0]
                    size_bytes = sum(f.stat().st_size for f in flat_parquets)
                except Exception as e:
                    logger.warning("Could not count rows for %s: %s", table_name, e)

            # Upsert rather than UPDATE. On an instance whose extract.duckdb predates
            # this table, `_meta` has no row for it, and a bare UPDATE would match
            # nothing — leaving the table absent from the catalog (and so unlisted for
            # users) until someone happened to re-run init_extract.
            now = datetime.now(timezone.utc)
            existing = conn.execute(
                "SELECT count(*) FROM _meta WHERE table_name = ?",
                [table_name],
            ).fetchone()[0]
            if existing:
                conn.execute(
                    "UPDATE _meta SET rows = ?, size_bytes = ?, extracted_at = ? WHERE table_name = ?",
                    [rows, size_bytes, now, table_name],
                )
            else:
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                    [table_name, f"Jira {table_name}", rows, size_bytes, now],
                )
            conn.execute("CHECKPOINT")
            return

        hive_parquets = list(table_dir.glob("month=*/data.parquet"))

        rows = 0
        size_bytes = 0
        if hive_parquets:
            try:
                glob_path = str(table_dir / "month=*" / "*.parquet")
                # Recreate view to pick up new/changed hive partition dirs
                conn.execute(
                    f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS "
                    f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true, hive_partitioning=true)"
                )
                rows = conn.execute(
                    f"SELECT count(*) FROM read_parquet('{glob_path}', union_by_name=true, hive_partitioning=true)"
                ).fetchone()[0]
                size_bytes = sum(f.stat().st_size for f in hive_parquets)
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
    from app.logging_config import setup_logging

    setup_logging(__name__)
    init_extract(get_default_output_dir())
