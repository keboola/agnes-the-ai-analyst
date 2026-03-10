"""Tests for business metric YAML definitions and parser."""

import yaml
import pytest
from pathlib import Path

from webapp.utils.metric_parser import MetricParser


METRICS_DIR = Path(__file__).parent.parent / "docs" / "metrics"

REQUIRED_FIELDS = [
    "name", "display_name", "category", "type", "unit",
    "grain", "time_column", "table", "description", "expression",
]


def _get_all_metric_files():
    """Return list of all metric YAML files."""
    return sorted(METRICS_DIR.glob("*/*.yml"))


class TestMetricYAMLValidity:
    """Validate all metric YAML files have required fields."""

    def test_metrics_directory_exists(self):
        assert METRICS_DIR.exists(), f"Metrics directory not found: {METRICS_DIR}"

    def test_at_least_one_metric_exists(self):
        files = _get_all_metric_files()
        assert len(files) > 0, "No metric YAML files found"

    @pytest.mark.parametrize("metric_file", _get_all_metric_files(), ids=lambda f: f.relative_to(METRICS_DIR).as_posix())
    def test_all_metric_yamls_valid(self, metric_file):
        """Every metric YAML must have all required fields."""
        with open(metric_file) as f:
            raw = yaml.safe_load(f)

        assert isinstance(raw, list), f"{metric_file.name}: expected YAML list, got {type(raw).__name__}"
        assert len(raw) >= 1, f"{metric_file.name}: YAML list is empty"

        metric = raw[0]
        assert isinstance(metric, dict), f"{metric_file.name}: first item is not a dict"

        missing = [field for field in REQUIRED_FIELDS if field not in metric]
        assert not missing, f"{metric_file.name}: missing required fields: {missing}"

        # Category must match parent directory name
        expected_category = metric_file.parent.name
        assert metric["category"] == expected_category, (
            f"{metric_file.name}: category '{metric['category']}' != directory '{expected_category}'"
        )


class TestMetricCategoriesInParser:
    """Verify CATEGORY_COLORS has entries for all used categories."""

    def test_all_categories_have_colors(self):
        files = _get_all_metric_files()
        categories_used = set()
        for f in files:
            with open(f) as fh:
                raw = yaml.safe_load(fh)
            if isinstance(raw, list) and raw:
                categories_used.add(raw[0].get("category", ""))

        parser = MetricParser(METRICS_DIR)
        missing = categories_used - set(parser.CATEGORY_COLORS.keys())
        assert not missing, f"CATEGORY_COLORS missing entries for: {missing}"


class TestMetricParserParsesSample:
    """Parse one metric and verify structured output."""

    def test_parse_total_revenue(self):
        parser = MetricParser(METRICS_DIR)
        data = parser.parse_metric("revenue/total_revenue.yml")

        assert data["name"] == "total_revenue"
        assert data["display_name"] == "Total Revenue"
        assert data["category"] == "revenue"
        assert data["category_color"] == "#0073D1"
        assert data["metadata"]["unit"] == "USD"
        assert data["metadata"]["grain"] == "monthly"
        assert len(data["dimensions"]) > 0
        assert "sql" in data["sql_examples"]
        assert data["technical"]["table"] == "orders"
        assert data["technical"]["expression"] == "SUM(total_amount)"

    def test_parse_metric_with_tables_field(self):
        parser = MetricParser(METRICS_DIR)
        data = parser.parse_metric("revenue/average_order_value.yml")

        assert data["name"] == "average_order_value"
        assert "sql_by_segment" in data["sql_examples"]


class TestLoadMetricsData:
    """Verify _load_metrics_data returns correct structure."""

    def test_returns_four_categories(self):
        from webapp.app import _load_metrics_data
        result = _load_metrics_data()
        assert isinstance(result, list)
        assert len(result) == 4
        category_keys = [c["key"] for c in result]
        assert "revenue" in category_keys
        assert "customers" in category_keys
        assert "marketing" in category_keys
        assert "support" in category_keys

    def test_total_metrics_count(self):
        from webapp.app import _load_metrics_data
        result = _load_metrics_data()
        total = sum(len(c["metrics"]) for c in result)
        assert total == 10

    def test_metric_has_required_fields(self):
        from webapp.app import _load_metrics_data
        result = _load_metrics_data()
        for cat in result:
            for m in cat["metrics"]:
                assert "name" in m
                assert "display_name" in m
                assert "description" in m
                assert "grain" in m
                assert "path" in m


class TestDynamicSqlFields:
    """Verify sql_by_* fields are auto-discovered by parser."""

    def test_dynamic_sql_fields_discovered(self):
        parser = MetricParser(METRICS_DIR)
        data = parser.parse_metric("revenue/total_revenue.yml")
        # sql_by_channel should be found via dynamic discovery
        assert "sql_by_channel" in data["sql_examples"]
        assert data["sql_examples"]["sql_by_channel"]["title"] == "By Channel"

    def test_dynamic_sql_title_generation(self):
        parser = MetricParser(METRICS_DIR)
        data = parser.parse_metric("customers/repeat_purchase_rate.yml")
        # sql_by_channel should be found via dynamic discovery
        assert "sql_by_channel" in data["sql_examples"]
        assert data["sql_examples"]["sql_by_channel"]["title"] == "By Channel"

    def test_static_sql_still_works(self):
        parser = MetricParser(METRICS_DIR)
        data = parser.parse_metric("revenue/total_revenue.yml")
        assert "sql" in data["sql_examples"]
        assert data["sql_examples"]["sql"]["title"] == "Basic Query"
