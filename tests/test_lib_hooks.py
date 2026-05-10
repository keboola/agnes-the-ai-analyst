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
    # SessionStart has three entries: (1) capture-session as the very first
    # so the hook stdin (transcript_path) is appended to the queue before
    # any other hook runs; (2) chained self-upgrade ; pull — self-upgrade
    # runs first so a wire-protocol bump lands before pull tries to use
    # the new CLI; (3) refresh-marketplace as a separate entry so a
    # failure (e.g. fresh workspace with no clone) doesn't suppress the
    # data pull above.
    #
    # `agnes push` is NOT in SessionStart — the queue mechanism handles
    # orphans on the next SessionEnd, so the old self-heal entry was
    # redundant + would re-upload the just-starting (empty) session.
    assert len(starts) == 3
    capture = next((c for c in starts if "agnes capture-session" in c), None)
    assert capture is not None, "Expected SessionStart capture-session entry"
    assert capture.startswith("bash -c "), (
        f"capture-session hook must be wrapped in bash -c for Windows; got: {capture!r}"
    )
    assert not any("agnes push" in c for c in starts), (
        f"agnes push must NOT be in SessionStart; got: {starts!r}"
    )
    chain = next(
        (c for c in starts if "agnes self-upgrade" in c and "agnes pull" in c),
        None,
    )
    assert chain is not None, (
        "Expected one SessionStart entry chaining self-upgrade and pull"
    )
    assert "agnes self-upgrade --quiet" in chain
    assert "agnes pull --quiet" in chain
    # The refresh-marketplace command is wrapped in `bash -c "..."` so the
    # `2>/dev/null || true` shell syntax is interpreted on Windows, where
    # Claude Code runs hook commands directly without invoking a shell.
    refresh = next((c for c in starts if "agnes refresh-marketplace" in c), None)
    assert refresh is not None
    assert refresh.startswith("bash -c "), (
        f"refresh-marketplace hook must be wrapped in bash -c for Windows; got: {refresh!r}"
    )
    # Hook is now a detector — `--check` only. Plugin install/update
    # happens in the `/update-agnes-plugins` slash command instead.
    # Pinning the flag here prevents an accidental regression to the old
    # `--quiet` form (which performed a full reconcile silently).
    assert "--check" in refresh, (
        f"refresh-marketplace hook must use --check (detector mode); got: {refresh!r}"
    )
    assert "--quiet" not in refresh, (
        f"refresh-marketplace hook must NOT use --quiet (removed flag); got: {refresh!r}"
    )
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    assert "agnes push --quiet" in ends[0]


def test_install_idempotent(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # Three SessionStart entries (capture-session + chained self-upgrade/pull
    # + refresh-marketplace), one SessionEnd entry (push). Re-install must
    # NOT duplicate them.
    assert len(cfg["hooks"]["SessionStart"]) == 3
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
    assert len(starts) == 3
    assert any("agnes capture-session" in c for c in starts)
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    # `agnes push` lives only in SessionEnd now.
    assert not any("agnes push" in c for c in starts)
    # Legacy command must be gone from BOTH starts.
    assert not any("da sync" in c for c in starts)


def test_install_replaces_prior_single_pull_entry(tmp_path):
    """Workspaces bootstrapped by a CLI version that only installed a
    single SessionStart entry (`agnes pull`, no refresh-marketplace) must
    upgrade to the three-entry layout on the next install — not end up
    stacking the new entries on top of the old one."""
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
    assert len(starts) == 3
    assert any("agnes capture-session" in c for c in starts)
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    assert not any("agnes push" in c for c in starts)


def test_install_replaces_v0_43_chained_self_upgrade_pull_entry(tmp_path):
    """Workspaces bootstrapped on v0.43.0 had a single SessionStart entry
    chaining `agnes self-upgrade; agnes pull` in one shell line. Upgrading
    those workspaces to v0.44.0+ must collapse that entry and re-install
    the new two-entry layout — not stack the v0.44 entries on top of the
    v0.43 chained one (which would re-run self-upgrade twice on every
    session and leave the old format around forever).
    """
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": (
                    "agnes self-upgrade --quiet 2>/dev/null || true; "
                    "agnes pull --quiet 2>/dev/null || true"
                )}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": "agnes push --quiet 2>/dev/null || true"}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    # Exactly three entries — the v0.43 chained line was replaced, not stacked.
    assert len(starts) == 3, starts
    chain = next(
        (c for c in starts if "agnes self-upgrade" in c and "agnes pull" in c),
        None,
    )
    assert chain is not None
    assert any("agnes capture-session" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    assert not any("agnes push" in c for c in starts)
    # SessionEnd untouched (single push entry).
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    assert "agnes push --quiet" in ends[0]


def test_install_replaces_old_quiet_refresh_with_check(tmp_path):
    """A workspace bootstrapped before the slash-command split has the old
    `--quiet` form in its refresh-marketplace SessionStart entry. The next
    `agnes init` must replace that entry with the new `--check` form, NOT
    stack the new entry alongside the old one (which would re-run the
    full reconcile every session — exactly the behaviour we just moved
    behind the slash command).
    """
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
                {"hooks": [{"type": "command", "command": (
                    'bash -c "agnes push --quiet 2>/dev/null || true"'
                )}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": (
                    'bash -c "( nohup agnes push --quiet </dev/null '
                    '>/dev/null 2>&1 & ) ; true"'
                )}]},
            ],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    # Exactly one refresh-marketplace entry remains (no stacking).
    refresh_entries = [c for c in starts if "agnes refresh-marketplace" in c]
    assert len(refresh_entries) == 1, refresh_entries
    refresh = refresh_entries[0]
    assert "--check" in refresh, (
        f"old --quiet entry must have been rewritten to --check; got: {refresh!r}"
    )
    assert "--quiet" not in refresh, (
        f"old --quiet form must be gone after re-init; got: {refresh!r}"
    )


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
    # Third-party entry stays + all three agnes entries get added.
    assert len(starts) == 4
    assert any("echo hi from another tool" in c for c in starts)
    assert any("agnes capture-session" in c for c in starts)
    assert any("agnes pull" in c for c in starts)
    assert any("agnes refresh-marketplace" in c for c in starts)
    assert not any("agnes push" in c for c in starts)
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


def test_install_chains_self_upgrade_then_pull_in_one_entry(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    starts = _commands_for(cfg, "SessionStart")
    # SessionStart has two entries: the chain (self-upgrade + pull) and
    # the standalone refresh-marketplace. This test pins the chain
    # invariant — order, both `|| true`-guarded — independent of the
    # refresh-marketplace entry being present.
    chain = next(
        (c for c in starts if "agnes self-upgrade" in c and "agnes pull" in c),
        None,
    )
    assert chain is not None, starts
    assert "agnes self-upgrade --quiet" in chain
    assert "agnes pull --quiet" in chain
    # Order is encoded in the shell — self-upgrade must appear first
    assert chain.index("agnes self-upgrade") < chain.index("agnes pull")
    # Both segments carry || true so neither failure aborts the line
    assert chain.count("|| true") >= 2


def test_install_idempotent_chained_entry(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # Three SessionStart entries (capture-session, chained self-upgrade+pull,
    # refresh-marketplace) — re-install must not duplicate any of them.
    assert len(cfg["hooks"]["SessionStart"]) == 3
    assert len(cfg["hooks"]["SessionEnd"]) == 1


def test_session_end_push_is_detached(tmp_path):
    """Regression test for the headless-mode SIGTERM bug.

    Claude Code in `-p` (headless) mode SIGTERMs SessionEnd hook
    subprocesses ~1s after launch, regardless of whether the hook is
    still working. `agnes push` for a typical workspace (10 session
    JSONLs) takes 5-30s, so a synchronous form gets killed mid-first-
    upload and most files never reach the server. The hook MUST run
    detached so the upload child survives the hook subprocess being
    torn down.

    This test pins the wrapper shape — `bash -c "( nohup ... & ) ; true"` —
    so a future refactor that re-introduces the synchronous form fails
    loudly here instead of silently regressing in production.
    """
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    ends = _commands_for(cfg, "SessionEnd")
    assert len(ends) == 1
    cmd = ends[0]
    assert "agnes push" in cmd, f"SessionEnd must still call agnes push; got: {cmd!r}"
    # Detachment markers — every one of these is load-bearing:
    # - `nohup` ignores SIGHUP if the controlling terminal disappears
    # - `&` backgrounds the child inside the subshell
    # - `</dev/null` decouples stdin so the parent doesn't wait on a pipe
    # - `>/dev/null 2>&1` decouples stdout/stderr likewise
    assert "nohup" in cmd, f"SessionEnd push must use nohup for detachment; got: {cmd!r}"
    assert "&" in cmd, f"SessionEnd push must background with &; got: {cmd!r}"
    assert "</dev/null" in cmd, (
        f"SessionEnd push must redirect stdin from /dev/null; got: {cmd!r}"
    )
    assert ">/dev/null 2>&1" in cmd, (
        f"SessionEnd push must redirect stdout/stderr to /dev/null; got: {cmd!r}"
    )
    # `bash -c` wrapping is required because Claude Code on Windows runs
    # hook commands directly (no shell), so the subshell + redirection
    # syntax wouldn't parse otherwise.
    assert cmd.startswith("bash -c "), (
        f"SessionEnd push must be wrapped in bash -c for Windows; got: {cmd!r}"
    )


def test_install_writes_statusline_when_absent(tmp_path):
    """Greenfield install: no prior statusLine → we write ours."""
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert "statusLine" in cfg
    assert cfg["statusLine"]["type"] == "command"
    assert "agnes statusline" in cfg["statusLine"]["command"]


def test_install_preserves_existing_user_statusline(tmp_path, capsys):
    """User has their own statusLine — we leave it alone and warn on stderr.
    Customizing the status bar is a personal preference; agnes shouldn't
    clobber it. Operators who want the private indicator alongside their
    own content can compose `agnes statusline` into their command."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    user_statusline = {"type": "command", "command": "my-custom-status"}
    settings_path.write_text(json.dumps({"statusLine": user_statusline}))

    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # User's statusLine intact.
    assert cfg["statusLine"] == user_statusline
    # Warning surfaced.
    captured = capsys.readouterr()
    assert "statusLine" in captured.err


def test_install_idempotent_when_statusline_already_ours(tmp_path):
    """Re-running install when our statusLine is already in place is a no-op,
    NOT a warning (idempotent re-init shouldn't spam the user)."""
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert "agnes statusline" in cfg["statusLine"]["command"]


def test_install_replaces_old_synchronous_session_end_push(tmp_path):
    """A workspace bootstrapped before the detachment fix has the old
    synchronous `agnes push --quiet 2>/dev/null || true` SessionEnd entry.
    On the next `agnes init`, that entry must be matched by the
    `agnes push` marker and replaced with the new detached form — not
    stacked alongside it."""
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
    assert "nohup" in ends[0], (
        f"Old synchronous push entry must have been replaced with the detached form; got: {ends!r}"
    )
