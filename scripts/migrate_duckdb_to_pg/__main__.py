"""CLI entry point: ``python -m scripts.migrate_duckdb_to_pg``."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot DuckDB → Postgres data migration",
    )
    parser.add_argument(
        "--duckdb-path",
        default=None,
        help="Path to system.duckdb (default: ${DATA_DIR}/state/system.duckdb)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only run the named task (target_table). Repeatable.",
    )
    parser.add_argument("--dry-run", action="store_true", help="No PG writes")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip post-copy validation (row counts + checksums)",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.duckdb_path:
        duckdb_path = Path(args.duckdb_path)
    else:
        data_dir = os.environ.get("DATA_DIR")
        if not data_dir:
            print("DATA_DIR not set and --duckdb-path not provided", file=sys.stderr)
            return 2
        duckdb_path = Path(data_dir) / "state" / "system.duckdb"
    if not duckdb_path.is_file():
        print(f"DuckDB file not found: {duckdb_path}", file=sys.stderr)
        return 2

    import src.db_pg as db_pg
    from src.duckdb_conn import _open_duckdb
    from scripts.migrate_duckdb_to_pg import run_all

    duck_conn = _open_duckdb(str(duckdb_path), read_only=True)
    pg_engine = db_pg.get_engine()

    reports = run_all(
        duck_conn,
        pg_engine,
        only=args.only or None,
        dry_run=args.dry_run,
        validate=not args.no_validate,
    )
    for r in reports:
        print(r)
    # Per-task errors land as {"table": ..., "error": ...} without a
    # checksum_match key; the default-True on .get() previously masked
    # them. Treat the explicit error key as the authoritative failure
    # signal. Both predicates must hold for exit 0.
    return (
        0 if all("error" not in r and r.get("checksum_match", True) for r in reports)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
