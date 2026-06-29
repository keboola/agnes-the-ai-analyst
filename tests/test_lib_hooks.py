"""Tests for cli/lib/hooks.py:install_claude_hooks.

Current layout: SessionStart has two Agnes entries — the chained
``agnes self-upgrade; agnes pull`` and the standalone
``agnes refresh-marketplace --check``. SessionEnd has one — the detached
``agnes push``. There is no ``agnes capture-session`` entry (push scans the
session folder directly now); the capture markers survive only so old
capture entries get stripped on the next install/refresh.
"""

import json
from pathlib import Path


from cli.lib.hooks import (
    install_claude_hooks,
    maybe_refresh_claude_hooks,
    workspace_has_agnes_hooks,
    workspace_has_legacy_hooks,
)


def _read_settings(workspace: Path) -> dict:
    return json.loads((workspace / ".claude" / "settings.json").read_text())


def _commands_for(cfg: dict, event: str) -> list[str]:
    return [
        entry["hooks"][0]["command"]
        for entry in cfg["hooks"].get(event, [])
        if entry.get("hooks")
    ]


def test_install_creates_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    # Two entries: (1) chained self-upgrade ; pull — self-upgrade first so a
    # wire-protocol bump lands before pull uses the new CLI; (2)
    # refresh-marketplace as a separate entry so its failure (e.g. no clone)
    # doesn't suppress the data pull.
    assert len(starts) == 2
    # No capture-session, and no push, in SessionStart.
    assert not any("capture-session" in c for c in starts), starts
    assert not any("agnes push" in c for c in starts), starts
    chain = next(
        (c for c in starts if "agnes self-upgrade" in c and "agnes pull" in c),
        None,
    )
    assert chain is not None, "Expected one SessionStart entry chaining self-upgrade and pull"
    assert "agnes self-upgrade --quiet" in chain
    assert "agnes pull --quiet" in chain
    refresh = next((c for c in starts if "agnes refresh-marketplace" in c), None)
    assert refresh is not None
    assert refresh.startswith("bash -c "), refresh
    assert "--check" in refresh
    assert "--quiet" not in refresh
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    assert "agnes push --quiet" in ends[0]


def test_session_end_has_no_capture_prefix(tmp_path):
    """SessionEnd is just the detached push — no `agnes capture-session`
    prefix, since push now scans the session folder directly."""
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    cmd = ends[0]
    assert "capture-session" not in cmd, cmd
    assert "nohup agnes push" in cmd


def test_all_installed_hooks_are_bash_wrapped(tmp_path):
    """Every Agnes-managed SessionStart / SessionEnd entry must be wrapped in
    `bash -c "..."` — Claude Code on Windows runs hook commands directly (no
    shell), so unwrapped `;`/`||`/redirection syntax fails silently."""
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    for cmd in _commands_for(cfg, "SessionStart") + _commands_for(cfg, "SessionEnd"):
        assert cmd.startswith("bash -c "), cmd


def test_install_idempotent(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert len(cfg["hooks"]["SessionStart"]) == 2
    assert len(cfg["hooks"]["SessionEnd"]) == 1


def test_install_replaces_old_da_sync_entries(tmp_path):
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
    assert not any("agnes push" in c for c in starts)
    assert not any("da sync" in c for c in starts)


def test_install_removes_legacy_capture_session_entries(tmp_path):
    """A workspace seeded by an older CLI (CLI-form `agnes capture-session`)
    or by the template (`bash .claude/hooks/capture-session/capture.sh`) must
    have BOTH capture entries stripped on the next install — not left shelling
    out to a deleted helper."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "bash .claude/hooks/capture-session/capture.sh"}]},
                {"hooks": [{"type": "command", "command": 'bash -c "agnes capture-session 2>/dev/null || true"'}]},
                {"hooks": [{"type": "command", "command": (
                    "agnes self-upgrade --quiet 2>/dev/null || true; "
                    "agnes pull --quiet 2>/dev/null || true"
                )}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": (
                    'bash -c "agnes capture-session 2>/dev/null || true ; '
                    '( nohup agnes push --quiet </dev/null >/dev/null 2>&1 & ) ; true"'
                )}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    ends = _commands_for(cfg, "SessionEnd")
    assert len(starts) == 2, starts
    assert not any("capture-session" in c for c in starts), starts
    assert len(ends) == 1
    assert "capture-session" not in ends[0], ends
    assert "nohup agnes push" in ends[0]


def test_install_replaces_prior_single_pull_entry(tmp_path):
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
    assert not any("agnes push" in c for c in starts)


def test_install_replaces_old_quiet_refresh_with_check(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": (
                    "agnes self-upgrade --quiet 2>/dev/null || true; "
                    "agnes pull --quiet 2>/dev/null || true"
                )}]},
                {"hooks": [{"type": "command", "command": (
                    'bash -c "agnes refresh-marketplace --quiet 2>/dev/null || true"'
                )}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    refresh_entries = [c for c in starts if "agnes refresh-marketplace" in c]
    assert len(refresh_entries) == 1, refresh_entries
    refresh = refresh_entries[0]
    assert "--check" in refresh
    assert "--quiet" not in refresh


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
    assert not any("agnes push" in c for c in starts)
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


def test_install_chains_self_upgrade_then_pull_in_one_entry(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    chain = next(
        (c for c in starts if "agnes self-upgrade" in c and "agnes pull" in c),
        None,
    )
    assert chain is not None, starts
    assert "agnes self-upgrade --quiet" in chain
    assert "agnes pull --quiet" in chain
    assert chain.index("agnes self-upgrade") < chain.index("agnes pull")
    assert chain.count("|| true") >= 2


def test_session_end_push_is_detached(tmp_path):
    """Regression test for the headless-mode SIGTERM bug — the SessionEnd
    push must run detached so the upload child survives the hook subprocess
    being torn down ~1s after launch in `-p` mode."""
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    cmd = ends[0]
    assert "agnes push" in cmd
    assert "nohup" in cmd
    assert "&" in cmd
    assert "</dev/null" in cmd
    assert ">/dev/null 2>&1" in cmd
    assert cmd.startswith("bash -c "), cmd


def test_install_writes_statusline_when_absent(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert "statusLine" in cfg
    assert cfg["statusLine"]["type"] == "command"
    assert "agnes statusline" in cfg["statusLine"]["command"]


def test_install_preserves_existing_user_statusline(tmp_path, capsys):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    user_statusline = {"type": "command", "command": "my-custom-status"}
    settings_path.write_text(json.dumps({"statusLine": user_statusline}))

    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["statusLine"] == user_statusline
    captured = capsys.readouterr()
    assert "statusLine" in captured.err


def test_install_idempotent_when_statusline_already_ours(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert "agnes statusline" in cfg["statusLine"]["command"]


def test_install_treats_explicit_null_statusline_as_unconfigured(tmp_path, capsys):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"statusLine": None}))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert isinstance(cfg["statusLine"], dict)
    assert "agnes statusline" in cfg["statusLine"]["command"]
    captured = capsys.readouterr()
    assert "preserved" not in captured.err


def test_install_treats_empty_statusline_as_unconfigured(tmp_path, capsys):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"statusLine": ""}))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert isinstance(cfg["statusLine"], dict)
    assert "agnes statusline" in cfg["statusLine"]["command"]
    captured = capsys.readouterr()
    assert "preserved" not in captured.err


def test_install_replaces_old_synchronous_session_end_push(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": "agnes push --quiet 2>/dev/null || true"}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1, ends
    assert "nohup" in ends[0], ends


# ---------------------------------------------------------------------------
# workspace_has_agnes_hooks / maybe_refresh_claude_hooks
# ---------------------------------------------------------------------------


def test_workspace_has_agnes_hooks_false_for_missing_settings(tmp_path):
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_workspace_has_agnes_hooks_false_for_empty_settings(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({}), encoding="utf-8")
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_workspace_has_agnes_hooks_false_for_invalid_json(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text("not json", encoding="utf-8")
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_workspace_has_agnes_hooks_false_for_third_party_only_hook(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hello"}]},
            ],
        }
    }), encoding="utf-8")
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_workspace_has_agnes_hooks_true_for_agnes_hook(tmp_path):
    install_claude_hooks(tmp_path)
    assert workspace_has_agnes_hooks(tmp_path) is True


def test_workspace_has_agnes_hooks_true_for_just_statusline(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "agnes statusline"},
    }), encoding="utf-8")
    assert workspace_has_agnes_hooks(tmp_path) is True


def test_maybe_refresh_noop_in_non_agnes_directory(tmp_path):
    """Critical safety: `agnes self-upgrade` invoked from a non-Agnes dir
    (e.g. ~/) must NOT create `.claude/settings.json` there."""
    refreshed = maybe_refresh_claude_hooks(tmp_path)
    assert refreshed is False
    assert not (tmp_path / ".claude").exists()


def test_maybe_refresh_migrates_capture_session_workspace(tmp_path):
    """Simulate a workspace seeded with the old capture-session layout
    (CLI-form capture in SessionStart, capture-prefixed SessionEnd, old
    refresh --quiet, no statusLine) and assert maybe_refresh brings it to the
    current scan-based layout: capture entries gone, refresh on --check,
    detached push, statusLine installed."""
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": 'bash -c "agnes capture-session 2>/dev/null || true"'}]},
                {"hooks": [{"type": "command",
                            "command": "agnes self-upgrade --quiet 2>/dev/null || true; "
                                       "agnes pull --quiet 2>/dev/null || true"}]},
                {"hooks": [{"type": "command",
                            "command": 'bash -c "agnes refresh-marketplace --quiet 2>/dev/null || true"'}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command",
                            "command": 'bash -c "agnes capture-session 2>/dev/null || true ; '
                                       '( nohup agnes push --quiet </dev/null >/dev/null 2>&1 & ) ; true"'}]},
            ],
        }
    }), encoding="utf-8")

    refreshed = maybe_refresh_claude_hooks(tmp_path)
    assert refreshed is True

    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    assert not any("capture-session" in c for c in starts), starts
    refresh = next((c for c in starts if "agnes refresh-marketplace" in c), None)
    assert refresh is not None and "--check" in refresh, refresh
    ends = _commands_for(cfg, "SessionEnd")
    assert all("capture-session" not in c for c in ends), ends
    assert any("nohup" in c for c in ends), ends
    assert cfg.get("statusLine", {}).get("command", "").startswith("agnes statusline")


def test_maybe_refresh_preserves_third_party_hooks(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "agnes self-upgrade --quiet || true"}]},
                {"hooks": [{"type": "command", "command": "echo hi from another tool"}]},
            ],
        }
    }), encoding="utf-8")
    refreshed = maybe_refresh_claude_hooks(tmp_path)
    assert refreshed is True
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    assert "echo hi from another tool" in starts, starts


# ---------------------------------------------------------------------------
# workspace_has_legacy_hooks — old server-flow detection (#478)
# ---------------------------------------------------------------------------


def _write_legacy_collect_session_settings(workspace: Path) -> None:
    sp = workspace / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [
                {"hooks": [{"type": "command",
                            "command": "python server/scripts/collect_session.py"}]},
            ],
        }
    }), encoding="utf-8")


def test_legacy_hooks_true_for_collect_session_workspace(tmp_path):
    _write_legacy_collect_session_settings(tmp_path)
    assert workspace_has_legacy_hooks(tmp_path) is True
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_legacy_hooks_true_for_server_scripts_session_start(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": "bash server/scripts/sync_session.sh"}]},
            ],
        }
    }), encoding="utf-8")
    assert workspace_has_legacy_hooks(tmp_path) is True
    assert workspace_has_agnes_hooks(tmp_path) is False


def test_legacy_hooks_false_for_modern_workspace(tmp_path):
    install_claude_hooks(tmp_path)
    assert workspace_has_agnes_hooks(tmp_path) is True
    assert workspace_has_legacy_hooks(tmp_path) is False


def test_legacy_hooks_false_for_mixed_workspace_with_agnes_hooks(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    cfg["hooks"].setdefault("SessionEnd", []).append(
        {"hooks": [{"type": "command",
                    "command": "python server/scripts/collect_session.py"}]}
    )
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(cfg), encoding="utf-8"
    )
    assert workspace_has_agnes_hooks(tmp_path) is True
    assert workspace_has_legacy_hooks(tmp_path) is False


def test_legacy_hooks_false_for_missing_settings(tmp_path):
    assert workspace_has_legacy_hooks(tmp_path) is False


def test_legacy_hooks_false_for_invalid_json(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text("not json {", encoding="utf-8")
    assert workspace_has_legacy_hooks(tmp_path) is False


def test_legacy_hooks_false_for_third_party_only(tmp_path):
    sp = tmp_path / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hello"}]},
            ],
        }
    }), encoding="utf-8")
    assert workspace_has_legacy_hooks(tmp_path) is False
