"""Tests for remote table skipping in DataSyncManager.sync_all().

Tables with query_mode == "remote" should be skipped during sync (no local
Parquet file is needed -- queries go directly to BigQuery). Tables with
query_mode "local" or "hybrid" must still be synced normally.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.config import TableConfig
from src.data_sync import DataSyncManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_table_config(
    table_id: str,
    name: str,
    query_mode: str = "local",
) -> TableConfig:
    """Create a minimal TableConfig for testing."""
    return TableConfig(
        id=table_id,
        name=name,
        description=f"Test table {name}",
        primary_key="id",
        sync_strategy="full_refresh",
        query_mode=query_mode,
    )


def _successful_sync_result() -> dict:
    """Return a fake successful sync result dict."""
    return {
        "success": True,
        "rows": 100,
        "file_size_mb": 0.5,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def table_local():
    return _make_table_config("t.local", "local_table", query_mode="local")


@pytest.fixture
def table_remote():
    return _make_table_config("t.remote", "remote_table", query_mode="remote")


@pytest.fixture
def table_hybrid():
    return _make_table_config("t.hybrid", "hybrid_table", query_mode="hybrid")


@pytest.fixture
def all_tables(table_local, table_remote, table_hybrid):
    return [table_local, table_remote, table_hybrid]


@pytest.fixture
def mock_config(all_tables):
    """Return a mock Config whose .tables list contains all three query modes."""
    cfg = MagicMock()
    cfg.tables = all_tables
    cfg.get_metadata_path.return_value = MagicMock()  # Path-like

    def _get_table_config(tid):
        return next((t for t in all_tables if t.id == tid), None)

    cfg.get_table_config.side_effect = _get_table_config
    return cfg


@pytest.fixture
def mock_data_source():
    """Return a mock DataSource that always succeeds."""
    ds = MagicMock()
    ds.sync_table.return_value = _successful_sync_result()
    return ds


@pytest.fixture
def sync_manager(mock_config, mock_data_source):
    """Create a DataSyncManager with mocked dependencies."""
    with (
        patch("src.data_sync.get_config", return_value=mock_config),
        patch("src.data_sync.create_data_source", return_value=mock_data_source),
        patch("src.data_sync.SyncState"),
    ):
        manager = DataSyncManager()
        # Patch out schema generation and auto-profiling (not under test)
        manager._generate_schema_yaml = MagicMock()
        yield manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSyncAllRemoteSkipping:
    """Verify that sync_all filters out remote tables."""

    def test_remote_table_not_synced(self, sync_manager, mock_data_source, table_remote):
        """Remote table must NOT be passed to data_source.sync_table."""
        sync_manager.sync_all()

        synced_ids = [
            call.args[0].id for call in mock_data_source.sync_table.call_args_list
        ]
        assert table_remote.id not in synced_ids

    def test_local_table_is_synced(self, sync_manager, mock_data_source, table_local):
        """Local table must be synced normally."""
        sync_manager.sync_all()

        synced_ids = [
            call.args[0].id for call in mock_data_source.sync_table.call_args_list
        ]
        assert table_local.id in synced_ids

    def test_hybrid_table_is_synced(self, sync_manager, mock_data_source, table_hybrid):
        """Hybrid table must be synced (needs local parquet for profiling)."""
        sync_manager.sync_all()

        synced_ids = [
            call.args[0].id for call in mock_data_source.sync_table.call_args_list
        ]
        assert table_hybrid.id in synced_ids

    def test_sync_call_count(self, sync_manager, mock_data_source):
        """Only local + hybrid tables should result in sync_table calls."""
        sync_manager.sync_all()

        # 3 tables total, 1 remote -> 2 sync calls
        assert mock_data_source.sync_table.call_count == 2

    def test_results_exclude_remote(self, sync_manager, table_remote):
        """The results dict must not contain an entry for the remote table."""
        results = sync_manager.sync_all()

        assert table_remote.id not in results

    def test_results_include_local_and_hybrid(
        self, sync_manager, table_local, table_hybrid
    ):
        """Results dict must contain entries for local and hybrid tables."""
        results = sync_manager.sync_all()

        assert table_local.id in results
        assert table_hybrid.id in results


class TestSyncAllAllRemote:
    """Edge case: every table is remote."""

    def test_no_sync_calls_when_all_remote(self, mock_config, mock_data_source):
        remote_only = [
            _make_table_config("t.r1", "remote1", query_mode="remote"),
            _make_table_config("t.r2", "remote2", query_mode="remote"),
        ]
        mock_config.tables = remote_only

        with (
            patch("src.data_sync.get_config", return_value=mock_config),
            patch("src.data_sync.create_data_source", return_value=mock_data_source),
            patch("src.data_sync.SyncState"),
        ):
            manager = DataSyncManager()
            manager._generate_schema_yaml = MagicMock()
            results = manager.sync_all()

        assert mock_data_source.sync_table.call_count == 0
        assert results == {}


class TestSyncAllNoRemote:
    """Edge case: no remote tables at all -- everything syncs."""

    def test_all_tables_synced(self, mock_config, mock_data_source):
        local_only = [
            _make_table_config("t.l1", "local1", query_mode="local"),
            _make_table_config("t.l2", "local2", query_mode="local"),
        ]
        mock_config.tables = local_only

        with (
            patch("src.data_sync.get_config", return_value=mock_config),
            patch("src.data_sync.create_data_source", return_value=mock_data_source),
            patch("src.data_sync.SyncState"),
        ):
            manager = DataSyncManager()
            manager._generate_schema_yaml = MagicMock()
            results = manager.sync_all()

        assert mock_data_source.sync_table.call_count == 2
        assert "t.l1" in results
        assert "t.l2" in results


class TestSyncAllWithTableFilter:
    """When sync_all receives an explicit table list, remote filtering still applies."""

    def test_explicit_remote_table_still_skipped(
        self, sync_manager, mock_data_source, table_remote
    ):
        """Even if explicitly listed, a remote table should be skipped."""
        sync_manager.sync_all(tables=[table_remote.id])

        assert mock_data_source.sync_table.call_count == 0

    def test_explicit_local_table_synced(
        self, sync_manager, mock_data_source, table_local
    ):
        """An explicitly listed local table should be synced."""
        sync_manager.sync_all(tables=[table_local.id])

        assert mock_data_source.sync_table.call_count == 1
        synced_id = mock_data_source.sync_table.call_args_list[0].args[0].id
        assert synced_id == table_local.id
