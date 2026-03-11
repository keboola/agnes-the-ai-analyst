"""
Comprehensive unit tests for the BigQuery data source adapter.

Tests the BigQueryDataSource class from connectors/bigquery/adapter.py
with all external dependencies (BigQueryClient, config, parquet_manager) mocked.

The google-cloud-bigquery package is not installed in test environments,
so we install stub modules in sys.modules before importing the adapter.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# ---------------------------------------------------------------------------
# Stub google.cloud.bigquery before any connector import
# ---------------------------------------------------------------------------
_bq_stub = MagicMock()
sys.modules.setdefault("google", _bq_stub)
sys.modules.setdefault("google.cloud", _bq_stub)
sys.modules.setdefault("google.cloud.bigquery", _bq_stub)

from src.config import TableConfig  # noqa: E402
from src.data_sync import SyncState  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_parquet_dir(tmp_path):
    """Provide a temporary directory for Parquet file output."""
    parquet_dir = tmp_path / "parquet" / "test_bucket"
    parquet_dir.mkdir(parents=True)
    return parquet_dir


@pytest.fixture
def mock_config(tmp_parquet_dir):
    """Create a mock Config object that returns paths inside tmp_parquet_dir."""
    config = MagicMock()
    config.get_parquet_path = MagicMock()
    config.get_partition_path = MagicMock()
    config.get_metadata_path.return_value = tmp_parquet_dir.parent / "metadata"
    return config


@pytest.fixture
def mock_bq_client():
    """Create a mock BigQueryClient with sensible defaults."""
    client = MagicMock()
    client.metadata_cache = {}
    client.get_date_columns.return_value = []
    client.get_pyarrow_schema.return_value = None
    return client


@pytest.fixture
def sync_state(tmp_path):
    """Create a real SyncState backed by a temp JSON file."""
    state_file = tmp_path / "metadata" / "sync_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    return SyncState(state_file)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_table_config(
    *,
    table_id: str = "project.dataset.orders",
    name: str = "orders",
    primary_key: str = "id",
    sync_strategy: str = "full_refresh",
    incremental_column: str | None = None,
    incremental_window_days: int | None = None,
    partition_by: str | None = None,
    partition_granularity: str | None = None,
    max_history_days: int | None = None,
) -> TableConfig:
    """Helper to build a TableConfig with safe defaults."""
    return TableConfig(
        id=table_id,
        name=name,
        description="Test table",
        primary_key=primary_key,
        sync_strategy=sync_strategy,
        incremental_column=incremental_column,
        incremental_window_days=incremental_window_days,
        partition_by=partition_by,
        partition_granularity=partition_granularity,
        max_history_days=max_history_days,
    )


def _sample_arrow_table(ids: list[int], names: list[str]) -> pa.Table:
    """Build a small PyArrow Table with id and name columns."""
    return pa.table({"id": ids, "name": names})


def _create_adapter(mock_config, mock_bq_client):
    """Instantiate BigQueryDataSource with mocked dependencies.

    Patches get_config and create_bq_client so that no real GCP
    credentials or network access are needed.
    """
    with patch("connectors.bigquery.adapter.get_config", return_value=mock_config), \
         patch("connectors.bigquery.adapter.create_bq_client", return_value=mock_bq_client):
        from connectors.bigquery.adapter import BigQueryDataSource
        adapter = BigQueryDataSource()
    return adapter


# ---------------------------------------------------------------------------
# 1. full_refresh writes valid Parquet file from Arrow table
# ---------------------------------------------------------------------------

class TestFullRefresh:

    def test_writes_valid_parquet(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """full_refresh should write a valid, readable Parquet file."""
        table_config = _make_table_config(sync_strategy="full_refresh")
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        arrow_data = _sample_arrow_table([1, 2, 3], ["Alice", "Bob", "Charlie"])
        mock_bq_client.read_table.return_value = arrow_data

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True
        assert result["rows"] == 3
        assert parquet_path.exists()

        # Verify Parquet content matches source data
        read_back = pq.read_table(parquet_path)
        assert read_back.num_rows == 3
        assert read_back.column_names == ["id", "name"]

    def test_applies_date_columns(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """full_refresh should call convert_date_columns_to_date32 when date columns exist."""
        table_config = _make_table_config()
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        arrow_data = _sample_arrow_table([1], ["Alice"])
        mock_bq_client.read_table.return_value = arrow_data
        mock_bq_client.get_date_columns.return_value = ["created_at"]

        with patch("connectors.bigquery.adapter.convert_date_columns_to_date32", return_value=arrow_data) as mock_conv:
            adapter = _create_adapter(mock_config, mock_bq_client)
            adapter.sync_table(table_config, sync_state)
            mock_conv.assert_called_once_with(arrow_data, ["created_at"])

    def test_applies_pyarrow_schema(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """full_refresh should call apply_schema_to_table when schema is available."""
        table_config = _make_table_config()
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        arrow_data = _sample_arrow_table([1], ["Alice"])
        mock_bq_client.read_table.return_value = arrow_data
        schema = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        mock_bq_client.get_pyarrow_schema.return_value = schema

        with patch("connectors.bigquery.adapter.apply_schema_to_table", return_value=arrow_data) as mock_apply:
            adapter = _create_adapter(mock_config, mock_bq_client)
            adapter.sync_table(table_config, sync_state)
            mock_apply.assert_called_once_with(arrow_data, schema)


# ---------------------------------------------------------------------------
# 2. incremental_column_sync merges correctly (dedup on PK, new data wins)
# ---------------------------------------------------------------------------

class TestIncrementalColumnSync:

    def test_merge_dedup_new_data_wins(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """Incremental sync should overwrite existing rows when PK matches (new data wins)."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="updated_at",
            incremental_window_days=7,
        )
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        # Write existing data
        existing = _sample_arrow_table([1, 2], ["Alice", "Bob"])
        pq.write_table(existing, parquet_path)

        # Simulate a previous sync timestamp
        sync_state.update_sync(
            table_id=table_config.id,
            table_name=table_config.name,
            strategy="incremental",
            rows=2,
            file_size_bytes=100,
        )

        # New data: id=2 gets updated name, id=3 is new
        new_data = _sample_arrow_table([2, 3], ["Bob_Updated", "Charlie"])
        mock_bq_client.read_table_incremental.return_value = new_data

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True
        assert result["rows"] == 3  # Alice + Bob_Updated + Charlie

        read_back = pq.read_table(parquet_path)
        df = read_back.to_pandas()
        assert set(df["id"].tolist()) == {1, 2, 3}
        # id=2 should have the updated name
        bob_row = df[df["id"] == 2].iloc[0]
        assert bob_row["name"] == "Bob_Updated"


# ---------------------------------------------------------------------------
# 3. incremental_column_sync with no new data returns existing file info
# ---------------------------------------------------------------------------

class TestIncrementalNoNewData:

    def test_returns_existing_file_info(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """When there is no new data, sync returns stats from the existing Parquet file."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="updated_at",
            incremental_window_days=7,
        )
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        # Write existing data
        existing = _sample_arrow_table([1, 2, 3], ["A", "B", "C"])
        pq.write_table(existing, parquet_path)

        # Mark a previous sync
        sync_state.update_sync(
            table_id=table_config.id,
            table_name=table_config.name,
            strategy="incremental",
            rows=3,
            file_size_bytes=100,
        )

        # No new rows
        empty_table = pa.table({
            "id": pa.array([], type=pa.int64()),
            "name": pa.array([], type=pa.string()),
        })
        mock_bq_client.read_table_incremental.return_value = empty_table

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True
        assert result["rows"] == 3  # existing row count preserved


# ---------------------------------------------------------------------------
# 4. partitioned_sync creates partition files
# ---------------------------------------------------------------------------

class TestPartitionedSync:

    def test_creates_partition_files(self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state):
        """Partitioned sync should create separate Parquet files per partition key."""
        import pandas as pd

        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="created_at",
            partition_by="created_at",
            partition_granularity="month",
            incremental_window_days=7,
        )

        # For partitioned tables, parquet_path is a directory
        partition_dir = tmp_parquet_dir / "orders"
        partition_dir.mkdir(parents=True, exist_ok=True)
        mock_config.get_parquet_path.return_value = partition_dir

        # Configure partition paths
        def _partition_path(tc, key):
            return partition_dir / f"{key}.parquet"
        mock_config.get_partition_path.side_effect = _partition_path

        # Build arrow table with timestamps in two months
        ts_jan = [pd.Timestamp("2026-01-15 10:00:00", tz="UTC")]
        ts_feb = [pd.Timestamp("2026-02-20 14:00:00", tz="UTC")]
        arrow_data = pa.table({
            "id": [1, 2],
            "name": ["Jan_Order", "Feb_Order"],
            "created_at": pa.array(ts_jan + ts_feb, type=pa.timestamp("us", tz="UTC")),
        })
        mock_bq_client.read_table.return_value = arrow_data

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True

        # Should have created two partition files
        partition_files = list(partition_dir.glob("*.parquet"))
        assert len(partition_files) == 2

        partition_names = sorted(f.stem for f in partition_files)
        assert "2026_01" in partition_names
        assert "2026_02" in partition_names


# ---------------------------------------------------------------------------
# 5. discover_tables delegates to BigQueryClient.discover_all_tables()
# ---------------------------------------------------------------------------

class TestDiscoverTables:

    def test_delegates_to_client(self, mock_config, mock_bq_client):
        """discover_tables should forward the call to BigQueryClient.discover_all_tables."""
        expected = [{"id": "proj.ds.t1", "name": "t1", "columns": ["a", "b"]}]
        mock_bq_client.discover_all_tables.return_value = expected

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.discover_tables()

        mock_bq_client.discover_all_tables.assert_called_once()
        assert result == expected


# ---------------------------------------------------------------------------
# 6. get_source_name returns "Google BigQuery"
# ---------------------------------------------------------------------------

class TestGetSourceName:

    def test_returns_google_bigquery(self, mock_config, mock_bq_client):
        adapter = _create_adapter(mock_config, mock_bq_client)
        assert adapter.get_source_name() == "Google BigQuery"


# ---------------------------------------------------------------------------
# 7. get_column_metadata returns correct format
# ---------------------------------------------------------------------------

class TestGetColumnMetadata:

    def test_returns_correct_format(self, mock_config, mock_bq_client):
        """get_column_metadata should transform BQ raw metadata into {columns: ...} format."""
        mock_bq_client.get_table_metadata.return_value = {
            "column_types": {"id": "INT64", "name": "STRING", "email": "STRING"},
            "column_descriptions": {"id": "Primary key", "email": "User email address"},
        }

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.get_column_metadata("project.dataset.users")

        assert "columns" in result
        assert result["columns"]["id"] == {"source_type": "INT64", "description": "Primary key"}
        assert result["columns"]["name"] == {"source_type": "STRING"}
        assert result["columns"]["email"] == {
            "source_type": "STRING",
            "description": "User email address",
        }

    def test_returns_none_when_no_column_types(self, mock_config, mock_bq_client):
        """get_column_metadata should return None if the metadata has no column types."""
        mock_bq_client.get_table_metadata.return_value = {
            "column_types": {},
            "column_descriptions": {},
        }

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.get_column_metadata("project.dataset.users")

        assert result is None


# ---------------------------------------------------------------------------
# 8. Error handling (query failure -> {success: False, error: ...})
# ---------------------------------------------------------------------------

class TestErrorHandling:

    def test_query_failure_returns_error_dict(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """When BigQuery query raises, sync_table returns {success: False, error: ...}."""
        table_config = _make_table_config()
        mock_config.get_parquet_path.return_value = tmp_parquet_dir / "orders.parquet"
        mock_bq_client.read_table.side_effect = RuntimeError("BigQuery API timeout")

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is False
        assert "BigQuery API timeout" in result["error"]
        assert result["strategy"] == "full_refresh"

    def test_unknown_strategy_returns_error(self, mock_config, mock_bq_client, sync_state):
        """Unknown sync_strategy in internal dispatch should produce an error result."""
        # We cannot create a TableConfig with an invalid strategy via constructor
        # (it validates). Instead, we mutate it after creation.
        table_config = _make_table_config()
        table_config.sync_strategy = "magic_sync"

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is False
        assert "Unknown sync strategy" in result["error"]


# ---------------------------------------------------------------------------
# 9. incremental_column config is used in WHERE clause
# ---------------------------------------------------------------------------

class TestIncrementalColumnUsedInWhere:

    def test_incremental_column_passed_to_client(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """The configured incremental_column should be forwarded to read_table_incremental."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="modified_at",
            incremental_window_days=14,
        )
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        # Write existing data so we enter the incremental path
        existing = _sample_arrow_table([1], ["Alice"])
        pq.write_table(existing, parquet_path)

        sync_state.update_sync(
            table_id=table_config.id,
            table_name=table_config.name,
            strategy="incremental",
            rows=1,
            file_size_bytes=100,
        )

        # Return empty to keep the test simple
        empty = pa.table({
            "id": pa.array([], type=pa.int64()),
            "name": pa.array([], type=pa.string()),
        })
        mock_bq_client.read_table_incremental.return_value = empty

        adapter = _create_adapter(mock_config, mock_bq_client)
        adapter.sync_table(table_config, sync_state)

        call_kwargs = mock_bq_client.read_table_incremental.call_args
        assert call_kwargs.kwargs["incremental_column"] == "modified_at"
        assert call_kwargs.kwargs["table_id"] == "project.dataset.orders"
        # since_value should be an ISO string
        assert "since_value" in call_kwargs.kwargs


# ---------------------------------------------------------------------------
# 10. First sync without existing file downloads all data
# ---------------------------------------------------------------------------

class TestFirstSyncDownloadsAll:

    def test_first_sync_reads_full_table(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """On first incremental sync (no existing file), adapter should read all data."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="updated_at",
            incremental_window_days=7,
        )
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        # No previous sync, no existing file
        arrow_data = _sample_arrow_table([1, 2, 3], ["A", "B", "C"])
        mock_bq_client.read_table.return_value = arrow_data

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True
        assert result["rows"] == 3
        # Should call read_table (full), not read_table_incremental
        mock_bq_client.read_table.assert_called_once_with(
            table_config.id, columns=None, row_filter=None,
        )
        mock_bq_client.read_table_incremental.assert_not_called()

    def test_first_sync_with_max_history_days(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """First sync with max_history_days should use read_table_incremental."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="updated_at",
            incremental_window_days=7,
            max_history_days=90,
        )
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path

        arrow_data = _sample_arrow_table([1, 2], ["A", "B"])
        mock_bq_client.read_table_incremental.return_value = arrow_data

        adapter = _create_adapter(mock_config, mock_bq_client)
        result = adapter.sync_table(table_config, sync_state)

        assert result["success"] is True
        # Should use read_table_incremental (not read_table) because max_history_days is set
        mock_bq_client.read_table_incremental.assert_called_once()
        call_kwargs = mock_bq_client.read_table_incremental.call_args.kwargs
        assert call_kwargs["incremental_column"] == "updated_at"
        mock_bq_client.read_table.assert_not_called()


# ---------------------------------------------------------------------------
# 11. sync_table dispatches to correct strategy based on sync_strategy
# ---------------------------------------------------------------------------

class TestSyncTableDispatch:

    def test_dispatches_full_refresh(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """sync_strategy='full_refresh' should call _full_refresh."""
        table_config = _make_table_config(sync_strategy="full_refresh")
        mock_config.get_parquet_path.return_value = tmp_parquet_dir / "orders.parquet"
        mock_bq_client.read_table.return_value = _sample_arrow_table([1], ["A"])

        adapter = _create_adapter(mock_config, mock_bq_client)

        with patch.object(adapter, "_full_refresh", wraps=adapter._full_refresh) as spy:
            adapter.sync_table(table_config, sync_state)
            spy.assert_called_once_with(table_config)

    def test_dispatches_incremental(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """sync_strategy='incremental' should call _incremental_sync."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="updated_at",
            incremental_window_days=7,
        )
        mock_config.get_parquet_path.return_value = tmp_parquet_dir / "orders.parquet"
        mock_bq_client.read_table.return_value = _sample_arrow_table([1], ["A"])

        adapter = _create_adapter(mock_config, mock_bq_client)

        with patch.object(adapter, "_incremental_sync", wraps=adapter._incremental_sync) as spy:
            adapter.sync_table(table_config, sync_state)
            spy.assert_called_once_with(table_config, sync_state)

    def test_dispatches_partitioned(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """sync_strategy='incremental' with partition_by should call _partitioned_sync."""
        import pandas as pd

        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column="created_at",
            partition_by="created_at",
            partition_granularity="month",
            incremental_window_days=7,
        )
        partition_dir = tmp_parquet_dir / "orders"
        partition_dir.mkdir(parents=True, exist_ok=True)
        mock_config.get_parquet_path.return_value = partition_dir

        def _partition_path(tc, key):
            return partition_dir / f"{key}.parquet"
        mock_config.get_partition_path.side_effect = _partition_path

        ts = [pd.Timestamp("2026-01-15 10:00:00", tz="UTC")]
        arrow_data = pa.table({
            "id": [1],
            "name": ["A"],
            "created_at": pa.array(ts, type=pa.timestamp("us", tz="UTC")),
        })
        mock_bq_client.read_table.return_value = arrow_data

        adapter = _create_adapter(mock_config, mock_bq_client)

        with patch.object(adapter, "_partitioned_sync", wraps=adapter._partitioned_sync) as spy:
            adapter.sync_table(table_config, sync_state)
            spy.assert_called_once()

    def test_incremental_without_column_falls_back_to_full_refresh(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """incremental strategy without incremental_column or partition_by falls back to full_refresh."""
        table_config = _make_table_config(
            sync_strategy="incremental",
            incremental_column=None,
            partition_by=None,
            incremental_window_days=7,
        )
        mock_config.get_parquet_path.return_value = tmp_parquet_dir / "orders.parquet"
        mock_bq_client.read_table.return_value = _sample_arrow_table([1], ["A"])

        adapter = _create_adapter(mock_config, mock_bq_client)

        with patch.object(adapter, "_full_refresh", wraps=adapter._full_refresh) as spy:
            result = adapter.sync_table(table_config, sync_state)
            spy.assert_called_once()
            assert result["success"] is True


# ---------------------------------------------------------------------------
# 12. _merge_arrow_tables deduplicates correctly
# ---------------------------------------------------------------------------

class TestMergeArrowTables:

    def test_dedup_on_single_pk(self, mock_config, mock_bq_client):
        """Merge should deduplicate on single primary key column, new data wins."""
        adapter = _create_adapter(mock_config, mock_bq_client)

        existing = pa.table({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        new_data = pa.table({"id": [2, 4], "val": ["B_new", "d"]})

        merged = adapter._merge_arrow_tables(existing, new_data, primary_key=["id"])
        df = merged.to_pandas().sort_values("id").reset_index(drop=True)

        assert list(df["id"]) == [1, 2, 3, 4]
        assert list(df["val"]) == ["a", "B_new", "c", "d"]

    def test_dedup_on_composite_pk(self, mock_config, mock_bq_client):
        """Merge should deduplicate on composite primary key."""
        adapter = _create_adapter(mock_config, mock_bq_client)

        existing = pa.table({
            "pk1": [1, 1, 2],
            "pk2": ["a", "b", "a"],
            "val": ["old_1a", "old_1b", "old_2a"],
        })
        new_data = pa.table({
            "pk1": [1, 2],
            "pk2": ["a", "a"],
            "val": ["new_1a", "new_2a"],
        })

        merged = adapter._merge_arrow_tables(existing, new_data, primary_key=["pk1", "pk2"])
        df = merged.to_pandas().sort_values(["pk1", "pk2"]).reset_index(drop=True)

        assert len(df) == 3
        # (1, a) should be updated
        row_1a = df[(df["pk1"] == 1) & (df["pk2"] == "a")].iloc[0]
        assert row_1a["val"] == "new_1a"
        # (1, b) should be preserved
        row_1b = df[(df["pk1"] == 1) & (df["pk2"] == "b")].iloc[0]
        assert row_1b["val"] == "old_1b"
        # (2, a) should be updated
        row_2a = df[(df["pk1"] == 2) & (df["pk2"] == "a")].iloc[0]
        assert row_2a["val"] == "new_2a"

    def test_merge_with_empty_new_data(self, mock_config, mock_bq_client):
        """Merging with empty new data should return existing data unchanged."""
        adapter = _create_adapter(mock_config, mock_bq_client)

        existing = pa.table({"id": [1, 2], "val": ["a", "b"]})
        empty = pa.table({
            "id": pa.array([], type=pa.int64()),
            "val": pa.array([], type=pa.string()),
        })

        merged = adapter._merge_arrow_tables(existing, empty, primary_key=["id"])
        assert merged.num_rows == 2

    def test_merge_with_empty_existing(self, mock_config, mock_bq_client):
        """Merging with empty existing data should return new data."""
        adapter = _create_adapter(mock_config, mock_bq_client)

        empty = pa.table({
            "id": pa.array([], type=pa.int64()),
            "val": pa.array([], type=pa.string()),
        })
        new_data = pa.table({"id": [1, 2], "val": ["a", "b"]})

        merged = adapter._merge_arrow_tables(empty, new_data, primary_key=["id"])
        assert merged.num_rows == 2


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

class TestMetadataCacheClearing:

    def test_clears_metadata_cache_before_sync(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """sync_table should clear the BQ metadata cache entry for the table being synced."""
        table_config = _make_table_config()
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path
        mock_bq_client.read_table.return_value = _sample_arrow_table([1], ["A"])

        # Pre-populate cache
        mock_bq_client.metadata_cache[table_config.id] = {"some": "cached_data"}

        adapter = _create_adapter(mock_config, mock_bq_client)
        adapter.sync_table(table_config, sync_state)

        assert table_config.id not in mock_bq_client.metadata_cache


class TestSyncStateUpdate:

    def test_sync_state_updated_after_success(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """After successful sync, the sync state should be updated with correct values."""
        table_config = _make_table_config()
        parquet_path = tmp_parquet_dir / "orders.parquet"
        mock_config.get_parquet_path.return_value = parquet_path
        mock_bq_client.read_table.return_value = _sample_arrow_table([1, 2], ["A", "B"])

        adapter = _create_adapter(mock_config, mock_bq_client)
        adapter.sync_table(table_config, sync_state)

        state = sync_state.get_table_state(table_config.id)
        assert state["rows"] == 2
        assert state["strategy"] == "full_refresh"
        assert state["table_name"] == "orders"
        assert "last_sync" in state

    def test_sync_state_not_updated_on_failure(
        self, mock_config, mock_bq_client, tmp_parquet_dir, sync_state
    ):
        """On sync failure, the sync state should NOT be updated."""
        table_config = _make_table_config()
        mock_config.get_parquet_path.return_value = tmp_parquet_dir / "orders.parquet"
        mock_bq_client.read_table.side_effect = RuntimeError("boom")

        adapter = _create_adapter(mock_config, mock_bq_client)
        adapter.sync_table(table_config, sync_state)

        state = sync_state.get_table_state(table_config.id)
        assert state == {}


class TestCreateDataSourceFactory:

    def test_factory_returns_adapter_instance(self, mock_config, mock_bq_client):
        """create_data_source() factory should return a BigQueryDataSource instance."""
        with patch("connectors.bigquery.adapter.get_config", return_value=mock_config), \
             patch("connectors.bigquery.adapter.create_bq_client", return_value=mock_bq_client):
            from connectors.bigquery.adapter import create_data_source, BigQueryDataSource
            instance = create_data_source()
            assert isinstance(instance, BigQueryDataSource)
