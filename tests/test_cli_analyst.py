"""Tests for da analyst setup/status commands."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _httpx_resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    return r


class TestAnalystStatus:
    def test_status_uninitialized(self, tmp_path):
        """Status shows 'no' for uninitialized workspace."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = runner.invoke(app, ["analyst", "status", "--workspace", str(workspace)])
        assert result.exit_code == 0
        assert "no" in result.output.lower() or "missing" in result.output.lower()

    def test_status_initialized(self, tmp_path):
        """Status shows initialized when CLAUDE.md with marker exists."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("# AI Data Analyst\nHello")
        (workspace / "data" / "parquet").mkdir(parents=True)
        (workspace / "data" / "metadata").mkdir(parents=True)

        result = runner.invoke(app, ["analyst", "status", "--workspace", str(workspace)])
        assert result.exit_code == 0
        assert "yes" in result.output.lower() or "initialized" in result.output.lower()

    def test_status_json_output(self, tmp_path):
        """--json flag produces valid JSON with expected keys."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = runner.invoke(app, ["analyst", "status", "--workspace", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "initialized" in data
        assert "freshness" in data
        assert "parquet_tables" in data

    def test_status_fresh_data(self, tmp_path):
        """Status shows 'fresh' when last_sync is recent."""
        from datetime import datetime, timezone
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("# AI Data Analyst\n")
        meta_dir = workspace / "data" / "metadata"
        meta_dir.mkdir(parents=True)
        (meta_dir / "last_sync.json").write_text(
            json.dumps({"synced_at": datetime.now(timezone.utc).isoformat()})
        )
        result = runner.invoke(app, ["analyst", "status", "--workspace", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["freshness"] == "fresh"

    def test_status_stale_data(self, tmp_path):
        """Status shows 'stale' when last_sync is >24 h ago."""
        from datetime import datetime, timezone, timedelta
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("# AI Data Analyst\n")
        meta_dir = workspace / "data" / "metadata"
        meta_dir.mkdir(parents=True)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        (meta_dir / "last_sync.json").write_text(json.dumps({"synced_at": old_ts}))
        result = runner.invoke(app, ["analyst", "status", "--workspace", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["freshness"] == "stale"


class TestAnalystSetup:
    def test_setup_existing_workspace_blocked(self, tmp_path):
        """Setup fails if workspace already initialized and --force not given."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("# AI Data Analyst\nInitialized")

        result = runner.invoke(app, [
            "analyst", "setup", "--server-url", "http://server", "--workspace", str(workspace),
        ])
        assert result.exit_code == 1
        assert "force" in result.output.lower() or "existing" in result.output.lower()

    def test_setup_server_unreachable(self, tmp_path):
        """Setup exits cleanly when server cannot be reached.
        httpx is imported inside _connect_to_instance, so patch the module reference
        that the function will use at call time.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        import httpx as _httpx
        mock_httpx = MagicMock(spec=_httpx)
        mock_httpx.get.side_effect = Exception("Connection refused")
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = runner.invoke(
                app,
                ["analyst", "setup", "--server-url", "http://unreachable:9999",
                 "--workspace", str(workspace)],
            )
        assert result.exit_code == 1
        assert "Cannot reach" in result.output
