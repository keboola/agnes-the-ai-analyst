"""Tests for da server subcommands (delegate to subprocess)."""

import subprocess
import pytest
from unittest.mock import patch, MagicMock, call

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


def _subprocess_result(returncode=0):
    r = MagicMock()
    r.returncode = returncode
    return r


class TestServerStatus:
    def test_server_status_runs_docker_compose_ps(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "status"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "docker compose ps" in cmd

    def test_server_status_nonzero_exit(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(1)):
            result = runner.invoke(app, ["server", "status"])
        assert result.exit_code != 0


class TestServerLogs:
    def test_server_logs_default_service(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "logs"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "docker compose logs" in cmd
        assert "app" in cmd

    def test_server_logs_custom_service(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "logs", "scheduler"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "scheduler" in cmd

    def test_server_logs_with_tail(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "logs", "--tail", "50"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "50" in cmd


class TestServerRestart:
    def test_server_restart_default_service(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "restart"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "docker compose restart" in cmd
        assert "app" in cmd
        assert "Restarted" in result.output

    def test_server_restart_named_service(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "restart", "scheduler"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "scheduler" in cmd


class TestServerDeploy:
    def test_server_deploy_production(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "deploy"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "kamal deploy" in cmd

    def test_server_deploy_staging(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "deploy", "--staging"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "staging" in cmd


class TestServerRollback:
    def test_server_rollback(self):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "rollback"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "rollback" in cmd


class TestServerBackup:
    def test_server_backup(self, tmp_path):
        with patch("cli.commands.server.subprocess.run", return_value=_subprocess_result(0)) as mock_run:
            result = runner.invoke(app, ["server", "backup", "--output", str(tmp_path)])
        assert result.exit_code == 0
        assert "Backup saved" in result.output
        cmd = mock_run.call_args[0][0]
        assert "docker compose cp" in cmd
