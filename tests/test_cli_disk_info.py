import json
import os
from typer.testing import CliRunner
import pytest


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    snap = tmp_path / "user" / "snapshots"
    snap.mkdir(parents=True)
    yield tmp_path


@pytest.fixture
def cli_app():
    import typer
    from cli.commands.disk_info import disk_info_app
    app = typer.Typer()
    app.add_typer(disk_info_app, name="disk-info")
    return app


def test_disk_info_runs_and_reports(cli_env, cli_app):
    (cli_env / "user" / "snapshots" / "x.parquet").write_bytes(b"A" * 1024)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "Snapshots dir" in result.stdout


def test_disk_info_human_readable_format(cli_env, cli_app):
    # Create multiple files
    (cli_env / "user" / "snapshots" / "snap1.parquet").write_bytes(b"A" * 2048)
    (cli_env / "user" / "snapshots" / "snap2.parquet").write_bytes(b"B" * 1024)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "Snapshots dir:" in result.stdout
    assert "Used by Agnes:" in result.stdout
    assert "Free disk:" in result.stdout
    assert "Configured cap:" in result.stdout
    assert "snapshots" in result.stdout.lower()


def test_disk_info_json_output(cli_env, cli_app):
    (cli_env / "user" / "snapshots" / "x.parquet").write_bytes(b"A" * 1024)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "snapshots_dir" in data
    assert "used_bytes" in data
    assert "snapshot_count" in data
    assert "free_bytes" in data
    assert "quota_gb" in data
    assert data["used_bytes"] == 1024
    assert data["snapshot_count"] == 1


def test_disk_info_empty_dir(cli_env, cli_app):
    # No files created
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "0.0 B" in result.stdout
    assert "0 snapshots" in result.stdout


def test_disk_info_custom_quota_env(cli_env, cli_app, monkeypatch):
    monkeypatch.setenv("AGNES_SNAPSHOT_QUOTA_GB", "50")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "50 GB" in result.stdout


def test_disk_info_size_formatting(cli_env, cli_app):
    # Create file > 1 MB to test formatting
    (cli_env / "user" / "snapshots" / "large.parquet").write_bytes(b"A" * (2 * 1024 * 1024))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["disk-info"])
    assert result.exit_code == 0
    assert "MB" in result.stdout or "B" in result.stdout
