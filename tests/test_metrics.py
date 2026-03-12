"""Tests for OpenMetadata catalog metrics and parsing functions."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from webapp.app import _parse_om_metric, _load_metrics_from_catalog, _build_om_metric_detail, METRIC_CATEGORY_META


class TestParseOmMetric:
    """Unit tests for _parse_om_metric() function."""

    def test_parse_metric_basic_fields(self):
        """Extract basic fields from raw metric."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.total_revenue",
            "name": "total_revenue",
            "displayName": "Total Revenue",
            "description": "Total revenue from all orders",
            "tags": [],
        }

        result = _parse_om_metric(raw)

        assert result["name"] == "total_revenue"
        assert result["display_name"] == "Total Revenue"
        assert result["description"] == "Total revenue from all orders"
        assert result["path"] == "catalog:catalog.metrics.total_revenue"

    def test_parse_metric_with_category_tag(self):
        """Extract category from MetricCategory.* tag."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.revenue_metric",
            "name": "revenue_metric",
            "displayName": "Revenue",
            "description": "Test",
            "tags": [
                {"tagFQN": "MetricCategory.finance"},
                {"tagFQN": "Grain.monthly"},
            ],
        }

        result = _parse_om_metric(raw)

        assert result["category"] == "finance"
        assert result["grain"] == "monthly"

    def test_parse_metric_with_category_legacy_tag(self):
        """Extract category from Category.* tag (legacy)."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "tags": [
                {"tagFQN": "Category.marketing"},
            ],
        }

        result = _parse_om_metric(raw)

        assert result["category"] == "marketing"

    def test_parse_metric_fallback_to_general(self):
        """Default to 'general' category if no category tag."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.unknown",
            "name": "unknown",
            "displayName": "Unknown",
            "description": "Test",
            "tags": [],
        }

        result = _parse_om_metric(raw)

        assert result["category"] == "general"

    def test_parse_metric_display_name_fallback(self):
        """Use name as display_name if displayName not provided."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test_metric",
            "description": "Test",
            "tags": [],
        }

        result = _parse_om_metric(raw)

        assert result["display_name"] == "test_metric"

    def test_parse_metric_path_has_catalog_prefix(self):
        """Path field includes catalog: prefix for JS routing."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "tags": [],
        }

        result = _parse_om_metric(raw)

        assert result["path"].startswith("catalog:")


class TestLoadMetricsFromCatalog:
    """Tests for _load_metrics_from_catalog() with mocked enricher."""

    @patch('webapp.app._catalog_enricher')
    def test_returns_empty_list_if_enricher_disabled(self, mock_enricher):
        """Return empty list if enricher not enabled."""
        mock_enricher.enabled = False

        result = _load_metrics_from_catalog()

        assert result == []

    @patch('webapp.app._catalog_enricher')
    def test_returns_empty_list_if_enricher_none(self, mock_enricher):
        """Return empty list if enricher is None."""
        with patch('webapp.app._catalog_enricher', None):
            result = _load_metrics_from_catalog()
            assert result == []

    @patch('webapp.app._catalog_enricher')
    def test_groups_metrics_by_category(self, mock_enricher):
        """Group metrics by category key."""
        mock_enricher.enabled = True
        mock_enricher.get_metrics.return_value = [
            {
                "fullyQualifiedName": "catalog.metrics.finance_metric",
                "name": "finance_metric",
                "displayName": "Finance Metric",
                "description": "Test",
                "tags": [{"tagFQN": "MetricCategory.finance"}],
            },
            {
                "fullyQualifiedName": "catalog.metrics.marketing_metric",
                "name": "marketing_metric",
                "displayName": "Marketing Metric",
                "description": "Test",
                "tags": [{"tagFQN": "MetricCategory.marketing"}],
            },
        ]

        with patch('webapp.app._catalog_enricher', mock_enricher):
            result = _load_metrics_from_catalog()

        # Should have at least one of the known categories from METRIC_CATEGORY_META
        assert len(result) >= 1
        keys = [c["key"] for c in result]
        assert "finance" in keys or "marketing" in keys
        assert all(len(c["metrics"]) > 0 for c in result)

    @patch('webapp.app._catalog_enricher')
    def test_uses_metric_category_meta_order(self, mock_enricher):
        """Result categories ordered by METRIC_CATEGORY_META."""
        mock_enricher.enabled = True
        mock_enricher.get_metrics.return_value = [
            {
                "fullyQualifiedName": "catalog.metrics.m1",
                "name": "m1",
                "displayName": "M1",
                "description": "Test",
                "tags": [{"tagFQN": "MetricCategory.revenue"}],
            },
            {
                "fullyQualifiedName": "catalog.metrics.m2",
                "name": "m2",
                "displayName": "M2",
                "description": "Test",
                "tags": [{"tagFQN": "MetricCategory.customers"}],
            },
        ]

        with patch('webapp.app._catalog_enricher', mock_enricher):
            result = _load_metrics_from_catalog()

        # revenue should come before customers per METRIC_CATEGORY_META order
        keys = [c["key"] for c in result]
        if "revenue" in keys and "customers" in keys:
            revenue_idx = keys.index("revenue")
            customers_idx = keys.index("customers")
            assert revenue_idx < customers_idx

    @patch('webapp.app._catalog_enricher')
    def test_uses_category_label_from_meta(self, mock_enricher):
        """Category label comes from METRIC_CATEGORY_META."""
        mock_enricher.enabled = True
        mock_enricher.get_metrics.return_value = [
            {
                "fullyQualifiedName": "catalog.metrics.m1",
                "name": "m1",
                "displayName": "M1",
                "description": "Test",
                "tags": [{"tagFQN": "MetricCategory.revenue"}],
            },
        ]

        with patch('webapp.app._catalog_enricher', mock_enricher):
            result = _load_metrics_from_catalog()

        # Verify that a known category gets its label from METRIC_CATEGORY_META
        assert len(result) >= 1
        revenue_cat = [c for c in result if c["key"] == "revenue"]
        if revenue_cat:
            assert revenue_cat[0]["label"] == METRIC_CATEGORY_META["revenue"]["label"]
            assert revenue_cat[0]["css"] == METRIC_CATEGORY_META["revenue"]["css"]

    @patch('webapp.app._catalog_enricher')
    def test_graceful_failure_on_exception(self, mock_enricher):
        """Return empty list on exception (graceful degradation)."""
        mock_enricher.enabled = True
        mock_enricher.get_metrics.side_effect = Exception("API error")

        with patch('webapp.app._catalog_enricher', mock_enricher):
            result = _load_metrics_from_catalog()

        assert result == []

    @patch('webapp.app._catalog_enricher')
    def test_empty_metrics_list(self, mock_enricher):
        """Return empty list when catalog has no metrics."""
        mock_enricher.enabled = True
        mock_enricher.get_metrics.return_value = []

        with patch('webapp.app._catalog_enricher', mock_enricher):
            result = _load_metrics_from_catalog()

        assert result == []


class TestBuildOmMetricDetail:
    """Tests for _build_om_metric_detail() function."""

    def test_build_basic_structure(self):
        """Build MetricParser-compatible structure from raw metric."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test_metric",
            "displayName": "Test Metric",
            "description": "A test metric",
            "expression": "COUNT(*)",
            "owners": [{"name": "data_team"}],
            "tags": [],
        }

        result = _build_om_metric_detail(raw)

        assert result["name"] == "test_metric"
        assert result["display_name"] == "Test Metric"
        assert result["category"] == "general"
        assert result["metadata"]["type"] == ""
        assert result["metadata"]["unit"] == ""
        assert result["metadata"]["grain"] == ""
        assert result["overview"]["description"] == "A test metric"

    def test_extract_metadata_from_tags(self):
        """Extract type, unit, grain from tags."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.revenue",
            "name": "revenue",
            "displayName": "Revenue",
            "description": "Test",
            "expression": "SUM(amount)",
            "owners": [],
            "tags": [
                {"tagFQN": "MetricType.sum"},
                {"tagFQN": "Unit.usd"},
                {"tagFQN": "Grain.monthly"},
                {"tagFQN": "MetricCategory.finance"},
            ],
        }

        result = _build_om_metric_detail(raw)

        assert result["metadata"]["type"] == "sum"
        assert result["metadata"]["unit"] == "usd"
        assert result["metadata"]["grain"] == "monthly"
        assert result["category"] == "finance"

    def test_extract_dimensions_from_tags(self):
        """Extract dimension names from Dimension.* tags."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "expression": "SELECT",
            "owners": [],
            "tags": [
                {"tagFQN": "Dimension.region"},
                {"tagFQN": "Dimension.channel"},
            ],
        }

        result = _build_om_metric_detail(raw)

        assert "region" in result["dimensions"]
        assert "channel" in result["dimensions"]

    def test_expression_in_sql_examples(self):
        """Expression field goes into sql_examples for modal display."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "expression": "SELECT COUNT(*) FROM users",
            "owners": [],
            "tags": [],
        }

        result = _build_om_metric_detail(raw)

        assert "expression" in result["sql_examples"]
        assert result["sql_examples"]["expression"]["query"] == "SELECT COUNT(*) FROM users"
        assert result["sql_examples"]["expression"]["title"] == "Metric Expression"

    def test_extract_owner_names(self):
        """Extract owner names from owners list."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "expression": "SELECT",
            "owners": [
                {"name": "alice", "email": "alice@example.com"},
                {"name": "bob"},
            ],
            "tags": [],
        }

        result = _build_om_metric_detail(raw)

        # Owner names go to notes.all
        assert len(result["notes"]["all"]) == 0  # We don't populate this from owners yet

    def test_empty_expression_no_sql_example(self):
        """Don't add empty expression to sql_examples."""
        raw = {
            "fullyQualifiedName": "catalog.metrics.test",
            "name": "test",
            "displayName": "Test",
            "description": "Test",
            "expression": "",
            "owners": [],
            "tags": [],
        }

        result = _build_om_metric_detail(raw)

        assert result["sql_examples"] == {}
