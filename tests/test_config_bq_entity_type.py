"""Tests for TableConfig.bq_entity_type field validation."""

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
class TestBqEntityTypeDefault:
    def test_default_is_view(self):
        table = _make_table()
        assert table.bq_entity_type == "view"


class TestBqEntityTypeValidValues:
    @pytest.mark.parametrize("entity_type", ["view", "table"])
    def test_valid_bq_entity_type(self, entity_type):
        table = _make_table(bq_entity_type=entity_type)
        assert table.bq_entity_type == entity_type


class TestBqEntityTypeInvalid:
    @pytest.mark.parametrize("bad_type", ["VIEW", "physical", "", "tables", "materialized"])
    def test_invalid_bq_entity_type_raises(self, bad_type):
        with pytest.raises(ValueError, match="Invalid bq_entity_type"):
            _make_table(bq_entity_type=bad_type)


class TestBqEntityTypeFromKwarg:
    def test_kwarg_sets_bq_entity_type(self):
        """Simulate what _parse_data_description does: pass bq_entity_type as kwarg."""
        table = TableConfig(
            id="proj.dataset.orders",
            name="orders",
            description="Order data",
            primary_key="order_id",
            sync_strategy="full_refresh",
            bq_entity_type="table",
        )
        assert table.bq_entity_type == "table"

    def test_kwarg_default_when_omitted(self):
        """When YAML has no bq_entity_type, _parse_data_description passes 'view'."""
        table = TableConfig(
            id="proj.dataset.orders",
            name="orders",
            description="Order data",
            primary_key="order_id",
            sync_strategy="full_refresh",
        )
        assert table.bq_entity_type == "view"
