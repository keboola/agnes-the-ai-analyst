"""CLI smoke tests for `agnes admin db ...`.

Covers the read-only `state` subcommand; mutation subcommands (migrate,
job, cancel) are covered by later phases.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _resp(status_code: int = 200, json_data: dict | None = None, text: str = ""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


class TestDbState:
    def test_db_state_json(self):
        payload = {
            "backend": "duckdb",
            "url_redacted": None,
            "allowed_transitions": ["side_car"],
            "current_job_id": None,
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "state", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["backend"] == "duckdb"
        assert data["allowed_transitions"] == ["side_car"]

    def test_db_state_text(self):
        payload = {
            "backend": "side_car",
            "url_redacted": "postgresql://agnes:****@postgres:5432/agnes",
            "allowed_transitions": ["cloud"],
            "current_job_id": None,
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "state"])
        assert result.exit_code == 0, result.output
        assert "side_car" in result.output
        assert "postgresql://agnes:****@postgres:5432/agnes" in result.output
        assert "cloud" in result.output

    def test_db_state_text_with_active_job(self):
        payload = {
            "backend": "side_car_in_progress",
            "url_redacted": None,
            "allowed_transitions": [],
            "current_job_id": "abc-123",
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "state"])
        assert result.exit_code == 0, result.output
        assert "abc-123" in result.output

    def test_db_state_api_error(self):
        with patch(
            "cli.commands.db.api_get",
            return_value=_resp(500, {"detail": "server boom"}, "server boom"),
        ):
            result = runner.invoke(app, ["admin", "db", "state"])
        assert result.exit_code != 0
