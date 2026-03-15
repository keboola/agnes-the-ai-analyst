"""
Configuration Management

This module handles:
1. Loading environment variables from .env file
2. Parsing data_description.md (YAML blocks with table definitions)
3. Validating configuration
4. Providing structured configuration data for other modules

SINGLE SOURCE OF TRUTH is data_description.md - it defines:
- List of tables to synchronize
- Sync strategies (full_refresh vs incremental)
- Primary keys and foreign keys
- Incremental columns and windows
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml
from dotenv import load_dotenv


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class ForeignKey:
    """
    Representation of foreign key relationship between tables.

    Attributes:
        column: Column name in this table (e.g., "company_id")
        references: Reference table and column (e.g., "company.id")
        description: Relationship description
    """
    column: str
    references: str
    description: Optional[str] = None


@dataclass
class WhereFilter:
    """
    Filter for exporting subset of table data.

    Used with Keboola Storage API whereFilters parameter.

    Attributes:
        column: Column name to filter on
        operator: Comparison operator (eq, ne, gt, ge, lt, le)
        values: List of values to compare against
    """
    column: str
    operator: str  # eq, ne, gt, ge, lt, le
    values: List[str] = field(default_factory=list)


@dataclass
class TableConfig:
    """
    Configuration for a single table.

    Attributes:
        id: Full table ID in Keboola (e.g., "in.c-sfdc.company")
        name: Short table name (e.g., "company")
        description: Table description
        primary_key: Primary key column name
        sync_strategy: "full_refresh", "incremental", or "partitioned"
        incremental_window_days: Number of days to backtrack for incremental sync
        partition_by: Column name to partition by (for incremental/partitioned with partitions)
        partition_granularity: Partition granularity: "month", "day", or "year"
        foreign_keys: List of foreign key relationships
        where_filters: List of filters to apply when exporting (for downloading subset of data)
        folder: Override folder name (instead of bucket-level folder_mapping)
        max_history_days: Max days of history for initial incremental load (None = download all)
        dataset: Dataset group name for on-demand tables (e.g., "kbc_telemetry_expert")
        initial_load_chunk_days: Chunk size in days for chunked initial load (default: 30)
        sync_schedule: Schedule for automatic sync: "every 15m", "every 1h", "daily 05:00" (UTC)
        profile_after_sync: Run profiler after sync (default True; disable for frequently synced tables)
    """
    id: str
    name: str
    description: str
    primary_key: str
    sync_strategy: str  # "full_refresh", "incremental", or "partitioned"
    incremental_window_days: Optional[int] = None
    partition_by: Optional[str] = None
    partition_granularity: Optional[str] = None  # "month", "day", "year"
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    where_filters: List[WhereFilter] = field(default_factory=list)
    folder: Optional[str] = None
    max_history_days: Optional[int] = None
    dataset: Optional[str] = None
    initial_load_chunk_days: int = 30
    incremental_column: Optional[str] = None  # Column for timestamp-based incremental sync (BigQuery)
    columns: Optional[List[str]] = None  # Subset of columns to sync (None = all)
    row_filter: Optional[str] = None  # SQL WHERE clause for filtering (e.g., "event_date >= '2024-01-01'")
    query_mode: str = "local"  # "local" (Parquet) | "remote" (BQ direct) | "hybrid" (sync subset, query BQ)
    partition_column_type: str = "TIMESTAMP"  # BQ SQL type for partition column: "DATE", "TIMESTAMP", "DATETIME"
    catalog_fqn: Optional[str] = None  # Explicit OpenMetadata FQN override (auto-derived if not set)
    sync_schedule: Optional[str] = None  # Schedule: "every 15m", "every 1h", "daily 05:00" (UTC)
    profile_after_sync: bool = True  # Run profiler after sync (disable for frequently synced tables)

    def __post_init__(self):
        """Validate configuration after initialization."""
        # Validate query_mode
        valid_query_modes = ("local", "remote", "hybrid")
        if self.query_mode not in valid_query_modes:
            raise ValueError(
                f"Invalid query_mode '{self.query_mode}' for table {self.id}. "
                f"Allowed values: {', '.join(valid_query_modes)}"
            )

        # Validate sync_strategy
        if self.sync_strategy not in ["full_refresh", "incremental", "partitioned"]:
            raise ValueError(
                f"Invalid sync_strategy '{self.sync_strategy}' for table {self.id}. "
                f"Allowed values: 'full_refresh', 'incremental', 'partitioned'"
            )

        # For incremental strategy:
        # - changedSince is calculated from last sync timestamp (Keboola internal)
        # - partition_by is optional - if set, output will be partitioned
        if self.sync_strategy == "incremental":
            if not self.incremental_window_days:
                # Default 7 days if not specified
                self.incremental_window_days = 7
                logger.warning(
                    f"Table {self.id}: incremental_window_days not set, "
                    f"using default 7 days"
                )
            # If partition_by is set, validate partition_granularity
            if self.partition_by:
                if not self.partition_granularity:
                    self.partition_granularity = "month"
                    logger.info(
                        f"Table {self.id}: partition_granularity not set, "
                        f"using default 'month'"
                    )
                if self.partition_granularity not in ["month", "day", "year"]:
                    raise ValueError(
                        f"Invalid partition_granularity '{self.partition_granularity}' for table {self.id}. "
                        f"Allowed values: 'month', 'day', 'year'"
                    )

        # Validate partition_column_type
        valid_column_types = ("DATE", "TIMESTAMP", "DATETIME")
        if self.partition_column_type not in valid_column_types:
            raise ValueError(
                f"Invalid partition_column_type '{self.partition_column_type}' for table {self.id}. "
                f"Allowed values: {', '.join(valid_column_types)}"
            )

        # Validate sync_schedule format
        if self.sync_schedule:
            import re as _re
            valid_schedule = (
                _re.match(r"^every \d+[mh]$", self.sync_schedule)
                or _re.match(r"^daily \d{2}:\d{2}$", self.sync_schedule)
            )
            if not valid_schedule:
                raise ValueError(
                    f"Invalid sync_schedule '{self.sync_schedule}' for table {self.id}. "
                    f"Allowed formats: 'every 15m', 'every 1h', 'daily 05:00'"
                )

        # For partitioned, partition_by must be defined
        if self.sync_strategy == "partitioned":
            if not self.partition_by:
                raise ValueError(
                    f"Table {self.id} has sync_strategy='partitioned', "
                    f"but partition_by is missing"
                )
            if not self.partition_granularity:
                self.partition_granularity = "month"
                logger.info(
                    f"Table {self.id}: partition_granularity not set, "
                    f"using default 'month'"
                )
            if self.partition_granularity not in ["month", "day", "year"]:
                raise ValueError(
                    f"Invalid partition_granularity '{self.partition_granularity}' for table {self.id}. "
                    f"Allowed values: 'month', 'day', 'year'"
                )

    def get_primary_key_columns(self) -> List[str]:
        """
        Get primary key as list of column names.

        Supports both single and composite primary keys.
        Composite PKs are defined as comma-separated string: "col1, col2"

        Returns:
            List of column names forming the primary key
        """
        # Split by comma and strip whitespace
        return [col.strip() for col in self.primary_key.split(",")]

    def is_partitioned(self) -> bool:
        """Check if table output should be partitioned.

        Returns True for:
        - partitioned strategy (always partitioned)
        - incremental strategy with partition_by set
        """
        if self.sync_strategy == "partitioned":
            return True
        if self.sync_strategy == "incremental" and self.partition_by:
            return True
        return False


class Config:
    """
    Main configuration class.

    Loads environment variables and parses data_description.md.
    Provides access to all configuration parameters.
    """

    def __init__(self, env_file: Optional[str] = None):
        """
        Initialize configuration.

        Args:
            env_file: Path to .env file. If None, looks for .env in project root.
        """
        # Find project root (folder containing data_description.md)
        self.project_root = self._find_project_root()

        # Load environment variables
        if env_file is None:
            env_file = self.project_root / ".env"

        if env_file.exists():
            load_dotenv(env_file)
            logger.info(f"Loaded from .env: {env_file}")
        else:
            logger.warning(
                f".env file not found: {env_file}. "
                f"Use config/.env.template as reference."
            )

        # Read by connectors/keboola/ if enabled
        self.keboola_token = os.getenv("KEBOOLA_STORAGE_TOKEN")
        self.keboola_stack_url = os.getenv("KEBOOLA_STACK_URL")
        self.keboola_project_id = os.getenv("KEBOOLA_PROJECT_ID")
        self.data_dir = Path(os.getenv("DATA_DIR", "./data"))
        self.docs_output_dir = Path(os.getenv("DOCS_OUTPUT_DIR", "./docs"))
        self.data_source = os.getenv("DATA_SOURCE", "local")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

        # Set log level
        logging.getLogger().setLevel(self.log_level)

        # Validate required environment variables
        self._validate_env_vars()

        # Parse data_description.md
        self.tables, self.folder_mapping = self._parse_data_description()

        logger.info(f"Configuration loaded: {len(self.tables)} tables")

    def _find_project_root(self) -> Path:
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

        # Try parent folders (up to 5 levels)
        for _ in range(5):
            current = current.parent
            if (current / "docs" / "data_description.md").exists():
                return current

        raise FileNotFoundError(
            "docs/data_description.md not found. "
            "Make sure you're running from project root."
        )

    def _resolve_placeholder(self, value: str) -> str:
        """
        Resolve placeholders in filter values.

        Supported placeholders:
        - {{last_week}}: 7 days ago
        - {{last_month}}: 30 days ago
        - {{last_2_months}}: 60 days ago
        - {{last_3_months}}: 90 days ago
        - {{last_6_months}}: 180 days ago
        - {{last_year}}: 365 days ago
        - {{last_2_years}}: 730 days ago
        - {{today}}: Today's date

        Args:
            value: String that may contain placeholder

        Returns:
            Resolved string with actual date values
        """
        if not isinstance(value, str):
            return value

        today = datetime.now()

        placeholders = {
            "{{last_week}}": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
            "{{last_month}}": (today - timedelta(days=30)).strftime("%Y-%m-%d"),
            "{{last_2_months}}": (today - timedelta(days=60)).strftime("%Y-%m-%d"),
            "{{last_3_months}}": (today - timedelta(days=90)).strftime("%Y-%m-%d"),
            "{{last_6_months}}": (today - timedelta(days=180)).strftime("%Y-%m-%d"),
            "{{last_year}}": (today - timedelta(days=365)).strftime("%Y-%m-%d"),
            "{{last_2_years}}": (today - timedelta(days=730)).strftime("%Y-%m-%d"),
            "{{today}}": today.strftime("%Y-%m-%d"),
        }

        result = value
        for placeholder, replacement in placeholders.items():
            if placeholder in result:
                result = result.replace(placeholder, replacement)
                logger.debug(f"Resolved placeholder: {placeholder} -> {replacement}")

        return result

    def _validate_env_vars(self):
        """
        Validate that required environment variables are set based on data source type.

        Raises:
            ValueError: If any required variable is missing
        """
        # Keboola env vars are validated by connectors/keboola/adapter.py at init time.
        # No source-specific validation needed here.
        pass

    def _parse_data_description(self) -> tuple[List[TableConfig], Dict[str, str]]:
        """
        Parse docs/data_description.md and extract table definitions.

        Looks for YAML blocks in markdown file and parses them.

        Returns:
            Tuple of (List of TableConfig objects, folder_mapping dict)

        Raises:
            FileNotFoundError: If docs/data_description.md doesn't exist
            yaml.YAMLError: If YAML is invalid
        """
        # Check CONFIG_DIR first, then project root
        config_dir = Path(os.environ.get("CONFIG_DIR", ""))
        if config_dir and (config_dir / "data_description.md").exists():
            data_desc_path = config_dir / "data_description.md"
        else:
            data_desc_path = self.project_root / "docs" / "data_description.md"

        if not data_desc_path.exists():
            raise FileNotFoundError(
                f"docs/data_description.md not found: {data_desc_path}"
            )

        # Collect all markdown files to parse: main + dataset files
        md_files = [data_desc_path]
        datasets_dir = self.project_root / "docs" / "datasets"
        if datasets_dir.exists():
            for md_file in sorted(datasets_dir.glob("*.md")):
                md_files.append(md_file)
                logger.info(f"Found dataset file: {md_file.name}")

        # Find YAML blocks (between ```yaml and ```) from all files
        yaml_pattern = r'```yaml\n(.*?)```'
        yaml_matches = []
        for md_file in md_files:
            content = md_file.read_text()
            yaml_matches.extend(re.findall(yaml_pattern, content, re.DOTALL))

        if not yaml_matches:
            raise ValueError(
                "data_description.md contains no YAML blocks. "
                "Make sure tables are defined in ```yaml blocks."
            )

        # Parse all YAML blocks and merge them
        all_tables = []
        folder_mapping = {}
        for yaml_block in yaml_matches:
            try:
                data = yaml.safe_load(yaml_block)
                if data:
                    if "tables" in data:
                        all_tables.extend(data["tables"])
                    if "folder_mapping" in data:
                        folder_mapping.update(data["folder_mapping"])
            except yaml.YAMLError as e:
                logger.error(f"Error parsing YAML: {e}")
                raise

        if not all_tables:
            raise ValueError(
                "data_description.md contains no tables. "
                "Make sure YAML block contains 'tables:' key."
            )

        # Convert to TableConfig objects
        table_configs = []
        for table_data in all_tables:
            # Parse foreign keys
            fk_list = []
            if "foreign_keys" in table_data:
                for fk_data in table_data["foreign_keys"]:
                    fk = ForeignKey(
                        column=fk_data["column"],
                        references=fk_data["references"],
                        description=fk_data.get("description")
                    )
                    fk_list.append(fk)

            # Parse where filters with placeholder resolution
            wf_list = []
            if "where_filters" in table_data:
                for wf_data in table_data["where_filters"]:
                    # Resolve placeholders in values
                    resolved_values = [
                        self._resolve_placeholder(v) for v in wf_data.get("values", [])
                    ]
                    wf = WhereFilter(
                        column=wf_data["column"],
                        operator=wf_data["operator"],
                        values=resolved_values
                    )
                    wf_list.append(wf)

            # Create TableConfig
            config = TableConfig(
                id=table_data["id"],
                name=table_data["name"],
                description=table_data["description"],
                primary_key=table_data["primary_key"],
                sync_strategy=table_data["sync_strategy"],
                incremental_window_days=table_data.get("incremental_window_days"),
                partition_by=table_data.get("partition_by"),
                partition_granularity=table_data.get("partition_granularity"),
                foreign_keys=fk_list,
                where_filters=wf_list,
                folder=table_data.get("folder"),
                max_history_days=table_data.get("max_history_days"),
                dataset=table_data.get("dataset"),
                initial_load_chunk_days=table_data.get("initial_load_chunk_days", 30),
                incremental_column=table_data.get("incremental_column"),
                columns=table_data.get("columns"),
                row_filter=table_data.get("row_filter"),
                query_mode=table_data.get("query_mode", "local"),
                partition_column_type=table_data.get("partition_column_type", "TIMESTAMP"),
                catalog_fqn=table_data.get("catalog_fqn"),
                sync_schedule=table_data.get("sync_schedule"),
                profile_after_sync=table_data.get("profile_after_sync", True),
            )
            table_configs.append(config)

        return table_configs, folder_mapping

    def get_table_config(self, table_id: str) -> Optional[TableConfig]:
        """
        Get configuration for specific table by ID.

        Args:
            table_id: Full table ID (e.g., "in.c-sfdc.company")

        Returns:
            TableConfig or None if table not in configuration
        """
        for table in self.tables:
            if table.id == table_id:
                return table
        return None

    def get_parquet_path(self, table_config: TableConfig) -> Path:
        """
        Get path to Parquet file for given table.

        Format: data/parquet/{folder_name}/{table_name}.parquet
        For partitioned tables: data/parquet/{folder_name}/{table_name}/ (directory)

        Folder name is determined by folder_mapping in data_description.md.
        Falls back to bucket name if no mapping exists.

        Args:
            table_config: Table configuration

        Returns:
            Path to Parquet file (or directory for partitioned tables)
        """
        # Extract bucket name from table ID (e.g., "in.c-crm" from "in.c-crm.company")
        bucket_name = ".".join(table_config.id.split(".")[:-1])

        # Use folder mapping if available, otherwise fall back to bucket name
        folder_name = self.folder_mapping.get(bucket_name, bucket_name)

        # Table-level folder override (e.g., folder: kbc_telemetry_expert)
        if table_config.folder:
            folder_name = table_config.folder

        parquet_dir = self.data_dir / "parquet" / folder_name
        parquet_dir.mkdir(parents=True, exist_ok=True)

        if table_config.is_partitioned():
            # For partitioned tables, return directory path
            partition_dir = parquet_dir / table_config.name
            partition_dir.mkdir(parents=True, exist_ok=True)
            return partition_dir
        else:
            return parquet_dir / f"{table_config.name}.parquet"

    def get_partition_path(self, table_config: TableConfig, partition_key: str) -> Path:
        """
        Get path to specific partition file.

        Args:
            table_config: Table configuration (must be partitioned)
            partition_key: Partition key (e.g., "2026_01" for monthly)

        Returns:
            Path to partition Parquet file
        """
        if not table_config.is_partitioned():
            raise ValueError(f"Table {table_config.id} is not partitioned")

        partition_dir = self.get_parquet_path(table_config)
        return partition_dir / f"{partition_key}.parquet"

    def get_metadata_path(self) -> Path:
        """
        Get path to metadata folder.

        Returns:
            Path to metadata folder
        """
        metadata_dir = self.data_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        return metadata_dir

    def get_staging_path(self) -> Path:
        """
        Get path to staging folder for temporary files.

        Uses /tmp/data_analyst_staging for faster I/O and to avoid filling /data disk.
        Directory is created by deploy.sh on server startup.

        Returns:
            Path to staging folder
        """
        staging_dir = Path("/tmp/data_analyst_staging")
        staging_dir.mkdir(parents=True, exist_ok=True)
        return staging_dir

    def get_duckdb_path(self) -> Path:
        """
        Get path to DuckDB database.

        Returns:
            Path to DuckDB file
        """
        duckdb_dir = self.data_dir / "duckdb"
        duckdb_dir.mkdir(parents=True, exist_ok=True)
        return duckdb_dir / "analytics.duckdb"


# Singleton instance for easy access from entire application
_config_instance: Optional[Config] = None


def get_config() -> Config:
    """
    Get singleton configuration instance.

    On first call initializes configuration, then returns existing instance.

    Returns:
        Config instance
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance


# For testing - allows resetting config
def reset_config():
    """Reset singleton config instance. For testing only."""
    global _config_instance
    _config_instance = None


if __name__ == "__main__":
    # Test configuration
    print("🔧 Testing configuration...")

    try:
        config = get_config()

        print(f"\n✅ Configuration loaded successfully!")
        print(f"   Project ID: {config.keboola_project_id}")
        print(f"   Stack URL: {config.keboola_stack_url}")
        print(f"   Data dir: {config.data_dir}")
        print(f"   Number of tables: {len(config.tables)}")

        print(f"\n📊 Tables:")
        for table in config.tables:
            print(f"   - {table.name} ({table.id})")
            print(f"     Strategy: {table.sync_strategy}")
            if table.sync_strategy == "incremental":
                print(f"     Incremental window: {table.incremental_window_days} days")
                if table.partition_by:
                    print(f"     Partitioned by: {table.partition_by} ({table.partition_granularity})")
            print(f"     Parquet: {config.get_parquet_path(table)}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
