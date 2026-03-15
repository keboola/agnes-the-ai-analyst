"""
Data Synchronization Manager

Orchestrates data synchronization from configured sources to local Parquet files.

Main functions:
1. Tracking sync state (when was last synchronization)
2. DataSource ABC for pluggable connectors
3. Sync single table or all tables at once
4. Progress tracking and error handling
5. Schema generation from synced Parquet files

Sync State:
- Stored in data/metadata/sync_state.json
- Contains timestamp of last synchronization for each table
- Used for incremental sync (changedSince parameter)
"""

import importlib
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from tqdm import tqdm

from .config import get_config, TableConfig
from config.loader import load_instance_config
from connectors.openmetadata.enricher import CatalogEnricher


logger = logging.getLogger(__name__)


class SyncState:
    """
    Synchronization state management.

    Stores and loads information about last synchronization of each table.
    """

    def __init__(self, state_file: Path):
        """
        Args:
            state_file: Path to JSON file with sync state
        """
        self.state_file = state_file
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """
        Load sync state from disk.

        Returns:
            Dictionary with sync state
        """
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading sync state: {e}")
                return {}
        return {}

    def _save_state(self):
        """
        Save sync state to disk.

        Creates data/metadata/ directory if needed.
        """
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving sync state: {e}")

    def get_last_sync(self, table_id: str) -> Optional[str]:
        """
        Get timestamp of last synchronization for given table.

        Args:
            table_id: Table identifier

        Returns:
            ISO timestamp string, or None if not synced yet
        """
        table_state = self.state.get(table_id, {})
        return table_state.get("last_sync")

    def get_table_state(self, table_id: str) -> Dict[str, Any]:
        """
        Get complete sync state for a table.

        Args:
            table_id: Table identifier

        Returns:
            Dictionary with table sync state
        """
        return self.state.get(table_id, {})

    def update_sync(
        self,
        table_id: str,
        table_name: str,
        strategy: str,
        rows: int,
        file_size_bytes: int,
        columns: int = 0,
        uncompressed_bytes: int = 0,
    ):
        """
        Update synchronization state for a table.

        Args:
            table_id: Table identifier
            table_name: Human-readable table name
            strategy: Sync strategy used
            rows: Number of rows synced
            file_size_bytes: Size of Parquet file in bytes
            columns: Number of columns
            uncompressed_bytes: Uncompressed data size
        """
        self.state[table_id] = {
            "table_name": table_name,
            "last_sync": datetime.now().isoformat(),
            "strategy": strategy,
            "rows": rows,
            "columns": columns,
            "file_size_mb": round(file_size_bytes / 1024 / 1024, 2),
            "uncompressed_mb": round(uncompressed_bytes / 1024 / 1024, 2),
        }

        self._save_state()


class DataSource(ABC):
    """
    Abstract class for data source.

    Connectors implement this to integrate different data backends.
    See connectors/keboola/ for a reference implementation.
    """

    @abstractmethod
    def sync_table(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Synchronize single table.

        Args:
            table_config: Table configuration
            sync_state: Sync state manager

        Returns:
            Dictionary with sync result
        """
        pass

    def discover_tables(self) -> List[Dict[str, Any]]:
        """List all available tables in the data source.

        Returns list of dicts with at minimum:
            id, name, bucket_id, columns, row_count, size_bytes,
            primary_key, last_change
        Default: empty list (source doesn't support discovery).
        """
        return []

    def get_column_metadata(self, table_id: str) -> Optional[Dict[str, Any]]:
        """Return processed column metadata for schema generation.

        Returns:
            {"columns": {"col_name": {"source_type": "...", "description": "..."}}}
            or None if the source doesn't support metadata.
        """
        return None

    def get_source_name(self) -> str:
        """Display name of this data source for schema comments."""
        return "Unknown"


def _get_uncompressed_size(parquet_path: Path) -> int:
    """Read total uncompressed size from Parquet file metadata."""
    try:
        import pyarrow.parquet as pq

        meta = pq.ParquetFile(parquet_path).metadata
        total = 0
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            for col_idx in range(rg.num_columns):
                total += rg.column(col_idx).total_uncompressed_size
        return total
    except Exception:
        return 0


class DataSyncManager:
    """
    Main data synchronization orchestrator.

    Manages sync of all tables and tracks results.
    """

    def __init__(self):
        """Initialize sync manager."""
        self.config = get_config()
        self.sync_state = SyncState(
            self.config.get_metadata_path() / "sync_state.json"
        )
        self.data_source = create_data_source()

        # Initialize OpenMetadata catalog enricher
        try:
            instance_config = load_instance_config()
            self.catalog_enricher = CatalogEnricher(instance_config)
        except Exception as e:
            logger.warning(f"Failed to initialize catalog enricher: {e}")
            self.catalog_enricher = CatalogEnricher({})  # Disabled enricher

    def _generate_schema_yaml(self):
        """
        Generate schema.yml file with actual table schemas from Parquet files.

        This file is auto-generated and contains:
        - Table names and descriptions
        - Column names, types (from Parquet), and descriptions (from source metadata)
        - Primary keys

        Output: DOCS_OUTPUT_DIR/schema.yml (default: ./docs/schema.yml)
        """
        import yaml
        import pyarrow.parquet as pq

        source_name = self.data_source.get_source_name()

        logger.info("Generating schema.yml from synced tables...")

        schema_data = {
            "_metadata": {
                "_schema_version": 2,
                "generated_at": datetime.now().isoformat(),
                "note": "AUTO-GENERATED - DO NOT EDIT. This file contains actual table schemas from synced Parquet files.",
                "source": source_name,
                "generator": "src/data_sync.py::DataSyncManager._generate_schema_yaml()",
            },
            "tables": {},
        }

        # Process each table in configuration
        for table_config in self.config.tables:
            try:
                parquet_path = self.config.get_parquet_path(table_config)

                # Skip if Parquet doesn't exist (table not synced yet)
                if table_config.partition_by:
                    if not parquet_path.exists() or not list(parquet_path.glob("*.parquet")):
                        logger.debug(f"  Skipping {table_config.name} (not synced yet)")
                        continue
                    first_partition = next(parquet_path.glob("*.parquet"))
                    pf = pq.ParquetFile(first_partition)
                else:
                    if not parquet_path.exists():
                        logger.debug(f"  Skipping {table_config.name} (not synced yet)")
                        continue
                    pf = pq.ParquetFile(parquet_path)

                arrow_schema = pf.schema_arrow

                # Get column metadata from data source (if supported)
                col_metadata = self.data_source.get_column_metadata(table_config.id)

                # Enrich with catalog metadata (OpenMetadata)
                catalog_data = self.catalog_enricher.enrich_table(table_config)

                # Extract column information
                columns = []
                for field_item in arrow_schema:
                    col_name = field_item.name
                    col_name_lower = col_name.lower()
                    pyarrow_type = str(field_item.type)

                    column_info = {
                        "name": col_name,
                        "type": pyarrow_type,
                    }

                    # Priority for description: catalog > BQ API > (nothing)
                    description = None
                    if catalog_data and col_name_lower in catalog_data.columns:
                        description = catalog_data.columns[col_name_lower].description
                    elif col_metadata and "columns" in col_metadata:
                        col_meta = col_metadata["columns"].get(col_name, {})
                        description = col_meta.get("description")

                    if description:
                        column_info["description"] = description

                    # Add source type from connector metadata
                    if col_metadata and "columns" in col_metadata:
                        col_meta = col_metadata["columns"].get(col_name, {})
                        if "source_type" in col_meta:
                            column_info["source_type"] = col_meta["source_type"]

                    columns.append(column_info)

                primary_key = table_config.get_primary_key_columns()

                # Priority for table description: catalog > data_description.md
                table_description = table_config.description
                if catalog_data:
                    table_description = catalog_data.description or table_description

                table_info = {
                    "table_id": table_config.id,
                    "description": table_description,
                    "primary_key": primary_key,
                    "sync_strategy": table_config.sync_strategy,
                    "columns": columns,
                }

                if table_config.partition_by:
                    table_info["partitioned_by"] = table_config.partition_by

                schema_data["tables"][table_config.name] = table_info

                logger.debug(f"  {table_config.name}: {len(columns)} columns")

            except Exception as e:
                logger.warning(f"  Error processing {table_config.name}: {e}")

        # Split tables into core (no dataset) and per-dataset groups
        core_tables = {}
        dataset_tables = {}  # {dataset_name: {table_name: table_info}}
        for table_name, table_info in schema_data["tables"].items():
            table_config = next(
                (t for t in self.config.tables if t.name == table_name), None
            )
            if table_config and table_config.dataset:
                ds = table_config.dataset
                if ds not in dataset_tables:
                    dataset_tables[ds] = {}
                dataset_tables[ds][table_name] = table_info
            else:
                core_tables[table_name] = table_info

        generated_at = schema_data["_metadata"]["generated_at"]

        def _write_schema_file(filepath, tables, note=""):
            """Write a schema YAML file with header comments."""
            filepath.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "_metadata": {
                    "_schema_version": 2,
                    "generated_at": generated_at,
                    "note": "AUTO-GENERATED - DO NOT EDIT.",
                    "source": source_name,
                    "generator": "src/data_sync.py::DataSyncManager._generate_schema_yaml()",
                },
                "tables": tables,
            }
            with open(filepath, "w") as f:
                f.write("# AUTO-GENERATED - DO NOT EDIT\n")
                f.write("# This file is automatically generated during data sync\n")
                f.write(f"# Generated: {generated_at}\n")
                if note:
                    f.write(f"# {note}\n")
                f.write("#\n")
                f.write("# Contains actual table schemas from synced Parquet files:\n")
                f.write("# - Column names and PyArrow types (from Parquet)\n")
                f.write(f"# - Source types and descriptions (from {source_name})\n")
                f.write("# - Primary keys and sync strategies\n")
                f.write("#\n")
                f.write("# For architectural documentation and relationships, see data_description.md\n")
                f.write("\n")
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Write core schema.yml
        schema_file = self.config.docs_output_dir / "schema.yml"
        _write_schema_file(schema_file, core_tables)
        logger.info(f"Core schema YAML: {len(core_tables)} tables -> {schema_file}")

        # Write per-dataset schema files
        for ds_name, ds_tables in dataset_tables.items():
            ds_schema_file = self.config.docs_output_dir / "datasets" / ds_name / "schema.yml"
            _write_schema_file(ds_schema_file, ds_tables, note=f"Dataset: {ds_name}")
            logger.info(f"Dataset schema YAML: {len(ds_tables)} tables -> {ds_schema_file}")

        total = len(core_tables) + sum(len(t) for t in dataset_tables.values())
        logger.info(f"Schema generation complete: {total} tables total")

        return schema_file

    def sync_table(self, table_id: str) -> Dict[str, Any]:
        """
        Synchronize single table by ID.

        Args:
            table_id: Table ID to synchronize

        Returns:
            Dictionary with sync result
        """
        table_config = self.config.get_table_config(table_id)
        if not table_config:
            raise ValueError(f"Table {table_id} not found in configuration")

        return self.data_source.sync_table(table_config, self.sync_state)

    def sync_all(self, tables: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        Synchronize all tables (or subset according to list).

        Args:
            tables: List of table IDs to synchronize. If None, syncs all.

        Returns:
            Dictionary {table_id: result} with sync results
        """
        if tables:
            table_configs = [
                self.config.get_table_config(tid) for tid in tables
            ]
            table_configs = [tc for tc in table_configs if tc is not None]
        else:
            table_configs = self.config.tables

        # Filter out remote-only tables (no local sync needed)
        remote_skipped = [
            tc for tc in table_configs if tc.query_mode == "remote"
        ]
        table_configs = [
            tc for tc in table_configs if tc.query_mode != "remote"
        ]

        if remote_skipped:
            logger.info(
                f"Skipping {len(remote_skipped)} remote-only tables "
                f"(query via BigQuery): "
                f"{', '.join(tc.name for tc in remote_skipped)}"
            )

        logger.info(f"Synchronizing {len(table_configs)} tables...")

        results = {}
        with tqdm(table_configs, desc="Syncing tables") as pbar:
            for table_config in pbar:
                pbar.set_description(f"Sync: {table_config.name}")

                result = self.data_source.sync_table(table_config, self.sync_state)
                results[table_config.id] = result

                if result["success"]:
                    pbar.write(
                        f"  {table_config.name}: {result['rows']:,} rows, "
                        f"{result['file_size_mb']:.2f} MB"
                    )
                else:
                    pbar.write(f"  {table_config.name}: {result['error']}")

        success_count = sum(1 for r in results.values() if r["success"])
        logger.info(
            f"Synchronization completed: {success_count}/{len(results)} tables successful"
        )

        # Generate schema.yml from synced tables
        if success_count > 0:
            try:
                self._generate_schema_yaml()
            except Exception as e:
                logger.warning(f"Failed to generate schema.yml: {e}")

        # Auto-profile changed tables
        if success_count > 0:
            self._auto_profile(results)

        return results

    def _auto_profile(
        self,
        results: Dict[str, Dict[str, Any]],
        skip_tables: Optional[List[str]] = None,
    ):
        """Run profiler on successfully synced tables.

        Args:
            results: Sync results dict {table_id: result}
            skip_tables: Table IDs to skip profiling for
        """
        skip_set = set(skip_tables or [])
        try:
            from src.profiler import profile_changed_tables
            changed = [
                self.config.get_table_config(tid).name
                for tid, r in results.items()
                if r.get("success")
                and self.config.get_table_config(tid)
                and tid not in skip_set
            ]
            if changed:
                result = profile_changed_tables(changed)
                logger.info(
                    f"Auto-profiling: {result['success']} profiled, "
                    f"{result['errors']} errors, {result['skipped']} skipped"
                )
            else:
                logger.info("No tables to profile (all skipped or none succeeded)")
        except Exception as e:
            logger.warning(f"Auto-profiling failed (non-fatal): {e}")

    def sync_scheduled(self) -> Dict[str, Dict[str, Any]]:
        """Synchronize only tables whose sync_schedule says they are due.

        Evaluates each table's sync_schedule against its last_sync timestamp.
        Only syncs tables that are due. Respects profile_after_sync flag.

        Returns:
            Dictionary {table_id: result} with sync results (only for synced tables)
        """
        from src.scheduler import is_table_due

        scheduled_tables = [
            tc for tc in self.config.tables
            if tc.sync_schedule and tc.query_mode != "remote"
        ]

        if not scheduled_tables:
            logger.info("No tables with sync_schedule configured")
            return {}

        # Evaluate which tables are due
        due_tables = []
        for tc in scheduled_tables:
            last_sync = self.sync_state.get_last_sync(tc.id)
            if is_table_due(tc.sync_schedule, last_sync):
                due_tables.append(tc)
                logger.info(f"Table {tc.name} is DUE (schedule: {tc.sync_schedule})")
            else:
                logger.debug(f"Table {tc.name} is not due (schedule: {tc.sync_schedule})")

        if not due_tables:
            logger.info(
                f"Checked {len(scheduled_tables)} scheduled tables, none are due"
            )
            return {}

        logger.info(
            f"Syncing {len(due_tables)}/{len(scheduled_tables)} due tables: "
            f"{', '.join(tc.name for tc in due_tables)}"
        )

        # Sync due tables
        results = {}
        for table_config in due_tables:
            try:
                result = self.data_source.sync_table(table_config, self.sync_state)
                results[table_config.id] = result
                if result["success"]:
                    logger.info(
                        f"  {table_config.name}: {result['rows']:,} rows, "
                        f"{result['file_size_mb']:.2f} MB"
                    )
                else:
                    logger.error(f"  {table_config.name}: {result['error']}")
            except Exception as e:
                logger.error(f"  {table_config.name}: sync failed: {e}")
                results[table_config.id] = {"success": False, "error": str(e)}

        success_count = sum(1 for r in results.values() if r["success"])
        logger.info(f"Scheduled sync: {success_count}/{len(results)} tables successful")

        # Generate schema.yml
        if success_count > 0:
            try:
                self._generate_schema_yaml()
            except Exception as e:
                logger.warning(f"Failed to generate schema.yml: {e}")

        # Profile only tables with profile_after_sync=True
        skip_profiler = [
            tc.id for tc in due_tables if not tc.profile_after_sync
        ]
        if skip_profiler:
            logger.info(
                f"Skipping profiler for: "
                f"{', '.join(self.config.get_table_config(tid).name for tid in skip_profiler)}"
            )

        profiled_any = False
        if success_count > 0:
            tables_to_profile = [
                tid for tid, r in results.items()
                if r.get("success") and tid not in set(skip_profiler)
            ]
            if tables_to_profile:
                self._auto_profile(results, skip_tables=skip_profiler)
                profiled_any = True

        # Restart webapp if profiler ran (new profiles.json needs reload)
        if profiled_any:
            self._restart_webapp()

        return results

    def _restart_webapp(self):
        """Restart webapp service to pick up new profiles.json."""
        import subprocess
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "webapp"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            logger.info("Webapp restarted successfully")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to restart webapp: {e.stderr.decode() if e.stderr else e}")
        except FileNotFoundError:
            logger.debug("systemctl not found (not running on server)")


def create_sync_manager() -> DataSyncManager:
    """
    Factory function to create DataSyncManager.

    Returns:
        DataSyncManager instance
    """
    return DataSyncManager()


def create_data_source(source_type: str = None) -> DataSource:
    """Create a data source based on configuration.

    Args:
        source_type: Override source type. If None, uses DATA_SOURCE env var.

    Returns:
        DataSource instance

    Raises:
        ValueError: If source type is unknown
        ImportError: If connector dependencies are missing
    """
    if source_type is None:
        source_type = get_config().data_source

    if source_type in ("local", "keboola"):
        try:
            from connectors.keboola.adapter import KeboolaDataSource
        except ModuleNotFoundError as e:
            if "kbcstorage" in str(e):
                raise ImportError(
                    "Keboola connector requires 'kbcstorage' package. "
                    "Install with: pip install kbcstorage"
                ) from e
            raise  # Re-raise real import errors
        return KeboolaDataSource()

    # Try dynamic connector import for other types
    try:
        mod = importlib.import_module(f"connectors.{source_type}.adapter")
        factory = getattr(mod, "create_data_source", None)
        if factory:
            return factory()
        # Fallback: look for a class named *DataSource
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if isinstance(attr, type) and issubclass(attr, DataSource) and attr is not DataSource:
                return attr()
    except ModuleNotFoundError:
        pass

    raise ValueError(
        f"Unknown data source: '{source_type}'. "
        f"Available connectors: keboola, bigquery. "
        f"Create connectors/{source_type}/adapter.py to add a new one."
    )


if __name__ == "__main__":
    # CLI interface for sync
    import sys

    scheduled_mode = "--scheduled" in sys.argv
    table_args = [a for a in sys.argv[1:] if a != "--scheduled"]

    try:
        manager = create_sync_manager()

        if scheduled_mode:
            print("Data Sync (scheduled mode)")
            results = manager.sync_scheduled()

            if not results:
                print("No tables due for sync")
                sys.exit(0)
        elif table_args:
            print("Data Sync")
            print(f"\nSynchronizing selected tables: {', '.join(table_args)}")
            results = manager.sync_all(tables=table_args)
        else:
            print("Data Sync")
            print("\nSynchronizing all tables...")
            results = manager.sync_all()

        success_count = sum(1 for r in results.values() if r["success"])
        total_count = len(results)

        if success_count == total_count:
            print(f"\nAll {total_count} tables synchronized successfully!")
            sys.exit(0)
        else:
            print(
                f"\n{success_count}/{total_count} tables synchronized. "
                f"Check logs for details."
            )
            sys.exit(1)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
