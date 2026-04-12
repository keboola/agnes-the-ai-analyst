"""Tests for da metrics list/show commands."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    return r


METRICS_LIST = [
    {"id": "revenue/mrr", "name": "mrr", "display_name": "Monthly Recurring Revenue",
     "category": "revenue", "unit": "USD"},
    {"id": "revenue/arr", "name": "arr", "display_name": "Annual Recurring Revenue",
     "category": "revenue", "unit": "USD"},
    {"id": "product/dau", "name": "dau", "display_name": "Daily Active Users",
     "category": "product", "unit": "users"},
]

MRR_DETAIL = {
    "id": "revenue/mrr",
    "name": "mrr",
    "display_name": "Monthly Recurring Revenue",
    "category": "revenue",
    "type": "sum",
    "unit": "USD",
    "grain": "monthly",
    "table_name": "subscriptions",
    "sql": "SELECT SUM(amount) FROM subscriptions WHERE status='active'",
    "description": "Total monthly recurring revenue from active subscriptions.",
    "synonyms": ["MRR", "monthly revenue"],
}


class TestMetricsList:
    def test_list_metrics_text(self):
        """list command groups metrics by category."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, METRICS_LIST)):
            result = runner.invoke(app, ["metrics", "list"])
        assert result.exit_code == 0
        assert "revenue" in result.output
        assert "mrr" in result.output
        assert "dau" in result.output

    def test_list_metrics_json(self):
        """--json flag outputs raw metric list as JSON."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, METRICS_LIST)):
            result = runner.invoke(app, ["metrics", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 3
        assert data[0]["id"] == "revenue/mrr"

    def test_list_metrics_empty(self):
        """Empty metric list shows 'No metrics found'."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, [])):
            result = runner.invoke(app, ["metrics", "list"])
        assert result.exit_code == 0
        assert "No metrics" in result.output

    def test_list_metrics_api_failure(self):
        """API error exits with non-zero code."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(500, {"detail": "Server error"})):
            result = runner.invoke(app, ["metrics", "list"])
        assert result.exit_code == 1

    def test_list_metrics_category_filter_passed(self):
        """--category passes query param to API."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, [])) as mock_get:
            runner.invoke(app, ["metrics", "list", "--category", "revenue"])
        mock_get.assert_called_once_with("/api/metrics", params={"category": "revenue"})

    def test_list_metrics_dict_response(self):
        """Response wrapped in {metrics: [...]} dict is handled."""
        wrapped = {"metrics": METRICS_LIST}
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, wrapped)):
            result = runner.invoke(app, ["metrics", "list"])
        assert result.exit_code == 0
        assert "mrr" in result.output


class TestMetricsShow:
    def test_show_metric_text(self):
        """show command displays metric details in text format."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, MRR_DETAIL)):
            result = runner.invoke(app, ["metrics", "show", "revenue/mrr"])
        assert result.exit_code == 0
        assert "Monthly Recurring Revenue" in result.output
        assert "SELECT SUM(amount)" in result.output
        assert "subscriptions" in result.output

    def test_show_metric_json(self):
        """--json flag outputs full metric detail as JSON."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(200, MRR_DETAIL)):
            result = runner.invoke(app, ["metrics", "show", "revenue/mrr", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "revenue/mrr"
        assert "sql" in data

    def test_show_metric_not_found(self):
        """Missing metric ID exits with 404 error."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(404, {})):
            result = runner.invoke(app, ["metrics", "show", "nonexistent/metric"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_show_metric_api_error(self):
        """Non-404 API error also exits with error."""
        with patch("cli.commands.metrics.api_get", return_value=_resp(500, {"detail": "error"})):
            result = runner.invoke(app, ["metrics", "show", "revenue/mrr"])
        assert result.exit_code == 1
