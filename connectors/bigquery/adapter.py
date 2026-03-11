"""
BigQuery data source adapter.

Implements the DataSource interface for Google BigQuery.
Reads tables via the BigQuery API, converts directly to Parquet files
using PyArrow (no CSV intermediate step).
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from src.config import get_config, TableConfig
from src.data_sync import DataSource, SyncState, _get_uncompressed_size
from src.parquet_manager import (
    convert_date_columns_to_date32,
    apply_schema_to_table,
)
from .client import create_client as create_bq_client


logger = logging.getLogger(__name__)


class BigQueryDataSource(DataSource):
    """
    Data source: Google BigQuery.

    Downloads data directly from BigQuery via PyArrow (no CSV step),
    writes to local Parquet files with schema enforcement.
    """

    def __init__(self):
        """Initialize BigQuery source with env var validation."""
        self.config = get_config()
        self.bq_client = create_bq_client()

    def get_column_metadata(self, table_id: str) -> Optional[Dict[str, Any]]:
        """Return BigQuery column metadata for schema generation.

        Returns:
            {"columns": {"col_name": {"source_type": "...", "description": "..."}}}
            or None if metadata unavailable.
        """
        raw = self.bq_client.get_table_metadata(table_id)
        column_types = raw.get("column_types", {})
        column_descriptions = raw.get("column_descriptions", {})

        if not column_types:
            return None

        result = {}
        for col_name, bq_type in column_types.items():
            entry = {"source_type": bq_type}
            if col_name in column_descriptions:
                entry["description"] = column_descriptions[col_name]
            result[col_name] = entry

        return {"columns": result}

    def discover_tables(self) -> List[Dict[str, Any]]:
        """Discover all available tables from BigQuery."""
        return self.bq_client.discover_all_tables()

    def get_source_name(self) -> str:
        """Display name of this data source."""
        return "Google BigQuery"

    def sync_table(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Synchronize table from BigQuery.

        Dispatches to the appropriate strategy based on table config.

        Args:
            table_config: Table configuration
            sync_state: Sync state manager

        Returns:
            Dictionary with sync result
        """
        logger.info(f"Syncing BQ table: {table_config.name} ({table_config.sync_strategy})")

        # Clear metadata cache for fresh types
        if table_config.id in self.bq_client.metadata_cache:
            del self.bq_client.metadata_cache[table_config.id]
            logger.debug(f"Cleared BQ metadata cache for {table_config.id}")

        try:
            if table_config.sync_strategy == "full_refresh":
                result = self._full_refresh(table_config)
            elif table_config.sync_strategy == "incremental":
                result = self._incremental_sync(table_config, sync_state)
            elif table_config.sync_strategy == "partitioned":
                result = self._partitioned_sync(table_config, sync_state)
            else:
                raise ValueError(f"Unknown sync strategy: {table_config.sync_strategy}")

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
            logger.error(f"Error syncing BQ table {table_config.name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "strategy": table_config.sync_strategy,
            }

    def _full_refresh(self, table_config: TableConfig) -> Dict[str, Any]:
        """
        Full refresh: stream table from BQ and write to Parquet in batches.

        Uses streaming (constant memory) instead of loading entire table into RAM.
        Each RecordBatch from BQ is written directly to disk via ParquetWriter.
        """
        logger.info(f"Full refresh (streaming): {table_config.name}")

        parquet_path = self.config.get_parquet_path(table_config)
        date_columns = self.bq_client.get_date_columns(table_config.id)
        pyarrow_schema = self.bq_client.get_pyarrow_schema(table_config.id)

        parquet_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream BQ results directly to Parquet file (constant memory)
        writer = None
        total_rows = 0
        num_columns = 0

        for batch in self.bq_client.read_table_streaming(
            table_config.id,
            columns=table_config.columns,
            row_filter=table_config.row_filter,
        ):
            if batch.num_rows == 0:
                continue

            # Convert batch to table for schema enforcement
            chunk = pa.Table.from_batches([batch])
            if date_columns:
                chunk = convert_date_columns_to_date32(chunk, date_columns)
            if pyarrow_schema:
                chunk = apply_schema_to_table(chunk, pyarrow_schema)

            if writer is None:
                writer = pq.ParquetWriter(
                    parquet_path, chunk.schema, compression="snappy",
                )
                num_columns = chunk.num_columns

            writer.write_table(chunk)
            total_rows += chunk.num_rows

            # Log progress every ~1M rows
            if total_rows % 1_000_000 < chunk.num_rows:
                logger.info(f"  -> {total_rows:,} rows written...")

        if writer:
            writer.close()

        file_size = parquet_path.stat().st_size if parquet_path.exists() else 0
        logger.info(
            f"Full refresh complete: {total_rows:,} rows, "
            f"{file_size / 1024 / 1024:.2f} MB"
        )

        return {
            "rows": total_rows,
            "columns": num_columns,
            "file_size_bytes": file_size,
            "uncompressed_bytes": _get_uncompressed_size(parquet_path) if total_rows > 0 else 0,
        }

    def _incremental_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Incremental sync: dispatch to column-based or partition-based strategy.
        """
        # If partition_by is set, use partitioned incremental
        if table_config.partition_by:
            return self._partitioned_sync(table_config, sync_state)

        # If incremental_column is set, use timestamp-based incremental
        if table_config.incremental_column:
            return self._incremental_column_sync(table_config, sync_state)

        # Fallback: full refresh (no incremental column configured)
        logger.warning(
            f"Table {table_config.name}: incremental strategy but no "
            f"incremental_column or partition_by configured, falling back to full refresh"
        )
        return self._full_refresh(table_config)

    def _incremental_column_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Timestamp-based incremental sync using incremental_column.

        Reads rows WHERE incremental_column > last_sync_value,
        merges with existing Parquet (dedup on primary key).
        """
        logger.info(
            f"Incremental column sync: {table_config.name} "
            f"(column: {table_config.incremental_column})"
        )

        parquet_path = self.config.get_parquet_path(table_config)
        date_columns = self.bq_client.get_date_columns(table_config.id)
        pyarrow_schema = self.bq_client.get_pyarrow_schema(table_config.id)

        # Determine since_value from last sync
        last_sync = sync_state.get_last_sync(table_config.id)

        if last_sync and parquet_path.exists():
            # Apply window: go back incremental_window_days from last sync
            last_sync_dt = datetime.fromisoformat(last_sync)
            window_days = table_config.incremental_window_days or 7
            since_dt = last_sync_dt - timedelta(days=window_days)
            since_value = since_dt.isoformat()

            logger.info(f"  -> Since: {since_value} (window: {window_days} days)")

            # Read incremental data
            new_data = self.bq_client.read_table_incremental(
                table_id=table_config.id,
                incremental_column=table_config.incremental_column,
                since_value=since_value,
                columns=table_config.columns,
            )

            if new_data.num_rows == 0:
                logger.info("  -> No new data since last sync")
                existing_pf = pq.ParquetFile(parquet_path)
                return {
                    "rows": existing_pf.metadata.num_rows,
                    "columns": len(existing_pf.schema_arrow),
                    "file_size_bytes": parquet_path.stat().st_size,
                    "uncompressed_bytes": _get_uncompressed_size(parquet_path),
                }

            # Merge with existing data
            logger.info(f"  -> Merging {new_data.num_rows} new rows with existing data")
            existing_table = pq.read_table(parquet_path)
            merged = self._merge_arrow_tables(
                existing_table, new_data, table_config.get_primary_key_columns()
            )

            # Apply schema enforcement
            if date_columns:
                merged = convert_date_columns_to_date32(merged, date_columns)
            if pyarrow_schema:
                merged = apply_schema_to_table(merged, pyarrow_schema)

            pq.write_table(merged, parquet_path, compression="snappy")

            file_size = parquet_path.stat().st_size
            logger.info(
                f"  -> Incremental sync complete: {merged.num_rows} total rows"
            )

            return {
                "rows": merged.num_rows,
                "columns": merged.num_columns,
                "file_size_bytes": file_size,
                "uncompressed_bytes": _get_uncompressed_size(parquet_path),
            }

        else:
            # First sync or no existing file -- full read
            logger.info("  -> First sync, reading all data")

            if table_config.max_history_days:
                since_dt = datetime.now() - timedelta(days=table_config.max_history_days)
                arrow_table = self.bq_client.read_table_incremental(
                    table_id=table_config.id,
                    incremental_column=table_config.incremental_column,
                    since_value=since_dt.isoformat(),
                    columns=table_config.columns,
                )
            else:
                arrow_table = self.bq_client.read_table(
                    table_config.id,
                    columns=table_config.columns,
                    row_filter=table_config.row_filter,
                )

            # Apply schema enforcement
            if date_columns:
                arrow_table = convert_date_columns_to_date32(arrow_table, date_columns)
            if pyarrow_schema:
                arrow_table = apply_schema_to_table(arrow_table, pyarrow_schema)

            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(arrow_table, parquet_path, compression="snappy")

            file_size = parquet_path.stat().st_size
            return {
                "rows": arrow_table.num_rows,
                "columns": arrow_table.num_columns,
                "file_size_bytes": file_size,
                "uncompressed_bytes": _get_uncompressed_size(parquet_path),
            }

    def _partitioned_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState,
    ) -> Dict[str, Any]:
        """
        Partition-based sync: read data by partition range and write partition files.
        """
        import pandas as pd

        partition_col = table_config.partition_by
        if not partition_col and table_config.incremental_column:
            partition_col = table_config.incremental_column

        if not partition_col:
            logger.warning(
                f"Table {table_config.name}: partitioned strategy but no "
                f"partition_by or incremental_column, falling back to full refresh"
            )
            return self._full_refresh(table_config)

        granularity = table_config.partition_granularity or "month"
        logger.info(
            f"Partitioned sync: {table_config.name} "
            f"(by {partition_col}, {granularity})"
        )

        partition_dir = self.config.get_parquet_path(table_config)
        date_columns = self.bq_client.get_date_columns(table_config.id)
        pyarrow_schema = self.bq_client.get_pyarrow_schema(table_config.id)

        # Determine time range
        last_sync = sync_state.get_last_sync(table_config.id)

        if last_sync:
            last_sync_dt = datetime.fromisoformat(last_sync)
            window_days = table_config.incremental_window_days or 7
            start_dt = last_sync_dt - timedelta(days=window_days)
            logger.info(f"  -> Reading from {start_dt.isoformat()} (window: {window_days} days)")
        else:
            if table_config.max_history_days:
                start_dt = datetime.now() - timedelta(days=table_config.max_history_days)
                logger.info(f"  -> First sync, limited to last {table_config.max_history_days} days")
            else:
                start_dt = None
                logger.info("  -> First sync, reading all data")

        # Read data from BigQuery
        if start_dt:
            arrow_table = self.bq_client.read_table_partitioned(
                table_id=table_config.id,
                partition_column=partition_col,
                start=start_dt.isoformat(),
                columns=table_config.columns,
            )
        else:
            arrow_table = self.bq_client.read_table(
                table_config.id,
                columns=table_config.columns,
                row_filter=table_config.row_filter,
            )

        if arrow_table.num_rows == 0:
            logger.info("  -> No data to sync")
            return self._get_partition_totals(partition_dir)

        logger.info(f"  -> Processing {arrow_table.num_rows} rows into partitions")

        # Convert to pandas for partitioning
        df = arrow_table.to_pandas()

        # Ensure partition column is datetime
        if not pd.api.types.is_datetime64_any_dtype(df[partition_col]):
            df[partition_col] = pd.to_datetime(df[partition_col], format="ISO8601", utc=True)

        # Create partition key
        if granularity == "month":
            df["_partition_key"] = df[partition_col].dt.strftime("%Y_%m")
        elif granularity == "day":
            df["_partition_key"] = df[partition_col].dt.strftime("%Y_%m_%d")
        elif granularity == "year":
            df["_partition_key"] = df[partition_col].dt.strftime("%Y")

        primary_key_cols = table_config.get_primary_key_columns()
        partitions_updated = set()

        for partition_key, group_df in df.groupby("_partition_key"):
            group_df = group_df.drop(columns=["_partition_key"])
            partition_path = self.config.get_partition_path(table_config, partition_key)
            partitions_updated.add(partition_key)

            # Merge with existing partition if it exists
            if partition_path.exists():
                existing_df = pd.read_parquet(partition_path)
                merged_df = pd.concat([existing_df, group_df], ignore_index=True)
                merged_df = merged_df.drop_duplicates(subset=primary_key_cols, keep="last")
            else:
                merged_df = group_df

            # Write partition
            table = pa.Table.from_pandas(merged_df, preserve_index=False)
            if date_columns:
                table = convert_date_columns_to_date32(table, date_columns)
            if pyarrow_schema:
                table = apply_schema_to_table(table, pyarrow_schema)
            pq.write_table(table, partition_path, compression="snappy")

        logger.info(f"  -> Partitioned sync complete: {len(partitions_updated)} partitions updated")

        return self._get_partition_totals(partition_dir)

    def _merge_arrow_tables(
        self,
        existing: pa.Table,
        new_data: pa.Table,
        primary_key: List[str],
    ) -> pa.Table:
        """
        Merge two Arrow tables with deduplication on primary key.

        New data overwrites existing rows with the same primary key.

        Args:
            existing: Existing data
            new_data: New/changed data
            primary_key: List of PK column names

        Returns:
            Merged PyArrow Table
        """
        import pandas as pd

        existing_df = existing.to_pandas()
        new_df = new_data.to_pandas()

        # Concat and dedup (keep last = new data wins)
        merged_df = pd.concat([existing_df, new_df], ignore_index=True)
        merged_df = merged_df.drop_duplicates(subset=primary_key, keep="last")

        return pa.Table.from_pandas(merged_df, preserve_index=False)

    def _get_partition_totals(self, partition_dir: Path) -> Dict[str, Any]:
        """
        Calculate totals from all partition files in a directory.
        """
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
                logger.warning(f"Skipping corrupt partition {part_path.name}: {e}")

        return {
            "rows": total_rows,
            "file_size_bytes": total_size,
            "partitions": len(all_partitions),
            "columns": total_columns,
            "uncompressed_bytes": total_uncompressed,
        }


def create_data_source() -> BigQueryDataSource:
    """Factory function for dynamic import compatibility."""
    return BigQueryDataSource()
