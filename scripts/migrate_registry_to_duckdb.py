"""Migrate table registry from data_description.md or JSON to DuckDB.

One-time script for existing deployments transitioning to extract.duckdb architecture.
Idempotent — safe to run multiple times (uses INSERT OR REPLACE).

Usage:
    python scripts/migrate_registry_to_duckdb.py [--from-md docs/data_description.md] [--from-json path/to/table_registry.json]
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


def _parse_table_id(table_id: str, default_source: str) -> dict:
    """Infer source_type, bucket, source_table from table ID.

    Keboola: 'in.c-crm.company' → bucket='in.c-crm', source_table='company'
    BigQuery: 'project.dataset.table' → bucket='dataset', source_table='table'
    """
    parts = table_id.rsplit(".", 1)
    if len(parts) == 2:
        return {"bucket": parts[0], "source_table": parts[1]}
    return {"bucket": "", "source_table": table_id}


def migrate_from_markdown(md_path: Path, source_type: str) -> list[dict]:
    """Parse data_description.md and return table configs."""
    content = md_path.read_text()
    yaml_blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)

    tables = []
    for block in yaml_blocks:
        data = yaml.safe_load(block)
        if data and "tables" in data:
            tables.extend(data["tables"])

    configs = []
    for t in tables:
        parsed = _parse_table_id(t.get("id", t.get("name", "")), source_type)
        configs.append(
            {
                "id": t.get("id", t.get("name", "")),
                "name": t.get("name", ""),
                "source_type": source_type,
                "bucket": parsed["bucket"],
                "source_table": parsed["source_table"],
                "sync_strategy": t.get("sync_strategy", "full_refresh"),
                "query_mode": t.get("query_mode", "local"),
                "sync_schedule": t.get("sync_schedule"),
                "profile_after_sync": t.get("profile_after_sync", True),
                "primary_key": t.get("primary_key"),
                "folder": t.get("folder"),
                "description": t.get("description", ""),
            }
        )

    return configs


def migrate_from_json(json_path: Path, source_type: str) -> list[dict]:
    """Parse table_registry.json and return table configs."""
    data = json.loads(json_path.read_text())
    tables = data.get("tables", [])

    configs = []
    for t in tables:
        parsed = _parse_table_id(t.get("id", t.get("name", "")), source_type)
        configs.append(
            {
                "id": t.get("id", t.get("name", "")),
                "name": t.get("name", ""),
                "source_type": source_type,
                "bucket": parsed["bucket"],
                "source_table": parsed["source_table"],
                "sync_strategy": t.get("sync_strategy", "full_refresh"),
                "query_mode": t.get("query_mode", "local"),
                "sync_schedule": t.get("sync_schedule"),
                "profile_after_sync": t.get("profile_after_sync", True),
                "primary_key": t.get("primary_key"),
                "folder": t.get("folder"),
                "description": t.get("description", ""),
            }
        )

    return configs


def write_to_duckdb(configs: list[dict]) -> int:
    """Write table configs to DuckDB table_registry. Returns count."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        repo = TableRegistryRepository(conn)
        count = 0
        for c in configs:
            repo.register(
                id=c["id"],
                name=c["name"],
                source_type=c["source_type"],
                bucket=c["bucket"],
                source_table=c["source_table"],
                sync_strategy=c["sync_strategy"],
                query_mode=c["query_mode"],
                sync_schedule=c["sync_schedule"],
                profile_after_sync=c["profile_after_sync"],
                primary_key=c["primary_key"],
                folder=c["folder"],
                description=c["description"],
                registered_by="migration",
            )
            count += 1
            logger.info("  Registered: %s (%s)", c["name"], c["id"])
        return count
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate table registry to DuckDB")
    parser.add_argument("--from-md", type=Path, help="Path to data_description.md")
    parser.add_argument("--from-json", type=Path, help="Path to table_registry.json")
    parser.add_argument("--source-type", default=None, help="Override source type (keboola, bigquery)")
    args = parser.parse_args()

    # Detect source type from instance.yaml if not specified
    source_type = args.source_type
    if not source_type:
        try:
            from app.instance_config import get_data_source_type

            source_type = get_data_source_type()
        except Exception:
            source_type = "keboola"
        logger.info("Detected source type: %s", source_type)

    configs = []
    if args.from_md:
        if not args.from_md.exists():
            logger.error("File not found: %s", args.from_md)
            sys.exit(1)
        configs = migrate_from_markdown(args.from_md, source_type)
        logger.info("Parsed %d tables from %s", len(configs), args.from_md)

    elif args.from_json:
        if not args.from_json.exists():
            logger.error("File not found: %s", args.from_json)
            sys.exit(1)
        configs = migrate_from_json(args.from_json, source_type)
        logger.info("Parsed %d tables from %s", len(configs), args.from_json)

    else:
        # Auto-detect: try JSON first, then markdown
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        json_path = data_dir / "src_data" / "metadata" / "table_registry.json"
        md_path = Path("docs/data_description.md")

        if json_path.exists():
            configs = migrate_from_json(json_path, source_type)
            logger.info("Auto-detected: parsed %d tables from %s", len(configs), json_path)
        elif md_path.exists():
            configs = migrate_from_markdown(md_path, source_type)
            logger.info("Auto-detected: parsed %d tables from %s", len(configs), md_path)
        else:
            logger.error("No source found. Use --from-md or --from-json")
            sys.exit(1)

    if not configs:
        logger.warning("No tables found to migrate")
        sys.exit(0)

    count = write_to_duckdb(configs)
    logger.info("Migration complete: %d tables written to DuckDB table_registry", count)


if __name__ == "__main__":
    main()
