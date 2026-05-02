"""`_install_claude_hooks` writes SessionStart/End hooks idempotently into
a Claude settings file (workspace-level for analyst workspaces)."""
import json
from pathlib import Path

from cli.commands.analyst import _install_claude_hooks


def test_install_creates_settings_when_missing(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _install_claude_hooks(settings)

    cfg = json.loads(settings.read_text())
    starts = cfg["hooks"]["SessionStart"]
    cmds = [h["command"] for e in starts for h in e["hooks"]]
    assert any("da sync --quiet" in c and "--upload-only" not in c for c in cmds), cmds

    ends = cfg["hooks"]["SessionEnd"]
    end_cmds = [h["command"] for e in ends for h in e["hooks"]]
    assert any("da sync --upload-only" in c for c in end_cmds), end_cmds


def test_install_preserves_existing_unrelated_hooks(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        },
        "permissions": {"allow": ["Bash(git status:*)"]},
        "model": "sonnet",
    }))

    _install_claude_hooks(settings)

    cfg = json.loads(settings.read_text())
    # Unrelated hook event preserved
    assert cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo hi"
    # Unrelated top-level keys preserved
    assert cfg["permissions"]["allow"] == ["Bash(git status:*)"]
    assert cfg["model"] == "sonnet"
    # Our new hooks added
    assert "SessionStart" in cfg["hooks"]
    assert "SessionEnd" in cfg["hooks"]


def test_install_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _install_claude_hooks(settings)
    first = json.loads(settings.read_text())
    _install_claude_hooks(settings)
    second = json.loads(settings.read_text())
    # No duplicate entries
    assert first["hooks"]["SessionStart"] == second["hooks"]["SessionStart"]
    assert first["hooks"]["SessionEnd"] == second["hooks"]["SessionEnd"]
    assert len(second["hooks"]["SessionStart"]) == 1
    assert len(second["hooks"]["SessionEnd"]) == 1


def test_install_replaces_old_da_sync_entry_without_duplicating(tmp_path):
    """If the user already has a `da sync` entry from a prior version, our
    install replaces it cleanly rather than appending a second copy."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "da sync"}]},  # old shape
                {"hooks": [{"type": "command", "command": "echo not-ours"}]},
            ]
        }
    }))

    _install_claude_hooks(settings)

    cfg = json.loads(settings.read_text())
    starts = cfg["hooks"]["SessionStart"]
    assert len(starts) == 2  # one ours, one third-party
    cmds = [h["command"] for e in starts for h in e["hooks"]]
    assert "da sync --quiet 2>/dev/null || true" in cmds
    assert "echo not-ours" in cmds
    assert all(c == "echo not-ours" or "da sync --quiet" in c for c in cmds), cmds


def test_install_skips_malformed_existing_settings(tmp_path, capsys):
    """If the settings file is corrupted JSON, warn on stderr and bail —
    don't crash the surrounding `da analyst setup` flow."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not valid json")

    _install_claude_hooks(settings)  # must not raise

    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err
    # File untouched
    assert settings.read_text() == "{not valid json"
