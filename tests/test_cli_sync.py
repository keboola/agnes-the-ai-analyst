"""Tests for da sync command."""

import json
import pytest
from unittest.mock import patch, MagicMock, call

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    return r


MANIFEST = {
    "tables": {
        "orders": {"hash": "abc123", "rows": 100, "size_bytes": 2048},
        "customers": {"hash": "def456", "rows": 50, "size_bytes": 1024},
    }
}


class TestSyncHappyPath:
    def test_sync_downloads_all_tables(self, tmp_config):
        """Sync with no local state downloads all tables."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download") as mock_dl:
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 2
        assert "Downloaded: 2" in result.output

    def test_sync_specific_table(self, tmp_config):
        """--table flag limits download to one table."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download") as mock_dl:
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync", "--table", "orders"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 1
        call_path = mock_dl.call_args[0][0]
        assert "orders" in call_path

    def test_sync_json_output(self, tmp_config):
        """--json flag produces valid JSON output (rich spinner may precede JSON)."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download"):
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync", "--json"])
        assert result.exit_code == 0
        # Rich Progress may output a spinner line before the JSON block
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert "downloaded" in data
        assert "errors" in data

    def test_sync_upload_only(self, tmp_config):
        """--upload-only skips download and calls upload."""
        with patch("cli.commands.sync.api_post", return_value=_resp(200)):
            result = runner.invoke(app, ["sync", "--upload-only"])
        assert result.exit_code == 0
        assert "session" in result.output.lower() or "upload" in result.output.lower()


class TestSyncErrors:
    def test_sync_manifest_failure(self, tmp_config):
        """Manifest fetch failure exits with error."""
        r = _resp(500)
        r.raise_for_status.side_effect = Exception("Server error")
        with patch("cli.commands.sync.api_get", return_value=r):
            result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "Failed to fetch manifest" in result.output

    def test_sync_download_error_recorded(self, tmp_config):
        """Download error is recorded in results but does not abort sync."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=Exception("timeout")):
                result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "Errors" in result.output

    def test_sync_skips_unchanged_tables(self, tmp_config, monkeypatch):
        """Tables with matching hashes are not re-downloaded."""
        state = {
            "tables": {
                "orders": {"hash": "abc123"},
                "customers": {"hash": "def456"},
            }
        }
        with patch("cli.commands.sync.get_sync_state", return_value=state):
            with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
                with patch("cli.commands.sync.stream_download") as mock_dl:
                    result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        # Nothing to download — both hashes match
        assert mock_dl.call_count == 0
        assert "Downloaded: 0" in result.output
