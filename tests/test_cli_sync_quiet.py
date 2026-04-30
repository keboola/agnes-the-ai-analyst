"""Verify `da sync --quiet` suppresses progress output but still completes."""
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from cli.main import app


def test_quiet_flag_suppresses_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"tables": {}, "assets": {}, "server_time": "2026-04-30T00:00:00Z"}
    fake_resp.raise_for_status = MagicMock()

    with patch("cli.commands.sync.api_get", return_value=fake_resp):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 0
    # No spinner glyphs, no "Found X tables" header
    assert "Found" not in result.stdout
    assert "Downloading" not in result.stdout
    # Final summary line is allowed and expected
    assert "Downloaded:" in result.stdout or result.stdout.strip() == ""
