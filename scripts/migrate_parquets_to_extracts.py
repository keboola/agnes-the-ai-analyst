"""Move existing parquet files to extract.duckdb directory structure.

One-time script for existing deployments. Moves parquets from
/data/src_data/parquet/ to /data/extracts/{source}/data/ and creates
extract.duckdb with _meta + views.

Usage:
    python scripts/migrate_parquets_to_extracts.py [--source keboola] [--dry-run]
"""

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


def migrate_parquets(source_name: str, dry_run: bool = False) -> dict:
    """Move parquets and create extract.duckdb.

    Returns: {moved: int, total_bytes: int, tables: list[str]}
    """
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    old_parquet_dir = data_dir / "src_data" / "parquet"
    new_extract_dir = data_dir / "extracts" / source_name
    new_data_dir = new_extract_dir / "data"

    if not old_parquet_dir.exists():
        logger.warning("No parquet directory found at %s", old_parquet_dir)
        return {"moved": 0, "total_bytes": 0, "tables": []}

    parquet_files = list(old_parquet_dir.rglob("*.parquet"))
    if not parquet_files:
        logger.warning("No parquet files found in %s", old_parquet_dir)
        return {"moved": 0, "total_bytes": 0, "tables": []}

    logger.info("Found %d parquet files in %s", len(parquet_files), old_parquet_dir)

    if not dry_run:
        new_data_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    total_bytes = 0
    tables = []

    for pq_file in parquet_files:
        table_name = pq_file.stem
        size = pq_file.stat().st_size
        dest = new_data_dir / pq_file.name

        if dry_run:
            logger.info("  [DRY RUN] Would move: %s -> %s (%d bytes)", pq_file, dest, size)
        else:
            # Copy instead of move to be safe — user can delete originals after verification
            shutil.copy2(str(pq_file), str(dest))
            logger.info("  Copied: %s -> %s (%d bytes)", pq_file.name, dest, size)

        moved += 1
        total_bytes += size
        if table_name not in tables:
            tables.append(table_name)

    # Create extract.duckdb
    if not dry_run and tables:
        db_path = new_extract_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        try:
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
            for table_name in tables:
                pq_path = str(new_data_dir / f"{table_name}.parquet")
                if not Path(pq_path).exists():
                    continue

                # Create view
                conn.execute(f"CREATE OR REPLACE VIEW \"{table_name}\" AS SELECT * FROM read_parquet('{pq_path}')")

                # Count rows
                try:
                    rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
                except Exception:
                    rows = 0

                size = Path(pq_path).stat().st_size
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                    [table_name, "", rows, size, now],
                )

            logger.info("Created extract.duckdb at %s with %d tables", db_path, len(tables))
        finally:
            conn.close()

    return {"moved": moved, "total_bytes": total_bytes, "tables": tables}


def main():
    parser = argparse.ArgumentParser(description="Migrate parquets to extract.duckdb structure")
    parser.add_argument("--source", default="keboola", help="Source name (default: keboola)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    args = parser.parse_args()

    result = migrate_parquets(args.source, dry_run=args.dry_run)
    logger.info(
        "Migration %s: %d files, %d tables, %.1f MB",
        "preview" if args.dry_run else "complete",
        result["moved"],
        len(result["tables"]),
        result["total_bytes"] / 1024 / 1024,
    )


if __name__ == "__main__":
    main()
