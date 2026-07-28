# tests/test_cli_db_migrate_yes_gate.py
"""MED-1 — ``--json`` does not bypass the ``--yes`` confirmation gate."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _resp(status_code: int = 200, json_data: dict | None = None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = ""
    return r


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def test_migrate_json_without_yes_refuses() -> None:
    """``agnes admin db migrate cloud --cloud-url ... --json`` (no
    ``--yes``) must refuse rather than silently auto-confirming.

    Pre-MED-1 the predicate ``needs_confirm = not yes and not as_json``
    accepted ``--json`` as a confirmation bypass; CI/cron callers
    could fire a destructive cutover with zero operator intent.
    """
    fake_response = {"job_id": "j1", "status": "pending"}
    with (
        patch("cli.commands.db.api_post", return_value=_resp(202, fake_response)),
        patch("cli.commands.db.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = False
        result = runner.invoke(
            app,
            ["admin", "db", "migrate", "cloud", "--cloud-url", "postgresql://x:y@h/db", "--json"],
            catch_exceptions=False,
        )
    assert result.exit_code != 0, (
        "CLI must non-zero when --json is passed without --yes; "
        f"got rc={result.exit_code}, stdout={result.output!r}"
    )
    assert "--yes" in result.output or "confirm" in result.output.lower(), (
        "error message must mention the --yes requirement"
    )


def test_migrate_json_with_yes_proceeds() -> None:
    """``--json --yes`` together is the explicit CI/cron path — proceeds."""
    fake_response = {"job_id": "j1", "status": "pending"}
    with (
        patch("cli.commands.db.api_post", return_value=_resp(202, fake_response)),
        patch("cli.commands.db.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = False
        result = runner.invoke(
            app,
            ["admin", "db", "migrate", "cloud", "--cloud-url", "postgresql://x:y@h/db", "--json", "--yes"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, (
        f"--json --yes should proceed; got rc={result.exit_code}, stdout={result.output!r}"
    )
    assert "j1" in result.output
