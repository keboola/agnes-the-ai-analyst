"""CLI smoke tests for `agnes admin db ...`.

Covers `state`, `migrate`, `job`, and `cancel` subcommands.
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


class TestDbMigrate:
    def test_db_migrate_starts_job(self):
        """migrate side_car --detach returns immediately with job_id."""
        payload = {"job_id": "abc-123", "status": "running"}
        with patch(
            "cli.commands.db.api_post",
            return_value=_resp(202, payload),
        ):
            result = runner.invoke(
                app, ["admin", "db", "migrate", "side_car", "--detach", "--yes"]
            )
        assert result.exit_code == 0, result.output
        assert "abc-123" in result.output

    def test_db_migrate_cloud_with_url(self):
        """migrate cloud with --cloud-url --detach --yes succeeds without prompting."""
        payload = {"job_id": "cloud-1", "status": "running"}
        with patch(
            "cli.commands.db.api_post",
            return_value=_resp(202, payload),
        ) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin", "db", "migrate", "cloud",
                    "--cloud-url", "postgresql://test",
                    "--detach", "--yes",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "cloud-1" in result.output
        # Verify cloud_url was forwarded in the JSON body.
        _, kwargs = mock_post.call_args
        body = kwargs.get("json") or {}
        assert body.get("target") == "cloud"
        assert body.get("cloud_url") == "postgresql://test"


class TestDbJob:
    def test_job_shows_status_json(self):
        """`db job <id> --json` prints job status JSON."""
        payload = {
            "job_id": "abc-123",
            "status": "success",
            "current_step": "flip_backend",
            "progress_pct": 100,
            "summary": {"tables_migrated": 28},
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "job", "abc-123", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["job_id"] == "abc-123"

    def test_job_shows_status_text(self):
        """`db job <id>` prints human-readable status."""
        payload = {
            "job_id": "abc-123",
            "status": "running",
            "current_step": "copy_tables",
            "progress_pct": 42,
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "job", "abc-123"])
        assert result.exit_code == 0, result.output
        assert "abc-123" in result.output
        assert "running" in result.output
        assert "copy_tables" in result.output
        assert "42" in result.output

    def test_job_shows_error_when_failed(self):
        """Failed job displays the error block."""
        payload = {
            "job_id": "abc-123",
            "status": "failed",
            "current_step": "copy_tables",
            "progress_pct": 50,
            "error": {"step": "copy_tables", "message": "duck quacked"},
        }
        with patch("cli.commands.db.api_get", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["admin", "db", "job", "abc-123"])
        assert result.exit_code == 0, result.output
        assert "duck quacked" in result.output
        assert "copy_tables" in result.output

    def test_job_not_found(self):
        with patch(
            "cli.commands.db.api_get",
            return_value=_resp(404, {"detail": "Unknown job_id: abc-123"}),
        ):
            result = runner.invoke(app, ["admin", "db", "job", "abc-123"])
        assert result.exit_code != 0


class TestDbCancel:
    def test_cancel_succeeds(self):
        """`db cancel <id>` invokes POST /cancel/{id}, prints confirmation."""
        with patch(
            "cli.commands.db.api_post",
            return_value=_resp(200, {"cancelled": True}),
        ) as mock_post:
            result = runner.invoke(app, ["admin", "db", "cancel", "abc-123"])
        assert result.exit_code == 0, result.output
        assert "cancelled" in result.output.lower()
        # Verify the right URL was POSTed
        args, _ = mock_post.call_args
        assert "/api/admin/db/cancel/abc-123" in args[0]

    def test_cancel_rejected_past_point_of_no_return(self):
        """409 from server (past flip_backend) propagates as non-zero exit."""
        with patch(
            "cli.commands.db.api_post",
            return_value=_resp(
                409, {"detail": "Past point-of-no-return (step >= flip_backend)"}
            ),
        ):
            result = runner.invoke(app, ["admin", "db", "cancel", "abc-123"])
        assert result.exit_code != 0
        assert "409" in result.output or "point-of-no-return" in result.output


class TestDbMigrateConfirmGate:
    """Phase 6.1 — --yes / -y confirmation gate on `db migrate`."""

    _post_payload = {"job_id": "test-job-abc", "status": "pending"}
    _get_payload = {
        "job_id": "test-job-abc",
        "status": "success",
        "current_step": "flip_backend",
        "progress_pct": 100,
    }

    def _patches(self):
        """Return a dict of patch context managers for api_post and api_get."""
        return {
            "post": patch(
                "cli.commands.db.api_post",
                return_value=_resp(202, self._post_payload),
            ),
            "get": patch(
                "cli.commands.db.api_get",
                return_value=_resp(200, self._get_payload),
            ),
        }

    def test_refuses_without_yes_in_noninteractive_shell(self):
        """CLI MUST refuse a destructive migrate when stdin is not a TTY
        and --yes/-y wasn't passed.  Prevents fat-finger or accidental CI runs."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = False
            result = runner.invoke(app, ["admin", "db", "migrate", "side_car"])
        assert result.exit_code == 2, result.output
        assert "--yes" in result.output

    def test_proceeds_with_yes_flag_in_noninteractive_shell(self):
        """--yes bypasses the TTY check and the interactive confirmation."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = False
            result = runner.invoke(
                app, ["admin", "db", "migrate", "side_car", "--yes", "--detach"]
            )
        assert result.exit_code == 0, result.output

    def test_proceeds_with_short_y_flag(self):
        """-y is the short form of --yes."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = False
            result = runner.invoke(
                app, ["admin", "db", "migrate", "side_car", "-y", "--detach"]
            )
        assert result.exit_code == 0, result.output

    def test_json_without_yes_refuses(self):
        """MED-1: --json alone does NOT bypass the confirmation gate.
        CI/cron callers must pass --yes explicitly; --json is output format,
        not operator intent."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = False
            result = runner.invoke(
                app, ["admin", "db", "migrate", "side_car", "--json"]
            )
        assert result.exit_code != 0, result.output
        assert "--yes" in result.output

    def test_interactive_yes_proceeds(self):
        """When stdin is a TTY and the user answers 'y', migrate proceeds."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = True
            result = runner.invoke(
                app,
                ["admin", "db", "migrate", "side_car", "--detach"],
                input="y\n",
            )
        assert result.exit_code == 0, result.output

    def test_interactive_no_aborts(self):
        """When stdin is a TTY and the user answers 'n', the command aborts
        cleanly with exit 1."""
        with (
            self._patches()["post"],
            self._patches()["get"],
            patch("cli.commands.db.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = True
            result = runner.invoke(
                app,
                ["admin", "db", "migrate", "side_car"],
                input="n\n",
            )
        assert result.exit_code == 1, result.output
        assert "ancel" in result.output  # "Cancelled by operator"
