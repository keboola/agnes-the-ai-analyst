"""
Keboola data source adapter.

Implements the DataSource interface for Keboola Storage API.
Downloads tables via the Storage API, converts CSV exports to Parquet files
with full type metadata from Keboola column metadata.
"""

import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import pyarrow as pa
from tqdm import tqdm

from src.config import get_config, TableConfig
from src.data_sync import DataSource, SyncState, _get_uncompressed_size
from src.parquet_manager import (
    create_parquet_manager,
    _convert_column,
    convert_date_columns_to_date32,
    apply_schema_to_table,
)
from .client import create_client as create_keboola_client


logger = logging.getLogger(__name__)


class KeboolaDataSource(DataSource):
    """
    Data source: Direct download from Keboola Storage API.

    Downloads data directly from a Keboola project, converts CSV exports
    to typed Parquet files using column metadata for schema enforcement.
    """

    def __init__(self):
        """Initialize Keboola source with full env var validation."""
        self.config = get_config()

        # Validate all required Keboola env vars before proceeding
        missing = []
        if not self.config.keboola_token:
            missing.append("KEBOOLA_STORAGE_TOKEN")
        if not self.config.keboola_stack_url:
            missing.append("KEBOOLA_STACK_URL")
        if not self.config.keboola_project_id:
            missing.append("KEBOOLA_PROJECT_ID")
        if missing:
            raise ValueError(
                f"Missing required environment variables for Keboola connector: "
                f"{', '.join(missing)}. See config/.env.template"
            )

        self.keboola_client = create_keboola_client()
        self.parquet_manager = create_parquet_manager()

    def get_column_metadata(self, table_id: str) -> Optional[Dict[str, Any]]:
        """Return Keboola metadata with provider cascade applied.

        Delegates type resolution to the client's _resolve_keboola_type(),
        and extracts descriptions via provider priority cascade.

        Returns:
            {"columns": {"col_name": {"source_type": "...", "description": "..."}}}
            or None if metadata is unavailable.
        """
        raw = self.keboola_client.get_table_metadata(table_id)
        column_metadata = raw.get("column_metadata", {})

        if not column_metadata:
            return None

        PROVIDER_PRIORITY = [
            "user",
            "ai-metadata-enrichment",
            "keboola.snowflake-transformation",
        ]

        result = {}
        for col_name, col_meta_list in column_metadata.items():
            # Delegate type resolution to client
            source_type = self.keboola_client._resolve_keboola_type(col_meta_list)

            # Extract description via provider cascade
            description = None
            if isinstance(col_meta_list, list):
                description_by_provider = {}
                for entry in col_meta_list:
                    provider = entry.get("provider", "")
                    key = entry.get("key", "")
                    value = entry.get("value", "")
                    if key == "KBC.description":
                        description_by_provider[provider] = value

                for p in PROVIDER_PRIORITY:
                    if p in description_by_provider:
                        description = description_by_provider[p]
                        break

            result[col_name] = {"source_type": source_type}
            if description:
                result[col_name]["description"] = description

        return {"columns": result}

    def discover_tables(self) -> List[Dict[str, Any]]:
        """Discover all available tables from Keboola Storage."""
        return self.keboola_client.discover_all_tables()

    def get_source_name(self) -> str:
        """Display name of this data source."""
        return "Keboola Storage API"

    def _cleanup_staging(self):
        """
        Remove all files from staging directory.

        Called before chunked initial load and after failures to free up disk space.
        """
        staging_dir = self.config.get_staging_path()
        for f in staging_dir.glob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    logger.debug(f"Cleaned up staging file: {f.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {f.name}: {e}")

    def sync_table(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Synchronize table from Keboola.

        According to sync_strategy calls _full_refresh or _incremental_sync.

        Args:
            table_config: Table configuration
            sync_state: Sync state manager

        Returns:
            Dictionary with result:
            - success: bool
            - rows: int
            - strategy: str
            - error: str (if failed)
        """
        logger.info(f"Syncing table: {table_config.name} ({table_config.sync_strategy})")

        # Refresh metadata cache for this table to get latest types from Keboola
        if table_config.id in self.keboola_client.metadata_cache:
            del self.keboola_client.metadata_cache[table_config.id]
            logger.debug(f"Cleared metadata cache for {table_config.id}")

        try:
            if table_config.sync_strategy == "full_refresh":
                result = self._full_refresh(table_config)
            elif table_config.sync_strategy == "partitioned":
                result = self._partitioned_sync(table_config)
            else:  # incremental
                result = self._incremental_sync(table_config, sync_state)

            # Update sync state
            sync_state.update_sync(
                table_id=table_config.id,
                table_name=table_config.name,
                strategy=table_config.sync_strategy,
                rows=result["rows"],
                file_size_bytes=result["file_size_bytes"],
                columns=result.get("columns", 0),
                uncompressed_bytes=result.get("uncompressed_bytes", 0),
            )

            return {
                "success": True,
                "rows": result["rows"],
                "strategy": table_config.sync_strategy,
                "file_size_mb": result["file_size_bytes"] / 1024 / 1024,
            }

        except Exception as e:
            logger.error(f"Error syncing table {table_config.name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "strategy": table_config.sync_strategy,
            }

    def _full_refresh(self, table_config: TableConfig) -> Dict[str, Any]:
        """
        Full refresh sync strategy.

        Downloads entire table and replaces existing Parquet file.
        """
        logger.info(f"Full refresh: {table_config.name}")

        parquet_path = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export from Keboola to CSV
            filters_desc = ""
            if table_config.where_filters:
                filters_desc = f" (filters: {len(table_config.where_filters)})"
            logger.info(f"  -> Exporting from Keboola...{filters_desc}")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                where_filters=table_config.where_filters if table_config.where_filters else None,
            )

            # 2. Get dtypes for proper conversion
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. Convert CSV -> Parquet
            logger.info("  -> Converting to Parquet...")
            parquet_info = self.parquet_manager.csv_to_parquet(
                csv_path=tmp_csv_path,
                parquet_path=parquet_path,
                dtypes=dtypes,
                table_id=table_config.id,
                date_columns=date_columns,
                pyarrow_schema=pyarrow_schema,
            )

            return {
                "rows": parquet_info["rows"],
                "file_size_bytes": parquet_info["parquet_size_bytes"],
                "columns": parquet_info.get("columns", 0),
                "uncompressed_bytes": _get_uncompressed_size(parquet_path),
            }

        finally:
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _incremental_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Incremental sync strategy.

        Downloads only changed rows using changedSince API parameter.
        If partition_by is configured, outputs are partitioned.
        Otherwise, merges into a single Parquet file.
        """
        if table_config.partition_by:
            return self._incremental_partitioned_sync(table_config, sync_state)
        return self._incremental_single_file_sync(table_config, sync_state)

    def _incremental_single_file_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Incremental sync to a single Parquet file (no partitioning).
        """
        logger.info(f"Incremental sync (single file): {table_config.name}")

        parquet_path = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        # Determine timestamp for changedSince
        last_sync = sync_state.get_last_sync(table_config.id)

        if last_sync:
            last_sync_dt = datetime.fromisoformat(last_sync)
            window_days = table_config.incremental_window_days or 7
            changed_since_dt = last_sync_dt - timedelta(days=window_days)
            changed_since = changed_since_dt.isoformat()
            logger.info(
                f"  -> ChangedSince: {changed_since} (window: {window_days} days)"
            )
        else:
            if table_config.max_history_days:
                changed_since_dt = datetime.now() - timedelta(days=table_config.max_history_days)
                changed_since = changed_since_dt.isoformat()
                logger.info(
                    f"  -> First sync, limited to last {table_config.max_history_days} days "
                    f"(changedSince: {changed_since})"
                )
            else:
                logger.info("  -> First sync, downloading all data...")
                changed_since = None

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export changed data from Keboola
            logger.info("  -> Exporting changes from Keboola...")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                changed_since=changed_since,
            )

            if export_info["exported_rows"] == 0:
                logger.info("  -> No changes since last synchronization")
                if parquet_path.exists():
                    existing_info = self.parquet_manager.get_parquet_info(parquet_path)
                    return {
                        "rows": existing_info["rows"],
                        "file_size_bytes": existing_info["file_size_bytes"],
                        "columns": existing_info.get("columns", 0),
                        "uncompressed_bytes": _get_uncompressed_size(parquet_path),
                    }
                else:
                    return {"rows": 0, "file_size_bytes": 0, "columns": 0, "uncompressed_bytes": 0}

            # 2. Get dtypes and date columns
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. If Parquet exists, merge; otherwise create new
            if parquet_path.exists():
                logger.info(
                    f"  -> Merging {export_info['exported_rows']} changes into Parquet..."
                )

                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".parquet", delete=False, dir=staging_dir
                ) as tmp_parquet_file:
                    tmp_parquet_path = Path(tmp_parquet_file.name)

                try:
                    merge_info = self.parquet_manager.merge_parquet(
                        existing_parquet=parquet_path,
                        new_csv=tmp_csv_path,
                        output_parquet=tmp_parquet_path,
                        primary_key=table_config.get_primary_key_columns(),
                        dtypes=dtypes,
                        date_columns=date_columns,
                        pyarrow_schema=pyarrow_schema,
                    )

                    tmp_parquet_path.replace(parquet_path)

                    return {
                        "rows": merge_info["total_rows"],
                        "file_size_bytes": parquet_path.stat().st_size,
                        "columns": merge_info.get("total_columns", 0),
                        "uncompressed_bytes": _get_uncompressed_size(parquet_path),
                    }

                finally:
                    if tmp_parquet_path.exists():
                        tmp_parquet_path.unlink()

            else:
                logger.info("  -> Creating new Parquet...")
                parquet_info = self.parquet_manager.csv_to_parquet(
                    csv_path=tmp_csv_path,
                    parquet_path=parquet_path,
                    dtypes=dtypes,
                    table_id=table_config.id,
                    date_columns=date_columns,
                    pyarrow_schema=pyarrow_schema,
                )

                return {
                    "rows": parquet_info["rows"],
                    "file_size_bytes": parquet_info["parquet_size_bytes"],
                    "columns": parquet_info.get("columns", 0),
                    "uncompressed_bytes": _get_uncompressed_size(parquet_path),
                }

        finally:
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _incremental_partitioned_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Incremental sync with partitioned output.

        Downloads only changed rows using changedSince API parameter,
        then partitions by partition_by column and merges into existing
        partition files. Same logic as _partitioned_sync but uses
        changedSince instead of whereFilters.

        For initial load of large tables (max_history_days > chunk_days),
        uses chunked download to avoid filling up disk space. Each chunk
        has 1-day overlap with the next to ensure no data is lost at boundaries.
        """
        import pandas as pd

        logger.info(
            f"Incremental sync (partitioned): {table_config.name} "
            f"(by {table_config.partition_by}, {table_config.partition_granularity})"
        )

        partition_dir = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        last_sync = sync_state.get_last_sync(table_config.id)

        # For initial load (no last_sync), always use chunked approach
        if not last_sync:
            return self._chunked_initial_load(table_config, partition_dir, staging_dir)

        # Regular incremental sync
        last_sync_dt = datetime.fromisoformat(last_sync)
        window_days = table_config.incremental_window_days or 7
        changed_since_dt = last_sync_dt - timedelta(days=window_days)
        changed_since = changed_since_dt.isoformat()

        logger.info(
            f"  -> ChangedSince: {changed_since} (window: {window_days} days)"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            logger.info("  -> Exporting changes from Keboola...")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                changed_since=changed_since,
            )

            if export_info["exported_rows"] == 0:
                logger.info("  -> No changes since last synchronization")
                return self._get_partition_totals(partition_dir)

            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            logger.info(f"  -> Processing {export_info['exported_rows']} changed rows...")

            partitions_updated = self._process_csv_to_partitions(
                tmp_csv_path, table_config, partition_dir,
                dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema,
            )

            self._deduplicate_partitions(
                table_config, partitions_updated,
                date_columns=date_columns, pyarrow_schema=pyarrow_schema,
            )

            logger.info(f"  -> Incremental sync complete, {len(partitions_updated)} partitions updated")
            return self._get_partition_totals(partition_dir)

        finally:
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _chunked_initial_load(
        self,
        table_config: TableConfig,
        partition_dir: Path,
        staging_dir: Path,
    ) -> Dict[str, Any]:
        """
        Chunked initial load for large tables.

        Downloads data in time-window chunks to avoid filling up disk space.
        Each chunk has 1-day overlap with the next to ensure no data is lost
        at boundaries. Deduplication removes any duplicates from overlaps.
        """
        chunk_days = table_config.initial_load_chunk_days
        max_history_days = table_config.max_history_days
        overlap_days = 1
        max_chunks_safety = 120

        now = datetime.now()

        if max_history_days:
            num_chunks = (max_history_days + chunk_days - 1) // chunk_days
            logger.info(
                f"  -> CHUNKED INITIAL LOAD: {max_history_days} days in {num_chunks} chunks "
                f"of {chunk_days} days each (with {overlap_days}-day overlap)"
            )
        else:
            num_chunks = None
            logger.info(
                f"  -> CHUNKED INITIAL LOAD: iterating backwards in {chunk_days}-day chunks "
                f"until no more data (with {overlap_days}-day overlap)"
            )

        self._cleanup_staging()

        dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
        date_columns = self.keboola_client.get_date_columns(table_config.id)
        pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

        all_partitions_updated = set()
        chunk_idx = 0
        consecutive_empty_chunks = 0

        while True:
            if chunk_idx >= max_chunks_safety:
                logger.warning(f"  -> Reached safety limit of {max_chunks_safety} chunks, stopping")
                break

            if num_chunks is not None and chunk_idx >= num_chunks:
                break

            chunk_end_offset = chunk_idx * chunk_days
            chunk_start_offset = chunk_end_offset + chunk_days + overlap_days

            chunk_end = now - timedelta(days=chunk_end_offset) if chunk_idx > 0 else None
            chunk_start = now - timedelta(days=chunk_start_offset)

            if max_history_days and chunk_start_offset > max_history_days:
                chunk_start = now - timedelta(days=max_history_days)

            chunk_label = f"{chunk_idx + 1}" if num_chunks is None else f"{chunk_idx + 1}/{num_chunks}"
            logger.info(
                f"  -> Chunk {chunk_label}: "
                f"{chunk_start.strftime('%Y-%m-%d')} to "
                f"{chunk_end.strftime('%Y-%m-%d') if chunk_end else 'now'}"
            )

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", delete=False, dir=staging_dir
            ) as tmp_file:
                tmp_csv_path = Path(tmp_file.name)

            try:
                export_info = self.keboola_client.export_table(
                    table_id=table_config.id,
                    output_path=tmp_csv_path,
                    changed_since=chunk_start.isoformat(),
                    changed_until=chunk_end.isoformat() if chunk_end else None,
                )

                if export_info["exported_rows"] == 0:
                    logger.info("    -> No data in this chunk")
                    consecutive_empty_chunks += 1
                    if num_chunks is None and consecutive_empty_chunks >= 2:
                        logger.info(
                            f"  -> Found {consecutive_empty_chunks} consecutive empty chunks, "
                            f"assuming end of history"
                        )
                        break
                    chunk_idx += 1
                    continue

                consecutive_empty_chunks = 0
                logger.info(f"    -> Exported {export_info['exported_rows']} rows")

                partitions_updated = self._process_csv_to_partitions(
                    tmp_csv_path, table_config, partition_dir,
                    dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema,
                )
                all_partitions_updated.update(partitions_updated)

                logger.info(f"    -> Processed into {len(partitions_updated)} partitions")

            finally:
                if tmp_csv_path.exists():
                    tmp_csv_path.unlink()

            chunk_idx += 1

        if all_partitions_updated:
            logger.info(
                f"  -> Final deduplication of {len(all_partitions_updated)} partitions "
                f"(removing duplicates from {overlap_days}-day overlaps)..."
            )
            self._deduplicate_partitions(
                table_config, all_partitions_updated,
                date_columns=date_columns, pyarrow_schema=pyarrow_schema,
            )

        logger.info(
            f"  -> Chunked initial load complete: {len(all_partitions_updated)} partitions"
        )

        return self._get_partition_totals(partition_dir)

    def _process_csv_to_partitions(
        self,
        csv_path: Path,
        table_config: TableConfig,
        partition_dir: Path,
        dtypes: Optional[Dict[str, str]] = None,
        date_columns: Optional[List[str]] = None,
        pyarrow_schema: Optional[pa.Schema] = None,
    ) -> set:
        """
        Process CSV file and write to partition files.

        Returns:
            Set of partition keys that were updated
        """
        import pandas as pd
        import pyarrow.parquet as pq

        partition_col = table_config.partition_by
        granularity = table_config.partition_granularity or "month"

        partitions_updated = set()
        chunk_size = 500000  # 500k rows per pandas chunk

        chunk_num = 0
        for chunk_df in pd.read_csv(csv_path, chunksize=chunk_size, dtype=str):
            chunk_num += 1
            logger.debug(f"    -> Processing pandas chunk {chunk_num} ({len(chunk_df)} rows)...")

            if partition_col not in chunk_df.columns:
                raise ValueError(f"Partition column '{partition_col}' not found in data")

            # Apply dtypes using _convert_column (except datetime columns)
            if dtypes:
                for col, dtype in dtypes.items():
                    if col in chunk_df.columns and "datetime" not in dtype:
                        try:
                            chunk_df[col] = _convert_column(chunk_df[col], dtype, col_name=col)
                        except Exception as e:
                            logger.warning(f"Failed to apply dtype {dtype} to column {col}: {e}")

            # Convert partition column to datetime
            if not pd.api.types.is_datetime64_any_dtype(chunk_df[partition_col]):
                chunk_df[partition_col] = pd.to_datetime(
                    chunk_df[partition_col], format="ISO8601", utc=True
                )

            # Create partition key based on granularity
            if granularity == "month":
                chunk_df["_partition_key"] = chunk_df[partition_col].dt.strftime("%Y_%m")
            elif granularity == "day":
                chunk_df["_partition_key"] = chunk_df[partition_col].dt.strftime("%Y_%m_%d")
            elif granularity == "year":
                chunk_df["_partition_key"] = chunk_df[partition_col].dt.strftime("%Y")

            # Group by partition and append to partition files
            for partition_key, partition_df in chunk_df.groupby("_partition_key"):
                partition_df = partition_df.drop(columns=["_partition_key"])
                partition_path = self.config.get_partition_path(table_config, partition_key)
                partitions_updated.add(partition_key)

                if partition_path.exists():
                    existing_df = pd.read_parquet(partition_path)
                    merged_df = pd.concat([existing_df, partition_df], ignore_index=True)
                    table = pa.Table.from_pandas(merged_df, preserve_index=False)
                    if date_columns:
                        table = convert_date_columns_to_date32(table, date_columns)
                    if pyarrow_schema:
                        table = apply_schema_to_table(table, pyarrow_schema)
                    pq.write_table(table, partition_path, compression="snappy")
                else:
                    table = pa.Table.from_pandas(partition_df, preserve_index=False)
                    if date_columns:
                        table = convert_date_columns_to_date32(table, date_columns)
                    if pyarrow_schema:
                        table = apply_schema_to_table(table, pyarrow_schema)
                    pq.write_table(table, partition_path, compression="snappy")

        return partitions_updated

    def _deduplicate_partitions(
        self,
        table_config: TableConfig,
        partitions_to_dedup: set,
        date_columns: Optional[List[str]] = None,
        pyarrow_schema: Optional[pa.Schema] = None,
    ):
        """
        Deduplicate partition files based on primary key.
        """
        import pandas as pd
        import pyarrow.parquet as pq

        primary_key_cols = table_config.get_primary_key_columns()

        logger.info(f"  -> Deduplicating {len(partitions_to_dedup)} partitions...")

        for partition_key in sorted(partitions_to_dedup):
            partition_path = self.config.get_partition_path(table_config, partition_key)

            if not partition_path.exists():
                continue

            df = pd.read_parquet(partition_path)
            rows_before = len(df)
            df = df.drop_duplicates(subset=primary_key_cols, keep="last")
            rows_after = len(df)

            table = pa.Table.from_pandas(df, preserve_index=False)
            if date_columns:
                table = convert_date_columns_to_date32(table, date_columns)
            if pyarrow_schema:
                table = apply_schema_to_table(table, pyarrow_schema)
            pq.write_table(table, partition_path, compression="snappy")

            if rows_before != rows_after:
                logger.debug(
                    f"    -> Partition {partition_key}: {rows_before} -> {rows_after} rows "
                    f"(removed {rows_before - rows_after} duplicates)"
                )

    def _get_partition_totals(self, partition_dir: Path) -> Dict[str, Any]:
        """
        Calculate totals from all partition files in a directory.
        """
        import pyarrow.parquet as pq

        total_rows = 0
        total_size = 0
        total_uncompressed = 0
        total_columns = 0

        if not partition_dir.exists():
            return {"rows": 0, "file_size_bytes": 0, "columns": 0, "uncompressed_bytes": 0}

        all_partitions = list(partition_dir.glob("*.parquet"))

        for part_path in all_partitions:
            try:
                pf = pq.ParquetFile(part_path)
                meta = pf.metadata
                total_rows += meta.num_rows
                total_size += part_path.stat().st_size
                if total_columns == 0:
                    total_columns = len(pf.schema_arrow)
                for rg_idx in range(meta.num_row_groups):
                    rg = meta.row_group(rg_idx)
                    for col_idx in range(rg.num_columns):
                        total_uncompressed += rg.column(col_idx).total_uncompressed_size
            except Exception as e:
                logger.warning(f"    -> Skipping corrupt partition {part_path.name}: {e}")

        return {
            "rows": total_rows,
            "file_size_bytes": total_size,
            "partitions": len(all_partitions),
            "columns": total_columns,
            "uncompressed_bytes": total_uncompressed,
        }

    def _partitioned_sync(self, table_config: TableConfig) -> Dict[str, Any]:
        """
        Partitioned sync strategy.

        Downloads data and splits into monthly (or other granularity) partitions.
        Each partition is stored as separate Parquet file and merged independently.
        """
        logger.info(
            f"Partitioned sync: {table_config.name} "
            f"(by {table_config.partition_by}, {table_config.partition_granularity})"
        )

        partition_dir = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            filters_desc = ""
            if table_config.where_filters:
                filters_desc = f" (filters: {len(table_config.where_filters)})"
            logger.info(f"  -> Exporting from Keboola...{filters_desc}")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                where_filters=table_config.where_filters if table_config.where_filters else None,
            )

            if export_info["exported_rows"] == 0:
                logger.info("  -> No data exported")
                return self._get_partition_totals(partition_dir)

            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            logger.info(f"  -> Processing CSV in chunks ({export_info['exported_rows']} rows)...")

            partitions_seen = self._process_csv_to_partitions(
                tmp_csv_path, table_config, partition_dir,
                dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema,
            )

            self._deduplicate_partitions(
                table_config, partitions_seen,
                date_columns=date_columns, pyarrow_schema=pyarrow_schema,
            )

            totals = self._get_partition_totals(partition_dir)
            logger.info(
                f"  -> Partitioned sync complete: {totals.get('partitions', 0)} partitions on disk, "
                f"{totals['rows']} total rows"
            )

            return totals

        finally:
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()
