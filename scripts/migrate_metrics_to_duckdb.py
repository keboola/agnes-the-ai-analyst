"""Migrate metric YAML files to DuckDB metric_definitions table.

Usage:
    python scripts/migrate_metrics_to_duckdb.py [--metrics-dir docs/metrics]
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Migrate metric YAMLs to DuckDB")
    parser.add_argument("--metrics-dir", default="docs/metrics", help="Path to metrics directory")
    args = parser.parse_args()

    metrics_dir = Path(args.metrics_dir)
    if not metrics_dir.is_dir():
        logger.error("Metrics directory not found: %s", metrics_dir)
        sys.exit(1)

    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository
    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.import_from_yaml(metrics_dir)
        logger.info("Imported %d metrics from %s", count, metrics_dir)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
