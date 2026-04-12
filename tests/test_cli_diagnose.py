"""Tests for da diagnose command."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.elapsed = MagicMock()
    r.elapsed.total_seconds.return_value = 0.042
    return r


HEALTHY_HEALTH = {
    "status": "ok",
    "instance_name": "Test Instance",
    "services": {
        "duckdb": {"status": "ok", "tables": 5},
        "scheduler": {"status": "ok"},
    },
}


class TestDiagnoseText:
    def test_diagnose_healthy(self):
        """Diagnose healthy system shows 'healthy' overall."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "healthy" in result.output.lower()
        assert "api" in result.output
        assert "duckdb" in result.output

    def test_diagnose_api_unreachable(self):
        """Diagnose marks overall as unhealthy when API is down."""
        with patch("cli.commands.diagnose.api_get", side_effect=Exception("Connection refused")):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "unhealthy" in result.output.lower()
        assert "api" in result.output
        assert "Server unreachable" in result.output or "Suggested actions" in result.output

    def test_diagnose_warning_service(self):
        """Diagnose shows 'degraded' when a service reports warning."""
        health = {
            "services": {
                "duckdb": {"status": "warning", "stale_tables": ["orders"]},
            }
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()


class TestDiagnoseJson:
    def test_diagnose_json_output(self):
        """--json flag produces parseable JSON with expected structure."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "overall" in data
        assert "checks" in data
        assert "suggested_actions" in data

    def test_diagnose_json_api_down(self):
        """--json flag still emits valid JSON when API is unreachable."""
        with patch("cli.commands.diagnose.api_get", side_effect=Exception("timeout")):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["overall"] == "unhealthy"
        assert any(c["name"] == "api" and c["status"] == "error" for c in data["checks"])

    def test_diagnose_json_has_latency(self):
        """Healthy API check includes latency_ms in JSON output."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        api_check = next(c for c in data["checks"] if c["name"] == "api")
        assert "latency_ms" in api_check
