"""
Data Synchronization Manager

Orchestrates data synchronization from Keboola to local Parquet files.

Main functions:
1. Tracking sync state (when was last synchronization)
2. Implementation of sync strategies (full_refresh vs incremental)
3. Sync single table or all tables at once
4. Progress tracking and error handling
5. Preparation for future GCS scenario (data source abstraction)

Sync State:
- Stored in data/metadata/sync_state.json
- Contains timestamp of last synchronization for each table
- Used for incremental sync (changedSince parameter)
"""

import json
import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import pyarrow as pa
from tqdm import tqdm

from .config import get_config, TableConfig
from .keboola_client import create_client as create_keboola_client
from .parquet_manager import (
    create_parquet_manager,
    _convert_column,
    convert_date_columns_to_date32,
    apply_schema_to_table
)


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
            Dictionary with sync state or empty dict if file doesn't exist
        """
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error loading sync state: {e}")
                return self._empty_state()
        else:
            return self._empty_state()

    def _empty_state(self) -> Dict[str, Any]:
        """
        Create empty sync state.

        Returns:
            Dictionary with empty sync state
        """
        return {
            "last_updated": None,
            "tables": {}
        }

    def _save_state(self):
        """
        Save sync state to disk.
        """
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
            logger.debug("Sync state saved")
        except Exception as e:
            logger.error(f"Error saving sync state: {e}")

    def get_last_sync(self, table_id: str) -> Optional[str]:
        """
        Get timestamp of last synchronization for given table.

        Args:
            table_id: Table ID

        Returns:
            ISO timestamp or None if table hasn't been synchronized yet
        """
        return self.state["tables"].get(table_id, {}).get("last_sync")

    def update_sync(
        self,
        table_id: str,
        table_name: str,
        strategy: str,
        rows: int,
        file_size_bytes: int,
        columns: int = 0,
        uncompressed_bytes: int = 0
    ):
        """
        Update sync state for given table.

        Args:
            table_id: Table ID
            table_name: Table name
            strategy: Sync strategy used
            rows: Number of rows after sync
            file_size_bytes: Parquet file size (compressed, on disk)
            columns: Number of columns
            uncompressed_bytes: Uncompressed data size
        """
        now = datetime.now().isoformat()

        self.state["last_updated"] = now
        self.state["tables"][table_id] = {
            "last_sync": now,
            "table_name": table_name,
            "strategy": strategy,
            "rows": rows,
            "columns": columns,
            "file_size_mb": round(file_size_bytes / 1024 / 1024, 2),
            "uncompressed_mb": round(uncompressed_bytes / 1024 / 1024, 2)
        }

        self._save_state()


class DataSource(ABC):
    """
    Abstract class for data source.

    Allows adding GCS source in the future without changing rest of the code.
    """

    @abstractmethod
    def sync_table(
        self,
        table_config: TableConfig,
        sync_state: SyncState
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


class LocalKeboolaSource(DataSource):
    """
    Data source: Direct download from Keboola Storage API.

    Current implementation - downloads data directly from Keboola project.
    """

    def __init__(self):
        """Initialize Keboola source."""
        self.config = get_config()
        self.keboola_client = create_keboola_client()
        self.parquet_manager = create_parquet_manager()

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
        sync_state: SyncState
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
                uncompressed_bytes=result.get("uncompressed_bytes", 0)
            )

            return {
                "success": True,
                "rows": result["rows"],
                "strategy": table_config.sync_strategy,
                "file_size_mb": result["file_size_bytes"] / 1024 / 1024
            }

        except Exception as e:
            logger.error(f"Error syncing table {table_config.name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "strategy": table_config.sync_strategy
            }

    def _full_refresh(self, table_config: TableConfig) -> Dict[str, Any]:
        """
        Full refresh sync strategy.

        Downloads entire table and replaces existing Parquet file.

        Args:
            table_config: Table configuration

        Returns:
            Dictionary with sync result
        """
        logger.info(f"Full refresh: {table_config.name}")

        # Paths
        parquet_path = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        # Use staging directory for CSV download
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.csv',
            delete=False,
            dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export from Keboola to CSV
            filters_desc = ""
            if table_config.where_filters:
                filters_desc = f" (filters: {len(table_config.where_filters)})"
            logger.info(f"  → Exporting from Keboola...{filters_desc}")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                where_filters=table_config.where_filters if table_config.where_filters else None
            )

            # 2. Get dtypes for proper conversion
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. Convert CSV -> Parquet
            logger.info(f"  → Converting to Parquet...")
            parquet_info = self.parquet_manager.csv_to_parquet(
                csv_path=tmp_csv_path,
                parquet_path=parquet_path,
                dtypes=dtypes,
                table_id=table_config.id,
                date_columns=date_columns,
                pyarrow_schema=pyarrow_schema
            )

            return {
                "rows": parquet_info["rows"],
                "file_size_bytes": parquet_info["parquet_size_bytes"],
                "columns": parquet_info.get("columns", 0),
                "uncompressed_bytes": _get_uncompressed_size(parquet_path)
            }

        finally:
            # Delete temporary CSV
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _incremental_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState
    ) -> Dict[str, Any]:
        """
        Incremental sync strategy.

        Downloads only changed rows using changedSince API parameter.
        If partition_by is configured, outputs are partitioned (like partitioned strategy).
        Otherwise, merges into a single Parquet file.

        Args:
            table_config: Table configuration
            sync_state: Sync state manager

        Returns:
            Dictionary with sync result
        """
        # If partition_by is configured, use partitioned output
        if table_config.partition_by:
            return self._incremental_partitioned_sync(table_config, sync_state)

        # Otherwise, use single-file output
        return self._incremental_single_file_sync(table_config, sync_state)

    def _incremental_single_file_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState
    ) -> Dict[str, Any]:
        """
        Incremental sync to a single Parquet file (no partitioning).

        Args:
            table_config: Table configuration
            sync_state: Sync state manager

        Returns:
            Dictionary with sync result
        """
        logger.info(f"Incremental sync (single file): {table_config.name}")

        # Paths
        parquet_path = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        # Determine timestamp for changedSince
        last_sync = sync_state.get_last_sync(table_config.id)

        if last_sync:
            # Add incremental window (backtrack N days)
            last_sync_dt = datetime.fromisoformat(last_sync)
            window_days = table_config.incremental_window_days or 7
            changed_since_dt = last_sync_dt - timedelta(days=window_days)
            changed_since = changed_since_dt.isoformat()

            logger.info(
                f"  → ChangedSince: {changed_since} "
                f"(window: {window_days} days)"
            )
        else:
            # First sync
            if table_config.max_history_days:
                # Limit initial load to max_history_days
                changed_since_dt = datetime.now() - timedelta(days=table_config.max_history_days)
                changed_since = changed_since_dt.isoformat()
                logger.info(
                    f"  → First sync, limited to last {table_config.max_history_days} days "
                    f"(changedSince: {changed_since})"
                )
            else:
                # Download everything
                logger.info("  → First sync, downloading all data...")
                changed_since = None

        # Temporary CSV in staging directory
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.csv',
            delete=False,
            dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export changed data from Keboola
            logger.info(f"  → Exporting changes from Keboola...")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                changed_since=changed_since
            )

            # If there were no changes, done
            if export_info["exported_rows"] == 0:
                logger.info("  → No changes since last synchronization")

                # Return info from existing Parquet
                if parquet_path.exists():
                    existing_info = self.parquet_manager.get_parquet_info(parquet_path)
                    return {
                        "rows": existing_info["rows"],
                        "file_size_bytes": existing_info["file_size_bytes"],
                        "columns": existing_info.get("columns", 0),
                        "uncompressed_bytes": _get_uncompressed_size(parquet_path)
                    }
                else:
                    # File doesn't exist and there are no new rows - weird but OK
                    return {"rows": 0, "file_size_bytes": 0, "columns": 0, "uncompressed_bytes": 0}

            # 2. Get dtypes and date columns
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. If Parquet exists, merge; otherwise create new
            if parquet_path.exists():
                logger.info(
                    f"  → Merging {export_info['exported_rows']} changes into Parquet..."
                )

                # Merge to temporary Parquet in staging, then rename
                with tempfile.NamedTemporaryFile(
                    mode='w',
                    suffix='.parquet',
                    delete=False,
                    dir=staging_dir
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
                        pyarrow_schema=pyarrow_schema
                    )

                    # Overwrite original Parquet
                    tmp_parquet_path.replace(parquet_path)

                    return {
                        "rows": merge_info["total_rows"],
                        "file_size_bytes": parquet_path.stat().st_size,
                        "columns": merge_info.get("total_columns", 0),
                        "uncompressed_bytes": _get_uncompressed_size(parquet_path)
                    }

                finally:
                    # Cleanup temporary parquet if still exists
                    if tmp_parquet_path.exists():
                        tmp_parquet_path.unlink()

            else:
                # First sync - create new Parquet
                logger.info(f"  → Creating new Parquet...")

                parquet_info = self.parquet_manager.csv_to_parquet(
                    csv_path=tmp_csv_path,
                    parquet_path=parquet_path,
                    dtypes=dtypes,
                    table_id=table_config.id,
                    date_columns=date_columns,
                    pyarrow_schema=pyarrow_schema
                )

                return {
                    "rows": parquet_info["rows"],
                    "file_size_bytes": parquet_info["parquet_size_bytes"],
                    "columns": parquet_info.get("columns", 0),
                    "uncompressed_bytes": _get_uncompressed_size(parquet_path)
                }

        finally:
            # Cleanup temporary CSV
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _incremental_partitioned_sync(
        self,
        table_config: TableConfig,
        sync_state: SyncState
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

        Args:
            table_config: Table configuration (must have partition_by set)
            sync_state: Sync state manager

        Returns:
            Dictionary with sync result
        """
        import pandas as pd

        logger.info(
            f"Incremental sync (partitioned): {table_config.name} "
            f"(by {table_config.partition_by}, {table_config.partition_granularity})"
        )

        # Paths
        partition_dir = self.config.get_parquet_path(table_config)
        staging_dir = self.config.get_staging_path()

        # Determine timestamp for changedSince
        last_sync = sync_state.get_last_sync(table_config.id)

        # For initial load (no last_sync), always use chunked approach to avoid disk space issues
        if not last_sync:
            return self._chunked_initial_load(table_config, partition_dir, staging_dir)

        # Regular incremental sync (has last_sync)
        # Add incremental window (backtrack N days)
        last_sync_dt = datetime.fromisoformat(last_sync)
        window_days = table_config.incremental_window_days or 7
        changed_since_dt = last_sync_dt - timedelta(days=window_days)
        changed_since = changed_since_dt.isoformat()

        logger.info(
            f"  → ChangedSince: {changed_since} "
            f"(window: {window_days} days)"
        )

        # Temporary CSV in staging directory
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.csv',
            delete=False,
            dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export changed data from Keboola
            logger.info(f"  → Exporting changes from Keboola...")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                changed_since=changed_since
            )

            # If there were no changes, return info from existing partitions
            if export_info["exported_rows"] == 0:
                logger.info("  → No changes since last synchronization")
                return self._get_partition_totals(partition_dir)

            # 2. Get dtypes for proper conversion
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. Process in chunks (same as _partitioned_sync)
            logger.info(f"  → Processing {export_info['exported_rows']} changed rows...")

            partitions_updated = self._process_csv_to_partitions(
                tmp_csv_path, table_config, partition_dir,
                dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema
            )

            # 4. Deduplicate only updated partitions
            self._deduplicate_partitions(table_config, partitions_updated,
                                        date_columns=date_columns, pyarrow_schema=pyarrow_schema)

            logger.info(f"  → Incremental sync complete, {len(partitions_updated)} partitions updated")

            # 5. Return totals from all partitions
            return self._get_partition_totals(partition_dir)

        finally:
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()

    def _chunked_initial_load(
        self,
        table_config: TableConfig,
        partition_dir: Path,
        staging_dir: Path
    ) -> Dict[str, Any]:
        """
        Chunked initial load for large tables.

        Downloads data in time-window chunks to avoid filling up disk space.
        Each chunk has 1-day overlap with the next to ensure no data is lost
        at boundaries. Deduplication removes any duplicates from overlaps.

        If max_history_days is set, downloads exactly that many days.
        If not set, iterates backwards until an empty chunk is found.

        Args:
            table_config: Table configuration
            partition_dir: Directory for partition files
            staging_dir: Directory for temporary CSV files

        Returns:
            Dictionary with sync result
        """
        chunk_days = table_config.initial_load_chunk_days
        max_history_days = table_config.max_history_days
        overlap_days = 1  # 1-day overlap to ensure no data loss at boundaries
        max_chunks_safety = 120  # Safety limit: ~10 years with 30-day chunks

        now = datetime.now()

        if max_history_days:
            # Known history limit - calculate exact number of chunks
            num_chunks = (max_history_days + chunk_days - 1) // chunk_days
            logger.info(
                f"  → CHUNKED INITIAL LOAD: {max_history_days} days in {num_chunks} chunks "
                f"of {chunk_days} days each (with {overlap_days}-day overlap)"
            )
        else:
            # Unknown history - will iterate until empty chunk
            num_chunks = None
            logger.info(
                f"  → CHUNKED INITIAL LOAD: iterating backwards in {chunk_days}-day chunks "
                f"until no more data (with {overlap_days}-day overlap)"
            )

        # Clean staging before starting
        self._cleanup_staging()

        # Get dtypes once before the loop for proper type conversion
        dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
        date_columns = self.keboola_client.get_date_columns(table_config.id)
        pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

        all_partitions_updated = set()
        chunk_idx = 0
        consecutive_empty_chunks = 0

        # Process chunks from newest to oldest (so we get recent data first)
        while True:
            # Safety check
            if chunk_idx >= max_chunks_safety:
                logger.warning(f"  → Reached safety limit of {max_chunks_safety} chunks, stopping")
                break

            # Check if we've processed all planned chunks (when max_history_days is set)
            if num_chunks is not None and chunk_idx >= num_chunks:
                break

            # Calculate time window for this chunk
            # Start from now and go backwards
            chunk_end_offset = chunk_idx * chunk_days
            chunk_start_offset = chunk_end_offset + chunk_days + overlap_days

            chunk_end = now - timedelta(days=chunk_end_offset) if chunk_idx > 0 else None
            chunk_start = now - timedelta(days=chunk_start_offset)

            # For known history limit, don't go beyond max_history_days
            if max_history_days and chunk_start_offset > max_history_days:
                chunk_start = now - timedelta(days=max_history_days)

            chunk_label = f"{chunk_idx + 1}" if num_chunks is None else f"{chunk_idx + 1}/{num_chunks}"
            logger.info(
                f"  → Chunk {chunk_label}: "

                f"{chunk_start.strftime('%Y-%m-%d')} to "
                f"{chunk_end.strftime('%Y-%m-%d') if chunk_end else 'now'}"
            )

            # Create temporary CSV for this chunk
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.csv',
                delete=False,
                dir=staging_dir
            ) as tmp_file:
                tmp_csv_path = Path(tmp_file.name)

            try:
                # Export this chunk from Keboola
                export_info = self.keboola_client.export_table(
                    table_id=table_config.id,
                    output_path=tmp_csv_path,
                    changed_since=chunk_start.isoformat(),
                    changed_until=chunk_end.isoformat() if chunk_end else None
                )

                if export_info["exported_rows"] == 0:
                    logger.info(f"    → No data in this chunk")
                    consecutive_empty_chunks += 1
                    # If no max_history_days and we get 2 consecutive empty chunks, we're done
                    if num_chunks is None and consecutive_empty_chunks >= 2:
                        logger.info(f"  → Found {consecutive_empty_chunks} consecutive empty chunks, assuming end of history")
                        break
                    chunk_idx += 1
                    continue

                # Reset empty chunk counter on non-empty chunk
                consecutive_empty_chunks = 0
                logger.info(f"    → Exported {export_info['exported_rows']} rows")

                # Process CSV to partitions
                partitions_updated = self._process_csv_to_partitions(
                    tmp_csv_path, table_config, partition_dir,
                    dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema
                )
                all_partitions_updated.update(partitions_updated)

                logger.info(f"    → Processed into {len(partitions_updated)} partitions")

            finally:
                # Always clean up staging after each chunk
                if tmp_csv_path.exists():
                    tmp_csv_path.unlink()

            chunk_idx += 1

        # Final deduplication of all updated partitions
        # This removes duplicates from overlapping chunks
        if all_partitions_updated:
            logger.info(
                f"  → Final deduplication of {len(all_partitions_updated)} partitions "
                f"(removing duplicates from {overlap_days}-day overlaps)..."
            )
            self._deduplicate_partitions(table_config, all_partitions_updated,
                                        date_columns=date_columns, pyarrow_schema=pyarrow_schema)

        logger.info(
            f"  → Chunked initial load complete: {len(all_partitions_updated)} partitions"
        )

        return self._get_partition_totals(partition_dir)

    def _process_csv_to_partitions(
        self,
        csv_path: Path,
        table_config: TableConfig,
        partition_dir: Path,
        dtypes: Optional[Dict[str, str]] = None,
        date_columns: Optional[List[str]] = None,
        pyarrow_schema: Optional[pa.Schema] = None
    ) -> set:
        """
        Process CSV file and write to partition files.

        Args:
            csv_path: Path to CSV file
            table_config: Table configuration
            partition_dir: Directory for partition files
            dtypes: Dictionary with pandas dtypes from Keboola metadata
            date_columns: List of DATE-only columns to convert to DATE32
            pyarrow_schema: Optional PyArrow schema to enforce (prevents null-type columns)

        Returns:
            Set of partition keys that were updated
        """
        import pandas as pd
        import pyarrow as pa
        import pyarrow.parquet as pq

        partition_col = table_config.partition_by
        granularity = table_config.partition_granularity or "month"

        partitions_updated = set()
        chunk_size = 500000  # 500k rows per pandas chunk

        chunk_num = 0
        # Read CSV with dtype=str to prevent pandas from guessing types
        for chunk_df in pd.read_csv(csv_path, chunksize=chunk_size, dtype=str):
            chunk_num += 1
            logger.debug(f"    → Processing pandas chunk {chunk_num} ({len(chunk_df)} rows)...")

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
                    chunk_df[partition_col], format='ISO8601', utc=True
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
                    # Append to existing partition
                    existing_df = pd.read_parquet(partition_path)
                    merged_df = pd.concat([existing_df, partition_df], ignore_index=True)
                    # Convert to PyArrow table for DATE32 conversion
                    table = pa.Table.from_pandas(merged_df, preserve_index=False)
                    if date_columns:
                        table = convert_date_columns_to_date32(table, date_columns)
                    if pyarrow_schema:
                        table = apply_schema_to_table(table, pyarrow_schema)
                    pq.write_table(table, partition_path, compression="snappy")
                else:
                    # Create new partition
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
        pyarrow_schema: Optional[pa.Schema] = None
    ):
        """
        Deduplicate partition files based on primary key.

        Args:
            table_config: Table configuration
            partitions_to_dedup: Set of partition keys to deduplicate
            date_columns: List of DATE-only columns to convert to DATE32
            pyarrow_schema: Optional PyArrow schema to enforce (prevents null-type columns)
        """
        import pandas as pd
        import pyarrow as pa
        import pyarrow.parquet as pq

        primary_key_cols = table_config.get_primary_key_columns()

        logger.info(f"  → Deduplicating {len(partitions_to_dedup)} partitions...")

        for partition_key in sorted(partitions_to_dedup):
            partition_path = self.config.get_partition_path(table_config, partition_key)

            if not partition_path.exists():
                continue

            df = pd.read_parquet(partition_path)
            rows_before = len(df)
            df = df.drop_duplicates(subset=primary_key_cols, keep='last')
            rows_after = len(df)

            # Use full PyArrow pipeline instead of df.to_parquet()
            table = pa.Table.from_pandas(df, preserve_index=False)
            if date_columns:
                table = convert_date_columns_to_date32(table, date_columns)
            if pyarrow_schema:
                table = apply_schema_to_table(table, pyarrow_schema)
            pq.write_table(table, partition_path, compression="snappy")

            if rows_before != rows_after:
                logger.debug(
                    f"    → Partition {partition_key}: {rows_before} → {rows_after} rows "
                    f"(removed {rows_before - rows_after} duplicates)"
                )

    def _get_partition_totals(self, partition_dir: Path) -> Dict[str, Any]:
        """
        Calculate totals from all partition files in a directory.

        Args:
            partition_dir: Directory containing partition .parquet files

        Returns:
            Dictionary with total rows, size, columns, uncompressed size
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
                logger.warning(f"    → Skipping corrupt partition {part_path.name}: {e}")

        return {
            "rows": total_rows,
            "file_size_bytes": total_size,
            "partitions": len(all_partitions),
            "columns": total_columns,
            "uncompressed_bytes": total_uncompressed
        }

    def _partitioned_sync(self, table_config: TableConfig) -> Dict[str, Any]:
        """
        Partitioned sync strategy.

        Downloads data and splits into monthly (or other granularity) partitions.
        Each partition is stored as separate Parquet file and merged independently.
        Uses chunked reading to handle large files without running out of memory.

        Args:
            table_config: Table configuration

        Returns:
            Dictionary with sync result
        """
        logger.info(f"Partitioned sync: {table_config.name} (by {table_config.partition_by}, {table_config.partition_granularity})")

        # Paths
        partition_dir = self.config.get_parquet_path(table_config)  # Returns directory for partitioned
        staging_dir = self.config.get_staging_path()

        # Use staging directory for CSV download
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.csv',
            delete=False,
            dir=staging_dir
        ) as tmp_file:
            tmp_csv_path = Path(tmp_file.name)

        try:
            # 1. Export from Keboola to CSV
            filters_desc = ""
            if table_config.where_filters:
                filters_desc = f" (filters: {len(table_config.where_filters)})"
            logger.info(f"  → Exporting from Keboola...{filters_desc}")
            export_info = self.keboola_client.export_table(
                table_id=table_config.id,
                output_path=tmp_csv_path,
                where_filters=table_config.where_filters if table_config.where_filters else None
            )

            if export_info["exported_rows"] == 0:
                logger.info("  → No data exported")
                return self._get_partition_totals(partition_dir)

            # 2. Get dtypes for proper conversion
            dtypes = self.keboola_client.get_pandas_dtypes(table_config.id)
            date_columns = self.keboola_client.get_date_columns(table_config.id)
            pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)

            # 3. Process CSV in chunks using helper
            logger.info(f"  → Processing CSV in chunks ({export_info['exported_rows']} rows)...")

            partitions_seen = self._process_csv_to_partitions(
                tmp_csv_path, table_config, partition_dir,
                dtypes=dtypes, date_columns=date_columns, pyarrow_schema=pyarrow_schema
            )

            # 4. Final deduplication pass
            self._deduplicate_partitions(table_config, partitions_seen,
                                        date_columns=date_columns, pyarrow_schema=pyarrow_schema)

            # 5. Get totals from all partitions
            totals = self._get_partition_totals(partition_dir)
            logger.info(f"  → Partitioned sync complete: {totals.get('partitions', 0)} partitions on disk, {totals['rows']} total rows")

            return totals

        finally:
            # Delete temporary CSV
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()


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

        # Data source - currently always LocalKeboolaSource
        # In future: if config.data_source == "gcs": use GCSSource
        self.data_source = LocalKeboolaSource()

    def _generate_schema_yaml(self):
        """
        Generate schema.yml file with actual table schemas from Parquet files.

        This file is auto-generated and contains:
        - Table names and descriptions
        - Column names, types (from Parquet), and descriptions (from Keboola)
        - Primary keys

        Output: DOCS_OUTPUT_DIR/schema.yml (default: ./docs/schema.yml)
        """
        import yaml
        import pyarrow.parquet as pq
        from datetime import datetime

        logger.info("Generating schema.yml from synced tables...")

        schema_data = {
            "_metadata": {
                "generated_at": datetime.now().isoformat(),
                "note": "AUTO-GENERATED - DO NOT EDIT. This file contains actual table schemas from synced Parquet files.",
                "source": "Keboola Storage API",
                "generator": "src/data_sync.py::DataSyncManager._generate_schema_yaml()"
            },
            "tables": {}
        }

        # Get Keboola client for metadata
        keboola_client = self.data_source.keboola_client

        # Process each table in configuration
        for table_config in self.config.tables:
            try:
                # Get Parquet file path
                parquet_path = self.config.get_parquet_path(table_config)

                # Skip if Parquet doesn't exist (table not synced yet)
                if table_config.partition_by:
                    # For partitioned tables, check directory exists
                    if not parquet_path.exists() or not list(parquet_path.glob("*.parquet")):
                        logger.debug(f"  Skipping {table_config.name} (not synced yet)")
                        continue
                    # Read schema from first partition
                    first_partition = next(parquet_path.glob("*.parquet"))
                    pf = pq.ParquetFile(first_partition)
                else:
                    # For single-file tables
                    if not parquet_path.exists():
                        logger.debug(f"  Skipping {table_config.name} (not synced yet)")
                        continue
                    pf = pq.ParquetFile(parquet_path)

                # Get PyArrow schema
                arrow_schema = pf.schema_arrow

                # Get Keboola metadata for descriptions
                keboola_metadata = keboola_client.get_table_metadata(table_config.id)
                column_metadata = keboola_metadata.get("column_metadata", {})

                # Provider priority for metadata
                PROVIDER_PRIORITY = ["user", "ai-metadata-enrichment", "keboola.snowflake-transformation"]

                # Extract column information
                columns = []
                for field in arrow_schema:
                    col_name = field.name
                    pyarrow_type = str(field.type)

                    # Get Keboola type and description from metadata
                    keboola_type = "STRING"  # default
                    description = None

                    col_meta_list = column_metadata.get(col_name, [])
                    if isinstance(col_meta_list, list):
                        # Extract basetype and description by provider priority
                        basetype_by_provider = {}
                        description_by_provider = {}

                        for meta_entry in col_meta_list:
                            provider = meta_entry.get("provider", "")
                            key = meta_entry.get("key", "")
                            value = meta_entry.get("value", "")

                            if key == "KBC.datatype.basetype":
                                basetype_by_provider[provider] = value.upper()
                            elif key == "KBC.datatype.type" and provider not in basetype_by_provider:
                                basetype_by_provider[provider] = value.upper()
                            elif key == "KBC.description":
                                description_by_provider[provider] = value

                        # Apply cascade for basetype
                        for provider in PROVIDER_PRIORITY:
                            if provider in basetype_by_provider:
                                keboola_type = basetype_by_provider[provider]
                                break

                        # Apply cascade for description
                        for provider in PROVIDER_PRIORITY:
                            if provider in description_by_provider:
                                description = description_by_provider[provider]
                                break

                    column_info = {
                        "name": col_name,
                        "type": pyarrow_type,
                        "keboola_type": keboola_type
                    }

                    if description:
                        column_info["description"] = description

                    columns.append(column_info)

                # Get primary key
                primary_key = table_config.get_primary_key_columns()

                # Add table to schema
                table_info = {
                    "table_id": table_config.id,
                    "description": table_config.description,
                    "primary_key": primary_key,
                    "sync_strategy": table_config.sync_strategy,
                    "columns": columns
                }

                if table_config.partition_by:
                    table_info["partitioned_by"] = table_config.partition_by

                schema_data["tables"][table_config.name] = table_info

                logger.debug(f"  ✅ {table_config.name}: {len(columns)} columns")

            except Exception as e:
                logger.warning(f"  ⚠️  Error processing {table_config.name}: {e}")

        # Split tables into core (no dataset) and per-dataset groups
        core_tables = {}
        dataset_tables = {}  # {dataset_name: {table_name: table_info}}
        for table_name, table_info in schema_data["tables"].items():
            # Find matching table_config to check dataset field
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
                    "generated_at": generated_at,
                    "note": "AUTO-GENERATED - DO NOT EDIT.",
                    "source": "Keboola Storage API",
                    "generator": "src/data_sync.py::DataSyncManager._generate_schema_yaml()"
                },
                "tables": tables
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
                f.write("# - Keboola types and descriptions (from Keboola metadata)\n")
                f.write("# - Primary keys and sync strategies\n")
                f.write("#\n")
                f.write("# For architectural documentation and relationships, see data_description.md\n")
                f.write("\n")
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Write core schema.yml
        schema_file = self.config.docs_output_dir / "schema.yml"
        _write_schema_file(schema_file, core_tables)
        logger.info(f"✅ Core schema YAML: {len(core_tables)} tables → {schema_file}")

        # Write per-dataset schema files
        for ds_name, ds_tables in dataset_tables.items():
            ds_schema_file = self.config.docs_output_dir / "datasets" / ds_name / "schema.yml"
            _write_schema_file(ds_schema_file, ds_tables, note=f"Dataset: {ds_name}")
            logger.info(f"✅ Dataset schema YAML: {len(ds_tables)} tables → {ds_schema_file}")

        total = len(core_tables) + sum(len(t) for t in dataset_tables.values())
        logger.info(f"✅ Schema generation complete: {total} tables total")

        return schema_file

    def sync_table(self, table_id: str) -> Dict[str, Any]:
        """
        Synchronize single table by ID.

        Args:
            table_id: Table ID to synchronize

        Returns:
            Dictionary with sync result
        """
        # Find table config
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
        # Select tables to synchronize
        if tables:
            table_configs = [
                self.config.get_table_config(tid)
                for tid in tables
            ]
            table_configs = [tc for tc in table_configs if tc is not None]
        else:
            table_configs = self.config.tables

        logger.info(f"Synchronizing {len(table_configs)} tables...")

        # Sync each table with progress bar
        results = {}
        with tqdm(table_configs, desc="Syncing tables") as pbar:
            for table_config in pbar:
                pbar.set_description(f"Sync: {table_config.name}")

                result = self.data_source.sync_table(table_config, self.sync_state)
                results[table_config.id] = result

                # Show result in progress bar
                if result["success"]:
                    pbar.write(
                        f"✅ {table_config.name}: {result['rows']:,} rows, "
                        f"{result['file_size_mb']:.2f} MB"
                    )
                else:
                    pbar.write(f"❌ {table_config.name}: {result['error']}")

        # Summary
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

        return results


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
    """
    if source_type is None:
        source_type = get_config().data_source

    if source_type in ("local", "keboola"):
        return LocalKeboolaSource()

    # Try adapter factory for other types
    from .adapters import create_data_source as adapter_factory
    return adapter_factory(source_type)


if __name__ == "__main__":
    # CLI interface for sync
    import sys

    print("🔄 Keboola Data Sync")

    try:
        manager = create_sync_manager()

        # If there are arguments, sync only these tables
        if len(sys.argv) > 1:
            tables_to_sync = sys.argv[1:]
            print(f"\nSynchronizing selected tables: {', '.join(tables_to_sync)}")
            results = manager.sync_all(tables=tables_to_sync)
        else:
            # Sync all tables
            print("\nSynchronizing all tables...")
            results = manager.sync_all()

        # Result
        success_count = sum(1 for r in results.values() if r["success"])
        total_count = len(results)

        if success_count == total_count:
            print(f"\n✅ All {total_count} tables synchronized successfully!")
            sys.exit(0)
        else:
            print(
                f"\n⚠️  {success_count}/{total_count} tables synchronized. "
                f"Check logs for details."
            )
            sys.exit(1)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
