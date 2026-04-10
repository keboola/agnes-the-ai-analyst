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
    def test_no_claude_md_returns_false(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project

        assert _detect_existing_project(tmp_workspace) is False

    def test_claude_md_with_marker_returns_true(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project

        (tmp_workspace / "CLAUDE.md").write_text(
            "# Acme — AI Data Analyst\n\nThis workspace is connected to http://localhost:8000.\n",
            encoding="utf-8",
        )
        assert _detect_existing_project(tmp_workspace) is True

    def test_claude_md_without_marker_returns_false(self, tmp_workspace):
        from cli.commands.analyst import _detect_existing_project

        (tmp_workspace / "CLAUDE.md").write_text(
            "# Some Other Project\n\nNot an analyst workspace.\n",
            encoding="utf-8",
        )
        assert _detect_existing_project(tmp_workspace) is False

    def test_setup_blocked_when_existing_without_force(self, tmp_workspace):
        """Setup must exit(1) when workspace exists and --force not supplied."""
        (tmp_workspace / "CLAUDE.md").write_text(
            "# Acme — AI Data Analyst\nThis workspace is connected to http://localhost:8000.\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["analyst", "setup", "--server-url", "http://localhost:8000"])
        assert result.exit_code == 1
        assert "force" in result.output.lower() or "force" in (result.stderr or "").lower()

    def test_setup_proceeds_with_force(self, tmp_workspace):
        """--force bypasses existing-project detection."""
        (tmp_workspace / "CLAUDE.md").write_text(
            "# Acme — AI Data Analyst\nThis workspace is connected to http://localhost:8000.\n",
            encoding="utf-8",
        )

        with patch("cli.commands.analyst._connect_to_instance", return_value="tok"), \
             patch("cli.commands.analyst._download_metadata"), \
             patch("cli.commands.analyst._download_data", return_value=0), \
             patch("cli.commands.analyst._initialize_duckdb", return_value=0), \
             patch("cli.commands.analyst._get_instance_name", return_value="Acme"), \
             patch("cli.commands.analyst._generate_claude_md"):
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
# TestGenerateClaudeMd
# ---------------------------------------------------------------------------

class TestGenerateClaudeMd:
    def test_template_substitution(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _generate_claude_md

        _create_workspace(tmp_workspace)
        _generate_claude_md(
            tmp_workspace,
            instance_name="Acme Corp",
            server_url="https://data.acme.com",
            sync_interval="2 hours",
        )

        content = (tmp_workspace / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Acme Corp" in content
        assert "https://data.acme.com" in content
        assert "2 hours" in content

    def test_creates_claude_local_md_when_absent(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _generate_claude_md

        _create_workspace(tmp_workspace)
        _generate_claude_md(
            tmp_workspace,
            instance_name="Acme",
            server_url="http://localhost:8000",
            sync_interval="1 hour",
        )

        local_md = tmp_workspace / ".claude" / "CLAUDE.local.md"
        assert local_md.exists()
        assert local_md.read_text(encoding="utf-8").strip() != ""

    def test_does_not_overwrite_existing_local_md(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace, _generate_claude_md

        _create_workspace(tmp_workspace)
        local_md = tmp_workspace / ".claude" / "CLAUDE.local.md"
        original_content = "# My custom notes\n\nDo not overwrite me.\n"
        local_md.write_text(original_content, encoding="utf-8")

        _generate_claude_md(
            tmp_workspace,
            instance_name="Acme",
            server_url="http://localhost:8000",
            sync_interval="1 hour",
        )

        assert local_md.read_text(encoding="utf-8") == original_content

    def test_uses_template_file_if_available(self, tmp_workspace):
        """Smoke-test that the real template file is found and substituted."""
        from cli.commands.analyst import _create_workspace, _generate_claude_md

        _create_workspace(tmp_workspace)
        _generate_claude_md(
            tmp_workspace,
            instance_name="TestCo",
            server_url="https://test.example.com",
            sync_interval="30 minutes",
        )

        content = (tmp_workspace / "CLAUDE.md").read_text(encoding="utf-8")
        # Template contains these literals after substitution
        assert "TestCo" in content
        assert "https://test.example.com" in content
        assert "30 minutes" in content
        # Ensure placeholders are gone
        assert "{instance_name}" not in content
        assert "{server_url}" not in content
        assert "{sync_interval}" not in content


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
