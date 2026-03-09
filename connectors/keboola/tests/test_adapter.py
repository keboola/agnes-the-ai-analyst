"""Tests for Keboola adapter and DataSource ABC / factory in src.data_sync.

Covers:
- DataSource ABC default method behaviour
- create_data_source factory: keboola import error, unknown source, dynamic lookup
- KeboolaDataSource env var validation
"""

from unittest.mock import patch, MagicMock

import pytest

from src.data_sync import DataSource, SyncState, create_data_source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MinimalSource(DataSource):
    """Concrete DataSource that only implements the required abstract method."""

    def sync_table(self, table_config, sync_state):
        return {"success": True, "rows": 0}


# ---------------------------------------------------------------------------
# 1. DataSource ABC default methods
# ---------------------------------------------------------------------------

class TestDataSourceABCDefaultMethods:
    """Verify that the optional methods on the DataSource ABC return sensible defaults."""

    def test_get_column_metadata_returns_none(self):
        source = _MinimalSource()
        assert source.get_column_metadata("any.table.id") is None

    def test_get_source_name_returns_unknown(self):
        source = _MinimalSource()
        assert source.get_source_name() == "Unknown"


# ---------------------------------------------------------------------------
# 2. Factory: keboola without kbcstorage
# ---------------------------------------------------------------------------

class TestFactoryKeboolaWithoutKbcstorage:
    """create_data_source('keboola') must raise ImportError when kbcstorage is missing."""

    def test_raises_import_error(self):
        # Patch the import inside create_data_source so that importing
        # connectors.keboola.adapter triggers a ModuleNotFoundError
        # mentioning kbcstorage (simulates the package not being installed).
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _fake_import(name, *args, **kwargs):
            if name == "connectors.keboola.adapter":
                raise ModuleNotFoundError("No module named 'kbcstorage'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            with pytest.raises(ImportError, match="kbcstorage"):
                create_data_source("keboola")


# ---------------------------------------------------------------------------
# 3. Factory: unknown source type
# ---------------------------------------------------------------------------

class TestFactoryUnknownSource:
    """create_data_source with a non-existent source type must raise ValueError."""

    def test_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown data source.*nonexistent"):
            create_data_source("nonexistent")

    def test_error_message_contains_guidance(self):
        with pytest.raises(ValueError, match="connectors/nonexistent/adapter.py"):
            create_data_source("nonexistent")


# ---------------------------------------------------------------------------
# 4. Factory: dynamic connector lookup
# ---------------------------------------------------------------------------

class TestFactoryDynamicConnectorLookup:
    """create_data_source attempts dynamic import for unknown connector types."""

    def test_jira_lookup_falls_through_to_value_error(self):
        """'jira' has no adapter.py exporting a DataSource, so the factory
        should try importing connectors.jira.adapter, fail, and finally
        raise ValueError."""
        with pytest.raises(ValueError, match="Unknown data source.*jira"):
            create_data_source("jira")

    def test_dynamic_import_is_attempted(self):
        """Verify that importlib.import_module is called with the expected
        module path when the source type is not hard-coded."""
        with patch("src.data_sync.importlib.import_module", side_effect=ModuleNotFoundError) as mock_imp:
            with pytest.raises(ValueError):
                create_data_source("custom_source")
            mock_imp.assert_called_once_with("connectors.custom_source.adapter")

    def test_dynamic_import_with_factory_function(self):
        """If the dynamically loaded module exposes create_data_source(),
        the factory should call it and return its result."""
        fake_source = _MinimalSource()
        fake_module = MagicMock()
        fake_module.create_data_source = MagicMock(return_value=fake_source)

        with patch("src.data_sync.importlib.import_module", return_value=fake_module):
            result = create_data_source("my_connector")

        assert result is fake_source
        fake_module.create_data_source.assert_called_once()

    def test_dynamic_import_with_datasource_subclass(self):
        """If the dynamically loaded module has no factory but exposes a
        DataSource subclass, the factory should instantiate it."""
        import types

        fake_module = types.ModuleType("connectors.my_connector.adapter")
        fake_module.MyDataSource = _MinimalSource

        with patch("src.data_sync.importlib.import_module", return_value=fake_module):
            result = create_data_source("my_connector")

        assert isinstance(result, _MinimalSource)


# ---------------------------------------------------------------------------
# 5. KeboolaDataSource validates env vars
# ---------------------------------------------------------------------------

class TestKeboolaAdapterValidatesEnvVars:
    """KeboolaDataSource.__init__ must raise ValueError when required
    Keboola env vars are missing."""

    def _make_mock_config(self, token="", stack_url="", project_id=""):
        """Build a mock config with the given Keboola credential values."""
        cfg = MagicMock()
        cfg.keboola_token = token
        cfg.keboola_stack_url = stack_url
        cfg.keboola_project_id = project_id
        return cfg

    def test_all_missing(self):
        mock_cfg = self._make_mock_config()
        with patch("connectors.keboola.adapter.get_config", return_value=mock_cfg):
            with pytest.raises(ValueError, match="KEBOOLA_STORAGE_TOKEN"):
                from connectors.keboola.adapter import KeboolaDataSource
                KeboolaDataSource()

    def test_token_missing(self):
        mock_cfg = self._make_mock_config(
            stack_url="https://connection.keboola.com",
            project_id="12345",
        )
        with patch("connectors.keboola.adapter.get_config", return_value=mock_cfg):
            with pytest.raises(ValueError, match="KEBOOLA_STORAGE_TOKEN"):
                from connectors.keboola.adapter import KeboolaDataSource
                KeboolaDataSource()

    def test_stack_url_missing(self):
        mock_cfg = self._make_mock_config(
            token="my-token",
            project_id="12345",
        )
        with patch("connectors.keboola.adapter.get_config", return_value=mock_cfg):
            with pytest.raises(ValueError, match="KEBOOLA_STACK_URL"):
                from connectors.keboola.adapter import KeboolaDataSource
                KeboolaDataSource()

    def test_project_id_missing(self):
        mock_cfg = self._make_mock_config(
            token="my-token",
            stack_url="https://connection.keboola.com",
        )
        with patch("connectors.keboola.adapter.get_config", return_value=mock_cfg):
            with pytest.raises(ValueError, match="KEBOOLA_PROJECT_ID"):
                from connectors.keboola.adapter import KeboolaDataSource
                KeboolaDataSource()

    def test_error_lists_all_missing_vars(self):
        """When multiple env vars are missing, all should appear in the error message."""
        mock_cfg = self._make_mock_config()
        with patch("connectors.keboola.adapter.get_config", return_value=mock_cfg):
            with pytest.raises(ValueError) as exc_info:
                from connectors.keboola.adapter import KeboolaDataSource
                KeboolaDataSource()
            msg = str(exc_info.value)
            assert "KEBOOLA_STORAGE_TOKEN" in msg
            assert "KEBOOLA_STACK_URL" in msg
            assert "KEBOOLA_PROJECT_ID" in msg
