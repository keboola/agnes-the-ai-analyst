"""Tests for cli/lib/statusline.py:install_claude_statusline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.lib.statusline import (
    install_claude_statusline,
    statusline_template_for_tests,
    uninstall_claude_statusline,
)


def _read_settings(workspace: Path) -> dict:
    return json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_install_creates_script_and_settings(tmp_path):
    """Fresh workspace → script materialized + settings.json `statusLine`
    points at it. Both artifacts under <workspace>/.claude/ (workspace-
    scoped, mirrors install_claude_hooks layout)."""
    install_claude_statusline(tmp_path)

    script_path = tmp_path / ".claude" / "agnes-statusline.sh"
    assert script_path.is_file(), "script must be materialized"

    cfg = _read_settings(tmp_path)
    assert "statusLine" in cfg
    assert cfg["statusLine"]["type"] == "command"
    assert cfg["statusLine"]["command"] == str(script_path)


def test_install_script_content_matches_packaged_template(tmp_path):
    """The materialized script must be byte-identical to the wheel
    template (so agnes binary version always matches the script
    contract — auto-hide TTL, status file path, etc.). Pin via the
    `statusline_template_for_tests` helper."""
    install_claude_statusline(tmp_path)
    template = statusline_template_for_tests()
    assert template is not None, "wheel did not bundle the statusline template"
    on_disk = (tmp_path / ".claude" / "agnes-statusline.sh").read_text(encoding="utf-8")
    assert on_disk == template


def test_install_idempotent(tmp_path):
    """Re-running install on the same workspace must not duplicate the
    statusLine entry, must not overwrite a hand-edited script body, and
    must not break settings.json structure."""
    install_claude_statusline(tmp_path)
    script_path = tmp_path / ".claude" / "agnes-statusline.sh"
    # Hand-edit: operator added a comment.
    edited = script_path.read_text(encoding="utf-8") + "\n# operator-added comment\n"
    script_path.write_text(edited, encoding="utf-8")

    install_claude_statusline(tmp_path)

    # Hand-edit preserved (we only write the script when absent).
    assert script_path.read_text(encoding="utf-8") == edited
    # Single statusLine block, still pointing at our script.
    cfg = _read_settings(tmp_path)
    assert cfg["statusLine"]["command"] == str(script_path)


def test_install_does_not_overwrite_foreign_statusline(tmp_path, capsys):
    """If the operator already configured a custom statusLine pointing
    at a non-agnes script, agnes init must NOT overwrite it. Warning to
    stderr explains how to surface agnes notifications via the operator's
    own statusline."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "/some/operator-custom-statusline.sh"},
    }), encoding="utf-8")

    install_claude_statusline(tmp_path)

    cfg = _read_settings(tmp_path)
    assert cfg["statusLine"]["command"] == "/some/operator-custom-statusline.sh"
    captured = capsys.readouterr()
    assert "custom statusLine" in captured.err
    assert "refresh.status" in captured.err


def test_install_re_affirms_existing_agnes_statusline(tmp_path):
    """If statusLine already points at our script (re-init scenario,
    workspace path moved, etc.), install re-writes the path to the
    canonical location instead of leaving stale paths in settings."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "statusLine": {
            "type": "command",
            "command": "/old/path/to/agnes-statusline.sh",  # stale but recognizable
        },
    }), encoding="utf-8")

    install_claude_statusline(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["statusLine"]["command"] == str(tmp_path / ".claude" / "agnes-statusline.sh")


def test_install_preserves_existing_settings_keys(tmp_path):
    """Other top-level keys in settings.json (model, permissions, hooks,
    etc.) must survive install_claude_statusline untouched."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "model": "sonnet",
        "permissions": {"allow": ["Read", "Bash"]},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
    }), encoding="utf-8")

    install_claude_statusline(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["model"] == "sonnet"
    assert cfg["permissions"]["allow"] == ["Read", "Bash"]
    assert cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert "statusLine" in cfg


def test_install_handles_invalid_settings_json(tmp_path, capsys):
    """Malformed settings.json → warn + skip (don't crash agnes init).
    Same shape as install_claude_hooks for a foreign-bad settings file."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{ not valid json", encoding="utf-8")

    install_claude_statusline(tmp_path)

    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err or "skipping statusline install" in captured.err
    # Script still gets materialized — that part doesn't depend on settings.json.
    assert (tmp_path / ".claude" / "agnes-statusline.sh").is_file()


def test_uninstall_removes_script_and_strips_statusline(tmp_path):
    """`uninstall_claude_statusline` reverses `install_claude_statusline`
    only when statusLine points at our script — leaves foreign
    statusLines untouched. Used by tests + as the workspace-scoped
    cleanup primitive (the bash reset script handles user-home-only)."""
    install_claude_statusline(tmp_path)
    assert (tmp_path / ".claude" / "agnes-statusline.sh").is_file()

    uninstall_claude_statusline(tmp_path)
    assert not (tmp_path / ".claude" / "agnes-statusline.sh").exists()
    cfg = _read_settings(tmp_path)
    assert "statusLine" not in cfg


def test_uninstall_leaves_foreign_statusline_alone(tmp_path):
    """If the operator's own statusLine ended up in settings.json (e.g.
    install was previously skipped via the foreign-statusline branch),
    uninstall must NOT remove it."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "/operator/their-statusline.sh"},
    }), encoding="utf-8")
    uninstall_claude_statusline(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["statusLine"]["command"] == "/operator/their-statusline.sh"


def test_template_contains_required_invariants():
    """The bash template must reference ~/.agnes/refresh.status, must
    apply an age-out (default 1800 s = 30 min, configurable via
    AGNES_STATUS_TTL_S), and must drain stdin so Claude Code doesn't
    block on the write side. Pin all three so a future template rewrite
    can't accidentally drop one of them."""
    t = statusline_template_for_tests()
    assert t is not None
    assert "$HOME/.agnes/refresh.status" in t
    assert "AGNES_STATUS_TTL_S" in t
    assert "1800" in t  # default 30 min
    assert "cat > /dev/null" in t  # drain stdin
