#!/usr/bin/env python3
"""
DuckDB Manager - Initialize and manage DuckDB database with views from parquet files
and runtime BigQuery query registration for remote/hybrid tables.

For BigQuery data sources, tables with query_mode="remote" or "hybrid" are queried
at runtime via the Python BQ client and registered as in-memory Arrow tables in DuckDB.
This avoids the DuckDB BigQuery extension limitation (cannot read BQ views).

Usage:
    python3 scripts/duckdb_manager.py --reinit    # Initialize/reinitialize all views
    python3 -m scripts.duckdb_manager --reinit    # Alternative module import
"""

import duckdb
import logging
import os
import sys
import argparse
import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


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


def _get_bq_project_from_table_id(table_id: str) -> Optional[str]:
    """Extract BQ project ID from a fully-qualified table ID.

    Args:
        table_id: e.g. "prj-grp-dataview-prod-1ff9.finance_unit_economics.unit_economics"

    Returns:
        Project ID or None if format doesn't match BQ convention
    """
    parts = table_id.split(".")
    if len(parts) == 3 and "-" in parts[0]:
        return parts[0]
    return None


def _create_bq_client(project: str):
    """Create a BigQuery client. Separated for testability.

    Args:
        project: GCP project ID for billing

    Returns:
        google.cloud.bigquery.Client instance
    """
    from google.cloud import bigquery as bq_module

    return bq_module.Client(project=project)


def register_bq_table(
    conn: duckdb.DuckDBPyConnection,
    table_id: str,
    view_name: str,
    sql: str,
    bq_project: Optional[str] = None,
    _bq_client_factory=None,
) -> int:
    """
    Execute a BigQuery SQL query and register the result as a DuckDB view.

    Uses the Python BigQuery client (Query API) which supports BQ views,
    unlike the DuckDB BigQuery extension (Storage Read API).
    The result is held in memory as a PyArrow table -- no disk I/O.

    Args:
        conn: Open DuckDB connection
        table_id: BQ table ID for logging (e.g., "project.dataset.table")
        view_name: Name to register in DuckDB (e.g., "unit_economics_live")
        sql: Full BigQuery SQL query to execute
        bq_project: GCP project for billing. If None, uses BIGQUERY_PROJECT env var.
        _bq_client_factory: Override BQ client creation (for testing)

    Returns:
        Number of rows in the result

    Raises:
        ImportError: If google-cloud-bigquery is not installed
        ValueError: If bq_project is not set
    """
    project = bq_project or os.environ.get("BIGQUERY_PROJECT")
    if not project:
        raise ValueError(
            "BigQuery project not set. "
            "Pass bq_project or set BIGQUERY_PROJECT env var."
        )

    logger.info(f"Querying BQ: {table_id} -> {view_name}")
    logger.debug(f"SQL: {sql[:200]}...")

    factory = _bq_client_factory or _create_bq_client
    client = factory(project)
    job = client.query(sql)

    # Use Query API (not Storage Read API) to support BQ views
    try:
        arrow_table = job.to_arrow()
    except Exception as e:
        if "readsessions" in str(e) or "PERMISSION_DENIED" in str(e):
            logger.warning("BQ Storage API unavailable, falling back to REST")
            arrow_table = job.to_arrow(create_bqstorage_client=False)
        else:
            raise

    conn.register(view_name, arrow_table)
    logger.info(
        f"Registered {view_name}: {arrow_table.num_rows} rows, "
        f"{arrow_table.num_columns} cols (in-memory)"
    )

    return arrow_table.num_rows


def get_remote_tables(table_configs: List[Dict]) -> List[Dict]:
    """Return table configs with query_mode 'remote' or 'hybrid'.

    Args:
        table_configs: List of table configuration dicts

    Returns:
        List of remote/hybrid table configs
    """
    return [
        tc for tc in table_configs
        if tc.get("query_mode") in ("remote", "hybrid")
    ]


def init_duckdb(
    db_path="user/duckdb/analytics.duckdb",
    data_dir="server",
    verbose=True,
    bq_project: Optional[str] = None,
):
    """
    Initialize DuckDB database with views from parquet files.

    Creates DuckDB views for local/hybrid tables (from Parquet).
    Remote tables are NOT pre-loaded -- they are registered at query time
    via register_bq_table().

    Args:
        db_path: Path to DuckDB database file
        data_dir: Base data directory (e.g., "server" for analysts, "data" for server)
        verbose: Print progress messages
        bq_project: BigQuery execution project (for informational purposes only)

    Returns:
        True if successful, False otherwise
    """
    # Ensure database directory exists
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if verbose:
        print("Initializing DuckDB database...")

    try:
        # Find project root and parse data_description.md
        project_root = find_project_root()
        if verbose:
            print(f"   Project root: {project_root}")

        table_configs, folder_mapping = parse_data_description(project_root)
        if verbose:
            print(f"   Loaded {len(table_configs)} tables from data_description.md")

        # Separate tables by query_mode
        local_tables = []
        remote_tables = []
        hybrid_tables = []

        for tc in table_configs:
            mode = tc.get("query_mode", "local")
            if mode == "remote":
                remote_tables.append(tc)
            elif mode == "hybrid":
                hybrid_tables.append(tc)
            else:
                local_tables.append(tc)

        if verbose:
            print(f"   Query modes: {len(local_tables)} local, "
                  f"{len(remote_tables)} remote, {len(hybrid_tables)} hybrid")

        # Connect to database (creates if doesn't exist)
        conn = duckdb.connect(db_path)

        # Create local views from parquet files
        if verbose:
            print("\n   Creating views from parquet files...")

        created_views = []
        skipped_views = []

        # Process local and hybrid tables (both have local parquet)
        for table_config in local_tables + hybrid_tables:
            table_name = table_config['name']

            try:
                # Get parquet path
                parquet_path = get_parquet_path(table_config, folder_mapping, data_dir)

                # Check if file/directory exists
                if not parquet_path.exists():
                    skipped_views.append(table_name)
                    if verbose:
                        print(f"   [SKIP] {table_name} - parquet not found: {parquet_path}")
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
                            print(f"   [SKIP] {table_name} - no partition files")
                        continue

                    sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{glob_pattern}', union_by_name=true)"
                    if verbose:
                        mode_label = "hybrid" if table_config.get("query_mode") == "hybrid" else "local"
                        print(f"   [OK] {table_name} ({len(partition_files)} partitions, {mode_label})")
                else:
                    # Single parquet file
                    sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{parquet_path}')"
                    if verbose:
                        mode_label = "hybrid" if table_config.get("query_mode") == "hybrid" else "local"
                        print(f"   [OK] {table_name} ({mode_label})")

                conn.execute(sql)
                created_views.append(table_name)

            except Exception as e:
                if verbose:
                    print(f"   [ERR] Error creating {table_name}: {e}")
                return False

        # Log remote tables (queried at runtime via register_bq_table)
        if remote_tables:
            if verbose:
                print("\n   Remote tables (queried at runtime via BigQuery):")
            for table_config in remote_tables:
                table_name = table_config['name']
                table_id = table_config['id']
                if verbose:
                    print(f"   [BQ] {table_name} -> {table_id}")

        # Display table list with row counts
        if verbose:
            print(f"\n   Available tables ({len(created_views)} local views):")
            tables = conn.execute("SHOW TABLES").fetchall()
            for table in tables:
                try:
                    row_count = conn.execute(f"SELECT COUNT(*) FROM {table[0]}").fetchone()[0]
                    print(f"   - {table[0]}: {row_count:,} rows (local)")
                except Exception:
                    print(f"   - {table[0]}: (error counting rows)")

            if remote_tables:
                print(f"\n   Remote tables ({len(remote_tables)}, loaded on demand):")
                for tc in remote_tables:
                    print(f"   - {tc['name']}: via BQ Query API (use date filters!)")

        # Close connection
        conn.close()

        if verbose:
            print(f"\n   DuckDB database created: {db_path}")

        return True

    except Exception as e:
        if verbose:
            print(f"\n   Error initializing DuckDB: {e}")
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
        '--bq-project',
        default=None,
        help='BigQuery execution project (informational only)'
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
        verbose=not args.quiet,
        bq_project=args.bq_project,
    )

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
