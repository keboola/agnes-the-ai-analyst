"""Pure-function mapping/validation logic for the Keboola semantic-layer
importer (connectors/keboola/semantic_layer.py). No live API calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    dataset_lookup_by_table_id,
    require_master_token,
    resolve_table_name,
    table_lookup_from_registry,
)


class TestRequireMasterToken:
    def test_passes_silently_for_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": True}

        require_master_token(storage_client)  # must not raise

    def test_raises_for_non_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": False}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)

    def test_raises_for_missing_field(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)


class TestTableLookupFromRegistry:
    def test_builds_bucket_source_table_to_name_map(self):
        rows = [
            {
                "bucket": "in.c-example_source",
                "source_table": "orders",
                "name": "crm_orders",
            },
            {
                "bucket": "in.c-example_source",
                "source_table": "contacts",
                "name": "crm_contacts",
            },
        ]
        lookup = table_lookup_from_registry(rows)
        assert lookup == {
            ("in.c-example_source", "orders"): "crm_orders",
            ("in.c-example_source", "contacts"): "crm_contacts",
        }

    def test_skips_rows_missing_bucket_or_source_table(self):
        rows = [
            {"bucket": None, "source_table": "orders", "name": "x"},
            {"bucket": "in.c-example_source", "source_table": None, "name": "y"},
            {"bucket": "in.c-example_source", "source_table": "contacts", "name": None},
        ]
        assert table_lookup_from_registry(rows) == {}


class TestResolveTableName:
    def test_splits_on_last_dot_bucket_may_contain_dots(self):
        # Bucket ids look like `in.c-example_source` (contain dots themselves) —
        # must split the tableId on the LAST dot, not the first.
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.orders", lookup) == "crm_orders"

    def test_returns_none_for_unregistered_table(self):
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.unknown_table", lookup) is None

    def test_returns_none_for_malformed_table_id(self):
        assert resolve_table_name("no_dot_here", {}) is None


class TestDatasetLookupByTableId:
    def test_builds_table_id_to_attributes_map(self):
        items = [
            {
                "type": "semantic-dataset",
                "id": "d1",
                "attributes": {
                    "tableId": "in.c-example_source.orders",
                    "grain": "One row per order",
                },
            },
        ]
        lookup = dataset_lookup_by_table_id(items)
        assert lookup == {
            "in.c-example_source.orders": {
                "tableId": "in.c-example_source.orders",
                "grain": "One row per order",
            }
        }

    def test_skips_items_missing_table_id(self):
        items = [{"type": "semantic-dataset", "id": "d1", "attributes": {"name": "no tableId"}}]
        assert dataset_lookup_by_table_id(items) == {}
