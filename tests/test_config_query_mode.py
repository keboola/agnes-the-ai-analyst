"""Tests for TableConfig.query_mode field validation."""

import pytest

from src.config import TableConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_table(**overrides) -> TableConfig:
    """Create a TableConfig with sensible defaults, applying overrides."""
    defaults = dict(
        id="test.dataset.table",
        name="test_table",
        description="Test",
        primary_key="id",
        sync_strategy="full_refresh",
    )
    defaults.update(overrides)
    return TableConfig(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestQueryModeDefault:
    def test_default_is_local(self):
        table = _make_table()
        assert table.query_mode == "local"


class TestQueryModeValidValues:
    @pytest.mark.parametrize("mode", ["local", "remote", "hybrid"])
    def test_valid_query_mode(self, mode):
        table = _make_table(query_mode=mode)
        assert table.query_mode == mode


class TestQueryModeInvalid:
    @pytest.mark.parametrize("bad_mode", ["invalid", "Local", "REMOTE", "", "sql"])
    def test_invalid_query_mode_raises(self, bad_mode):
        with pytest.raises(ValueError, match="Invalid query_mode"):
            _make_table(query_mode=bad_mode)


class TestQueryModeFromKwarg:
    def test_kwarg_sets_query_mode(self):
        """Simulate what _parse_data_description does: pass query_mode as kwarg."""
        table = TableConfig(
            id="proj.dataset.orders",
            name="orders",
            description="Order data",
            primary_key="order_id",
            sync_strategy="full_refresh",
            query_mode="remote",
        )
        assert table.query_mode == "remote"

    def test_kwarg_default_when_omitted(self):
        """When YAML has no query_mode, _parse_data_description passes 'local'."""
        table = TableConfig(
            id="proj.dataset.orders",
            name="orders",
            description="Order data",
            primary_key="order_id",
            sync_strategy="full_refresh",
        )
        assert table.query_mode == "local"
