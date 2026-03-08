#!/usr/bin/env python3
"""
DuckDB Manager - Initialize and manage DuckDB database with views from parquet files.

This script dynamically reads table configurations from docs/data_description.md
and creates DuckDB views accordingly. No hardcoded table list needed!

Usage:
    python3 scripts/duckdb_manager.py --reinit    # Initialize/reinitialize all views
    python3 -m scripts.duckdb_manager --reinit    # Alternative module import
"""

import duckdb
import os
import sys
import argparse
import re
import yaml
from pathlib import Path
from typing import Dict, List, Tuple


def find_project_root() -> Path:
    """
    Find project root (folder containing docs/data_description.md).

    Searches from current folder upwards.

    Returns:
        Path to project root

    Raises:
        FileNotFoundError: If docs/data_description.md is not found
    """
    current = Path.cwd()

    # Try current folder first
    if (current / "docs" / "data_description.md").exists():
        return current

    # Also check for server/ subdirectory layout (analyst setup)
    if (current / "server" / "docs" / "data_description.md").exists():
        return current

    # Try parent folders (up to 5 levels)
    for _ in range(5):
        current = current.parent
        if (current / "docs" / "data_description.md").exists():
            return current
        if (current / "server" / "docs" / "data_description.md").exists():
            return current

    raise FileNotFoundError(
        "docs/data_description.md not found. "
        "Make sure you're running from project root."
    )


def parse_data_description(project_root: Path) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Parse data_description.md and extract table configurations.

    Args:
        project_root: Path to project root

    Returns:
        Tuple of (table_configs, folder_mapping)
        - table_configs: List of table configuration dicts
        - folder_mapping: Dict mapping bucket names to folder names
    """
    # Try both possible locations
    data_desc_path = project_root / "docs" / "data_description.md"
    if not data_desc_path.exists():
        data_desc_path = project_root / "server" / "docs" / "data_description.md"

    if not data_desc_path.exists():
        raise FileNotFoundError(f"data_description.md not found in {project_root}")

    with open(data_desc_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Collect all markdown files: main data_description + dataset files
    md_files = [data_desc_path]
    for datasets_dir_name in ["docs/datasets", "server/docs/datasets"]:
        datasets_dir = project_root / datasets_dir_name
        if datasets_dir.exists():
            for md_file in sorted(datasets_dir.glob("*.md")):
                md_files.append(md_file)

    # Parse YAML blocks from all files
    yaml_pattern = r'```yaml\s*\n(.*?)\n```'
    folder_mapping = {}
    table_configs = []

    for md_file in md_files:
        file_content = md_file.read_text() if md_file != data_desc_path else content
        for yaml_match in re.finditer(yaml_pattern, file_content, re.DOTALL):
            try:
                config_data = yaml.safe_load(yaml_match.group(1))
                if not config_data:
                    continue
                if 'folder_mapping' in config_data:
                    folder_mapping.update(config_data['folder_mapping'])
                if 'tables' in config_data:
                    table_configs.extend(config_data['tables'])
            except yaml.YAMLError as e:
                raise ValueError(f"Failed to parse YAML in {md_file.name}: {e}")

    if not table_configs:
        raise ValueError("No tables found in YAML configuration")

    return table_configs, folder_mapping


def get_parquet_path(table_config: Dict, folder_mapping: Dict[str, str], data_dir: str) -> Path:
    """
    Get path to Parquet file for given table.

    Format: {data_dir}/parquet/{folder_name}/{table_name}.parquet
    For partitioned tables: {data_dir}/parquet/{folder_name}/{table_name}/ (directory)

    Args:
        table_config: Table configuration dict
        folder_mapping: Mapping of bucket names to folder names
        data_dir: Base data directory (e.g., "server" for analysts, "data" for server)

    Returns:
        Path to Parquet file (or directory for partitioned tables)
    """
    # Extract bucket name from table ID (e.g., "in.c-crm" from "in.c-crm.company")
    table_id = table_config['id']
    bucket_name = ".".join(table_id.split(".")[:-1])

    # Use folder mapping if available, otherwise fall back to bucket name
    folder_name = folder_mapping.get(bucket_name, bucket_name)

    # Table-level folder override (e.g., folder: kbc_telemetry_expert)
    if table_config.get('folder'):
        folder_name = table_config['folder']

    table_name = table_config['name']
    sync_strategy = table_config.get('sync_strategy', 'full_refresh')

    parquet_dir = Path(data_dir) / "parquet" / folder_name

    # Determine if partitioned
    is_partitioned = (
        sync_strategy == "partitioned" or
        (sync_strategy == "incremental" and table_config.get('partition_by'))
    )

    if is_partitioned:
        # For partitioned tables, return directory path
        return parquet_dir / table_name
    else:
        # Single parquet file
        return parquet_dir / f"{table_name}.parquet"


def init_duckdb(db_path="user/duckdb/analytics.duckdb", data_dir="server", verbose=True):
    """
    Initialize DuckDB database with views from parquet files.

    Dynamically reads table configurations from docs/data_description.md
    and creates views accordingly.

    Args:
        db_path: Path to DuckDB database file
        data_dir: Base data directory (e.g., "server" for analysts, "data" for server)
        verbose: Print progress messages

    Returns:
        True if successful, False otherwise
    """
    # Ensure database directory exists
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if verbose:
        print("🦆 Inicializuji DuckDB databázi...")

    try:
        # Find project root and parse data_description.md
        project_root = find_project_root()
        if verbose:
            print(f"   📂 Project root: {project_root}")

        table_configs, folder_mapping = parse_data_description(project_root)
        if verbose:
            print(f"   📋 Načteno {len(table_configs)} tabulek z data_description.md")

        # Connect to database (creates if doesn't exist)
        conn = duckdb.connect(db_path)

        # Create views
        if verbose:
            print("\n📊 Vytvářím views z parquet souborů...")

        created_views = []
        skipped_views = []

        for table_config in table_configs:
            table_name = table_config['name']

            try:
                # Get parquet path
                parquet_path = get_parquet_path(table_config, folder_mapping, data_dir)

                # Check if file/directory exists
                if not parquet_path.exists():
                    skipped_views.append(table_name)
                    if verbose:
                        print(f"   ⚠️  Přeskakuji {table_name} - parquet neexistuje: {parquet_path}")
                    continue

                # Determine if partitioned
                sync_strategy = table_config.get('sync_strategy', 'full_refresh')
                is_partitioned = (
                    sync_strategy == "partitioned" or
                    (sync_strategy == "incremental" and table_config.get('partition_by'))
                )

                # Create view
                if is_partitioned:
                    # For partitioned tables, use glob pattern
                    glob_pattern = parquet_path / "*.parquet"

                    # Check if there are any partition files
                    partition_files = list(parquet_path.glob("*.parquet"))
                    if not partition_files:
                        skipped_views.append(table_name)
                        if verbose:
                            print(f"   ⚠️  Přeskakuji {table_name} - žádné partition soubory")
                        continue

                    sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{glob_pattern}', union_by_name=true)"
                    if verbose:
                        print(f"   ✅ {table_name} ({len(partition_files)} partitions)")
                else:
                    # Single parquet file
                    sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{parquet_path}')"
                    if verbose:
                        print(f"   ✅ {table_name}")

                conn.execute(sql)
                created_views.append(table_name)

            except Exception as e:
                if verbose:
                    print(f"   ❌ Chyba při vytváření {table_name}: {e}")
                return False

        # Display table list with row counts
        if verbose:
            print(f"\n📋 Seznam dostupných tabulek ({len(created_views)} vytvořeno):")
            tables = conn.execute("SHOW TABLES").fetchall()
            for table in tables:
                try:
                    row_count = conn.execute(f"SELECT COUNT(*) FROM {table[0]}").fetchone()[0]
                    print(f"   - {table[0]}: {row_count:,} řádků")
                except Exception as e:
                    print(f"   - {table[0]}: (chyba při počítání řádků)")

        # Close connection
        conn.close()

        if verbose:
            print(f"\n✅ DuckDB databáze vytvořena: {db_path}")
            print("💡 Můžeš začít analyzovat data pomocí DuckDB SQL dotazů")

        return True

    except Exception as e:
        if verbose:
            print(f"\n❌ Chyba při inicializaci DuckDB: {e}")
            import traceback
            traceback.print_exc()
        return False


def main():
    """Main entry point for command-line usage."""
    parser = argparse.ArgumentParser(
        description="DuckDB Manager - Initialize and manage DuckDB database"
    )
    parser.add_argument(
        '--reinit',
        action='store_true',
        help='Reinitialize all DuckDB views from parquet files'
    )
    parser.add_argument(
        '--db-path',
        default='user/duckdb/analytics.duckdb',
        help='Path to DuckDB database file (default: user/duckdb/analytics.duckdb)'
    )
    parser.add_argument(
        '--data-dir',
        default='server',
        help='Base data directory (default: server, use "data" for server deployment)'
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress progress messages'
    )

    args = parser.parse_args()

    # If no command provided, show help
    if not args.reinit:
        parser.print_help()
        sys.exit(1)

    # Run initialization
    success = init_duckdb(
        db_path=args.db_path,
        data_dir=args.data_dir,
        verbose=not args.quiet
    )

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
