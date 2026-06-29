"""Tests for agnes diagnose command."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
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

    def test_diagnose_json_wires_session_upload_warning(self, tmp_path):
        """`agnes diagnose --json` calls session_upload_health when
        workspace_root is set, appends the `session-upload` check, and surfaces
        the `agnes push --dry-run` suggested action on warning."""
        warning = {
            "name": "session-upload",
            "status": "warning",
            "expected_sessions": 9,
            "uploaded_entries": 1,
            "detail": "session upload may be failing. Try: `agnes push --dry-run`",
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)), \
             patch("cli.commands.diagnose.get_workspace_root", return_value=str(tmp_path)), \
             patch("cli.commands.diagnose.session_upload_health", return_value=warning):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert any(
            c["name"] == "session-upload" and c["status"] == "warning"
            for c in data["checks"]
        )
        assert any("agnes push --dry-run" in a for a in data["suggested_actions"])

    def test_diagnose_json_session_upload_info_without_workspace_root(self):
        """No workspace_root → session-upload check is info (not warning) and
        session_upload_health is not even called."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)), \
             patch("cli.commands.diagnose.get_workspace_root", return_value=None), \
             patch("cli.commands.diagnose.session_upload_health") as mock_health:
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        mock_health.assert_not_called()
        data = json.loads(result.output)
        check = next(c for c in data["checks"] if c["name"] == "session-upload")
        assert check["status"] == "info"


class TestAnalystAudienceFilter:
    """Issue #345 B — analysts shouldn't see ``Overall: degraded`` on fresh
    install just because the server has operator-level warnings (stale
    tables, session-pipeline behind). The role-aware headline lets the
    server tag each check with ``audience`` and report ``caller_role``;
    the CLI filters the overall when caller is analyst.
    """

    def test_analyst_sees_healthy_when_only_operator_checks_warn(self):
        """Analyst role + operator-side warning → ``Overall: healthy``
        with a secondary line surfacing the operator warning count.
        Pre-fix behaviour was ``Overall: degraded`` even though the
        analyst can't act on the stale-tables warning."""
        health = {
            "caller_role": "analyst",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
                "session_pipeline": {
                    "status": "warning",
                    "audience": "operator",
                    "detail": "verification-detector behind by ~8.8h",
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        # Headline is analyst-side healthy with a secondary count line
        assert "healthy (analyst-side)" in result.output
        assert "2 operator-side warnings" in result.output
        # Per-check rows still show the warnings — we just don't escalate
        assert "[warning] data" in result.output

    def test_admin_caller_role_aggregates_full_set(self):
        """Admin/operator role auto-promotes to the full aggregation —
        operator-side warnings DO escalate the headline."""
        health = {
            "caller_role": "admin",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()
        # No analyst-side qualifier — admin sees the full headline directly
        assert "analyst-side" not in result.output

    def test_analyst_with_include_operator_checks_flag(self):
        """``--include-operator-checks`` lets an analyst opt in to the
        full aggregation when they actually want to see the operator
        warnings drive the headline (e.g. when paging an operator)."""
        health = {
            "caller_role": "analyst",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose", "--include-operator-checks"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()
        assert "analyst-side" not in result.output

    def test_legacy_server_response_keeps_full_aggregation(self):
        """When the server doesn't ship ``caller_role`` (older deploy),
        the CLI must NOT silently start filtering — that would regress
        diagnoses against any pre-#345-B server. Test_diagnose_warning_service
        above already covers the same shape; this test makes the contract
        explicit."""
        health = {
            "services": {
                "duckdb": {"status": "warning", "stale_tables": ["x"]},
            },
            # No caller_role → no role-aware filtering.
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()
