"""Tests for cli/lib/hooks.py:install_claude_hooks."""

import json
from pathlib import Path

import pytest

from cli.lib.hooks import install_claude_hooks


def _read_settings(workspace: Path) -> dict:
    return json.loads((workspace / ".claude" / "settings.json").read_text())


def test_install_creates_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["hooks"]["SessionStart"]
    assert "agnes pull --quiet" in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cfg["hooks"]["SessionEnd"]
    assert "agnes push --quiet" in cfg["hooks"]["SessionEnd"][0]["hooks"][0]["command"]


def test_install_idempotent(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert len(cfg["hooks"]["SessionStart"]) == 1
    assert len(cfg["hooks"]["SessionEnd"]) == 1


def test_install_replaces_old_da_sync_entries(tmp_path):
    """Hook from a pre-rewrite workspace gets replaced cleanly."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "da sync --quiet"}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "da sync --upload-only --quiet"}]}],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert len(cfg["hooks"]["SessionStart"]) == 1
    assert "agnes pull" in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "da sync" not in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]


def test_install_preserves_third_party_hooks(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi from another tool"}]}],
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = cfg["hooks"]["SessionStart"]
    assert any("echo hi from another tool" in s["hooks"][0]["command"] for s in starts)
    assert any("agnes pull" in s["hooks"][0]["command"] for s in starts)
    assert cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo pre"


def test_install_handles_missing_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_install_handles_invalid_json(tmp_path, capsys):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not valid json {")
    install_claude_hooks(tmp_path)
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err or "warning" in captured.err.lower()
