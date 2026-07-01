"""Tests for `agnes config set-server` — merge-safe server URL write."""

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def test_set_server_creates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["config", "set-server", "https://s.example.com"])
    assert result.exit_code == 0
    from cli.config import get_server_url
    assert get_server_url() == "https://s.example.com"


def test_set_server_preserves_existing_keys(tmp_path, monkeypatch):
    """Setting the server URL must NOT drop other config keys (workspace_root)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli.config import load_config, save_config, set_workspace_root

    set_workspace_root("/home/me/ws")
    save_config({"server": "https://old.example.com"})

    result = runner.invoke(app, ["config", "set-server", "https://new.example.com"])
    assert result.exit_code == 0

    cfg = load_config()
    assert cfg["server"] == "https://new.example.com"
    assert cfg["workspace_root"] == "/home/me/ws"  # preserved, not clobbered
