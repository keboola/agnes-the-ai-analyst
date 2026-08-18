"""CLI tests for `agnes admin doctor --new-instance`."""

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


def _report(status="ok", check_status="ok"):
    return {
        "status": status,
        "checks": [
            {
                "name": "login-door",
                "status": check_status,
                "audience": "operator",
                "detail": "usable login door(s): password",
            },
            {
                "name": "email-delivery",
                "status": "info",
                "audience": "operator",
                "detail": "no email transport configured",
            },
        ],
    }


class TestProfileSelection:
    def test_bare_doctor_hints_the_profile(self):
        result = runner.invoke(app, ["admin", "doctor"])
        assert result.exit_code == 2
        assert "--new-instance" in result.output


class TestNewInstance:
    def test_ok_report_renders_checklist_and_exits_zero(self):
        with patch("cli.commands.admin_doctor.api_post", return_value=_resp(200, _report())) as post:
            result = runner.invoke(app, ["admin", "doctor", "--new-instance"])
        assert result.exit_code == 0
        assert "PASS login-door" in result.output
        assert "INFO email-delivery" in result.output
        assert "Overall: ok" in result.output
        assert post.call_args.args[0] == "/api/admin/doctor/new-instance"
        assert post.call_args.kwargs["json"] == {}

    def test_error_report_exits_nonzero(self):
        with patch(
            "cli.commands.admin_doctor.api_post",
            return_value=_resp(200, _report(status="error", check_status="error")),
        ):
            result = runner.invoke(app, ["admin", "doctor", "--new-instance"])
        assert result.exit_code == 1
        assert "FAIL login-door" in result.output

    def test_email_to_is_forwarded(self):
        with patch("cli.commands.admin_doctor.api_post", return_value=_resp(200, _report())) as post:
            result = runner.invoke(app, ["admin", "doctor", "--new-instance", "--email-to", "ops@example.org"])
        assert result.exit_code == 0
        assert post.call_args.kwargs["json"] == {"email_to": "ops@example.org"}

    def test_json_output(self):
        with patch("cli.commands.admin_doctor.api_post", return_value=_resp(200, _report())):
            result = runner.invoke(app, ["admin", "doctor", "--new-instance", "--json"])
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["checks"][0]["name"] == "login-door"

    def test_http_error_surfaces_detail(self):
        with patch(
            "cli.commands.admin_doctor.api_post",
            return_value=_resp(403, {"detail": "Admin access required"}),
        ):
            result = runner.invoke(app, ["admin", "doctor", "--new-instance"])
        assert result.exit_code == 1
        assert "Admin access required" in result.output
