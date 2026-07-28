"""CLI tests for `agnes admin analytics migrate` (wave-2G Task 6)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestMigrate:
    def test_success_prints_status_target_job_and_message(self):
        body = {
            "status": "triggered",
            "to": "ducklake",
            "job_id": "job_abc123",
            "message": "DuckLake rebuild enqueued ... restart to switch over.",
        }
        with patch("cli.commands.admin_analytics.api_post", return_value=_resp(202, body)) as mock_post:
            result = runner.invoke(app, ["admin", "analytics", "migrate", "--to", "ducklake"])
        assert result.exit_code == 0, result.output
        assert "Status:  triggered" in result.output
        assert "Target:  ducklake" in result.output
        assert "Job:     job_abc123" in result.output
        assert "restart to switch over." in result.output
        mock_post.assert_called_once_with(
            "/api/admin/analytics/migrate",
            json={"to": "ducklake"},
        )

    def test_success_json_output(self):
        body = {"status": "triggered", "to": "legacy", "job_id": "job_xyz", "message": "..."}
        with patch("cli.commands.admin_analytics.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "analytics", "migrate", "--to", "legacy", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["job_id"] == "job_xyz"

    def test_prerequisites_failure_prints_problem_list_and_exits_1(self):
        detail = {
            "error": "ducklake_prerequisites_failed",
            "problems": ["extension not loadable", "catalog unreachable"],
        }
        with patch(
            "cli.commands.admin_analytics.api_post",
            return_value=_resp(400, {"detail": detail}),
        ):
            result = runner.invoke(app, ["admin", "analytics", "migrate", "--to", "ducklake"])
        assert result.exit_code == 1
        assert "extension not loadable" in result.output
        assert "catalog unreachable" in result.output

    def test_conflict_already_in_progress_exits_1(self):
        with patch(
            "cli.commands.admin_analytics.api_post",
            return_value=_resp(
                409,
                {"detail": {"error": "analytics_migrate_already_in_progress", "job_id": "job_existing"}},
            ),
        ):
            result = runner.invoke(app, ["admin", "analytics", "migrate", "--to", "ducklake"])
        assert result.exit_code == 1
        assert "job_existing" in result.output

    def test_conflict_already_in_progress_json_still_exits_1(self):
        with patch(
            "cli.commands.admin_analytics.api_post",
            return_value=_resp(
                409,
                {"detail": {"error": "analytics_migrate_already_in_progress", "job_id": "job_existing"}},
            ),
        ):
            result = runner.invoke(app, ["admin", "analytics", "migrate", "--to", "ducklake", "--json"])
        assert result.exit_code == 1
