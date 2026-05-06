"""Tests for cli/lib/hooks.py:install_claude_hooks."""

import json
from pathlib import Path


from cli.lib.hooks import install_claude_hooks


def _read_settings(workspace: Path) -> dict:
    return json.loads((workspace / ".claude" / "settings.json").read_text())


def _commands_for(cfg: dict, event: str) -> list[str]:
    """Flatten the per-event command list — each entry has a list of hooks,
    each hook has a `command` field. We treat each entry as one command for
    assertion purposes (matches the install_claude_hooks contract: one
    entry per command)."""
    return [
        entry["hooks"][0]["command"]
        for entry in cfg["hooks"].get(event, [])
        if entry.get("hooks")
    ]


def test_install_creates_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    # SessionStart now has TWO entries: the data pull and the marketplace
    # refresh. They run as independent hook entries so a failure in one
    # (e.g. refresh-marketplace on a workspace that never cloned the
    # marketplace) doesn't prevent the other.
    assert len(starts) == 2
    assert any("agnes pull --quiet" in c for c in starts)
    assert any("agnes refresh-marketplace --quiet" in c for c in starts)
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    assert "agnes push --quiet" in ends[0]


def test_install_idempotent(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # Two SessionStart entries (pull + refresh-marketplace), one SessionEnd
    # entry (push). Re-install must NOT duplicate them.
    assert len(cfg["hooks"]["SessionStart"]) == 2
    assert len(cfg["hooks"]["SessionEnd"]) == 1


def test_install_replaces_old_da_sync_entries(tmp_path):
    """Hook from a pre-rewrite workspace gets replaced cleanly — legacy
    `da sync` entries are removed, both new agnes hooks land in their place."""
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
    starts = _commands_for(cfg, "SessionStart")
    assert len(starts) == 2
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    # Legacy command must be gone from BOTH starts.
    assert not any("da sync" in c for c in starts)


def test_install_replaces_prior_single_pull_entry(tmp_path):
    """Workspaces bootstrapped by a CLI version that only installed a
    single SessionStart entry (`agnes pull`, no refresh-marketplace) must
    upgrade to the two-entry layout on the next install — not end up with
    three entries (one old + two new)."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "agnes pull --quiet 2>/dev/null || true"}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    assert len(starts) == 2
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)


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
    starts = _commands_for(cfg, "SessionStart")
    # Third-party entry stays + both agnes entries get added.
    assert len(starts) == 3
    assert any("echo hi from another tool" in c for c in starts)
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    # Other event types untouched.
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
