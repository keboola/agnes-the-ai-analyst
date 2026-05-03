"""Tests for analyst bootstrap flow."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.chdir(ws)
    yield ws


# ---------------------------------------------------------------------------
# TestDetectExistingProject
# ---------------------------------------------------------------------------

class TestDetectExistingProject:
    def test_no_settings_json_returns_false(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project

        assert _detect_existing_project(tmp_workspace) is False

    def test_settings_json_with_da_sync_returns_true(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project
        import json as _json

        claude_dir = tmp_workspace / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "da sync --quiet 2>/dev/null || true"}]}],
            }
        }
        (claude_dir / "settings.json").write_text(_json.dumps(settings), encoding="utf-8")
        assert _detect_existing_project(tmp_workspace) is True

    def test_settings_json_without_da_sync_returns_false(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project
        import json as _json

        claude_dir = tmp_workspace / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.json").write_text(
            _json.dumps({"model": "sonnet"}), encoding="utf-8"
        )
        assert _detect_existing_project(tmp_workspace) is False

    def test_setup_blocked_when_existing_without_force(self, tmp_workspace):
        """Setup must exit(1) when workspace exists and --force not supplied."""
        import json as _json

        claude_dir = tmp_workspace / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "da sync --quiet 2>/dev/null || true"}]}],
            }
        }
        (claude_dir / "settings.json").write_text(_json.dumps(settings), encoding="utf-8")
        result = runner.invoke(app, ["analyst", "setup", "--server-url", "http://localhost:8000"])
        assert result.exit_code == 1
        assert "force" in result.output.lower() or "force" in (result.stderr or "").lower()

    def test_setup_proceeds_with_force(self, tmp_workspace):
        """--force bypasses existing-project detection."""
        import json as _json

        claude_dir = tmp_workspace / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "da sync --quiet 2>/dev/null || true"}]}],
            }
        }
        (claude_dir / "settings.json").write_text(_json.dumps(settings), encoding="utf-8")

        with patch("cli.commands.analyst._connect_to_instance", return_value="tok"), \
             patch("cli.commands.analyst._download_metadata"), \
             patch("cli.commands.analyst._download_data", return_value=0), \
             patch("cli.commands.analyst._initialize_duckdb", return_value=0), \
             patch("cli.commands.analyst._init_claude_workspace"):
            result = runner.invoke(
                app,
                ["analyst", "setup", "--server-url", "http://localhost:8000", "--force"],
            )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# TestCreateWorkspace
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    def test_creates_all_directories(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace

        _create_workspace(tmp_workspace)

        expected = [
            tmp_workspace / "data" / "parquet",
            tmp_workspace / "data" / "duckdb",
            tmp_workspace / "data" / "metadata",
            tmp_workspace / "user" / "artifacts",
            tmp_workspace / "user" / "sessions",
            tmp_workspace / ".claude",
        ]
        for d in expected:
            assert d.is_dir(), f"Expected directory missing: {d}"

    def test_idempotent(self, tmp_workspace):
        """Calling _create_workspace twice should not raise."""
        from cli.commands.analyst import _create_workspace

        _create_workspace(tmp_workspace)
        _create_workspace(tmp_workspace)  # should not raise


# ---------------------------------------------------------------------------
# TestInitClaudeWorkspace
# ---------------------------------------------------------------------------

class TestInitClaudeWorkspace:
    """Tests for _init_claude_workspace."""

    def test_does_not_write_claude_md_when_no_server_url(self, tmp_workspace):
        """Without server_url, CLAUDE.md must not be written."""
        from cli.commands.analyst import _create_workspace, _init_claude_workspace

        _create_workspace(tmp_workspace)
        _init_claude_workspace(tmp_workspace)

        assert not (tmp_workspace / "CLAUDE.md").exists(), (
            "CLAUDE.md must NOT be written when no server_url is provided"
        )

    def test_writes_claude_md_when_server_returns_200(self, tmp_workspace):
        """When /api/welcome returns 200, CLAUDE.md is written."""
        from cli.commands.analyst import _create_workspace, _init_claude_workspace
        from unittest.mock import MagicMock, patch

        _create_workspace(tmp_workspace)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": "# My CLAUDE.md\nHello analyst."}
        mock_resp.raise_for_status = MagicMock()

        with patch("cli.commands.analyst.httpx.get", return_value=mock_resp):
            _init_claude_workspace(tmp_workspace, server_url="https://example.com", token="tok")

        claude_md = tmp_workspace / "CLAUDE.md"
        assert claude_md.exists()
        assert "My CLAUDE.md" in claude_md.read_text(encoding="utf-8")

    def test_does_not_write_claude_md_when_no_claude_md_flag(self, tmp_workspace):
        """When server_url/token are empty (--no-claude-md path), CLAUDE.md is not written."""
        from cli.commands.analyst import _create_workspace, _init_claude_workspace

        _create_workspace(tmp_workspace)
        _init_claude_workspace(tmp_workspace, server_url="", token="")

        assert not (tmp_workspace / "CLAUDE.md").exists()

    def test_does_not_write_claude_md_on_404(self, tmp_workspace):
        """When /api/welcome returns 404 (older server), CLAUDE.md is skipped gracefully."""
        from cli.commands.analyst import _create_workspace, _init_claude_workspace
        from unittest.mock import MagicMock, patch
        import httpx

        _create_workspace(tmp_workspace)

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        with patch("cli.commands.analyst.httpx.get", return_value=mock_resp):
            # Must not raise
            _init_claude_workspace(tmp_workspace, server_url="https://example.com", token="tok")

        assert not (tmp_workspace / "CLAUDE.md").exists()

    def test_creates_claude_local_md_when_absent(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _init_claude_workspace

        _create_workspace(tmp_workspace)
        _init_claude_workspace(tmp_workspace)

        local_md = tmp_workspace / ".claude" / "CLAUDE.local.md"
        assert local_md.exists()
        assert local_md.read_text(encoding="utf-8").strip() != ""

    def test_does_not_overwrite_existing_local_md(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _init_claude_workspace

        _create_workspace(tmp_workspace)
        local_md = tmp_workspace / ".claude" / "CLAUDE.local.md"
        original_content = "# My custom notes\n\nDo not overwrite me.\n"
        local_md.write_text(original_content, encoding="utf-8")

        _init_claude_workspace(tmp_workspace)

        assert local_md.read_text(encoding="utf-8") == original_content

    def test_writes_settings_json(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _init_claude_workspace
        import json as _json

        _create_workspace(tmp_workspace)
        _init_claude_workspace(tmp_workspace)

        settings = _json.loads(
            (tmp_workspace / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        assert settings["model"] == "sonnet"
        assert "Read" in settings["permissions"]["allow"]

    def test_installs_session_hooks(self, tmp_workspace):
        """SessionStart and SessionEnd hooks must be present in settings.json."""
        from cli.commands.analyst import _create_workspace, _init_claude_workspace
        import json as _json

        _create_workspace(tmp_workspace)
        _init_claude_workspace(tmp_workspace)

        settings = _json.loads(
            (tmp_workspace / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        hooks = settings.get("hooks", {})
        assert "SessionStart" in hooks
        assert "SessionEnd" in hooks


# ---------------------------------------------------------------------------
# TestReturningSession
# ---------------------------------------------------------------------------

class TestReturningSession:
    def test_missing_when_no_last_sync_file(self, tmp_workspace):
        from cli.commands.analyst import _check_data_freshness

        assert _check_data_freshness(tmp_workspace) == "missing"

    def test_fresh_when_recent_sync(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _check_data_freshness

        _create_workspace(tmp_workspace)
        synced_at = datetime.now(timezone.utc).isoformat()
        (tmp_workspace / "data" / "metadata" / "last_sync.json").write_text(
            json.dumps({"synced_at": synced_at}), encoding="utf-8"
        )
        assert _check_data_freshness(tmp_workspace) == "fresh"

    def test_stale_when_old_sync(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _check_data_freshness

        _create_workspace(tmp_workspace)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        (tmp_workspace / "data" / "metadata" / "last_sync.json").write_text(
            json.dumps({"synced_at": old_time}), encoding="utf-8"
        )
        assert _check_data_freshness(tmp_workspace) == "stale"

    def test_status_command_output(self, tmp_workspace):
        result = runner.invoke(app, ["analyst", "status"])
        assert result.exit_code == 0
        assert "freshness" in result.output.lower() or "Data freshness" in result.output

    def test_status_command_json(self, tmp_workspace):
        result = runner.invoke(app, ["analyst", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "freshness" in data
        assert data["freshness"] == "missing"
        assert "parquet_tables" in data

    def test_status_fresh_after_setup_metadata(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace

        _create_workspace(tmp_workspace)
        synced_at = datetime.now(timezone.utc).isoformat()
        (tmp_workspace / "data" / "metadata" / "last_sync.json").write_text(
            json.dumps({"synced_at": synced_at}), encoding="utf-8"
        )

        result = runner.invoke(app, ["analyst", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["freshness"] == "fresh"
