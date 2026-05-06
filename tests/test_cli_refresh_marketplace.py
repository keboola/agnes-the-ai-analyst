"""Tests for `agnes refresh-marketplace` Typer wrapper."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest
from typer.testing import CliRunner

from cli.commands import refresh_marketplace as rm_module
from cli.commands.refresh_marketplace import refresh_marketplace_app

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()


# --- Test fixtures and helpers --------------------------------------------------


class _RecordedCall:
    """Captures a single subprocess.run invocation for assertion."""

    def __init__(self, cmd: list[str], env: Optional[dict] = None) -> None:
        self.cmd = cmd
        self.env = env or {}


class _SubprocessRecorder:
    """Replaces subprocess.run with a recording stub. Each scripted result
    is matched by command-prefix against incoming calls."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self.scripts: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []

    def script(self, prefix: tuple[str, ...], returncode: int = 0,
               stdout: str = "", stderr: str = "") -> None:
        """Register a scripted response. Calls whose cmd starts with
        `prefix` get this CompletedProcess."""
        self.scripts.append(
            (prefix, subprocess.CompletedProcess(args=list(prefix), returncode=returncode,
                                                 stdout=stdout, stderr=stderr))
        )

    def run(self, cmd, *args, env=None, capture_output=False, text=False, check=False, **kwargs):
        self.calls.append(_RecordedCall(cmd=list(cmd), env=dict(env) if env else {}))
        for prefix, scripted in self.scripts:
            if tuple(cmd[:len(prefix)]) == prefix:
                return scripted
        # Default: success, empty output. Lets tests that don't care about
        # specific subprocess routes pass without scripting every call.
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")


@pytest.fixture
def recorder(monkeypatch) -> _SubprocessRecorder:
    rec = _SubprocessRecorder()
    monkeypatch.setattr(rm_module.subprocess, "run", rec.run)
    return rec


@pytest.fixture
def with_clone(tmp_path, monkeypatch) -> Path:
    """Materialize a fake `~/.agnes/marketplace/.git/` so `is_dir()` succeeds.
    Redirects CLONE_DIR to the temp area."""
    clone = tmp_path / "marketplace"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(rm_module, "CLONE_DIR", clone)
    return clone


@pytest.fixture
def with_token(tmp_path, monkeypatch) -> str:
    """Persist a fake token through `cli.config.get_token`."""
    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "token.json").write_text(
        json.dumps({"access_token": "test-pat-1234", "email": "dev@localhost"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    return "test-pat-1234"


@pytest.fixture
def claude_in_path(monkeypatch):
    """Pretend `claude` resolves on PATH."""
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: "/fake/claude" if name == "claude" else None)


@pytest.fixture
def claude_not_in_path(monkeypatch):
    """Pretend `claude` is not installed."""
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: None)


# --- Tests ----------------------------------------------------------------------


def test_refresh_marketplace_help():
    result = runner.invoke(refresh_marketplace_app, ["--help"])
    assert result.exit_code == 0
    cleaned = _clean(result.output)
    assert "--quiet" in cleaned
    assert "--auto-upgrade" in cleaned


def test_refresh_marketplace_no_clone_is_silent_noop_with_quiet(tmp_path, monkeypatch, recorder):
    """When CLONE_DIR/.git doesn't exist and --quiet is passed (hook flow),
    exit 0 with no stdout and no subprocess calls."""
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, ["--quiet"])
    assert result.exit_code == 0
    assert _clean(result.output) == ""
    assert recorder.calls == []  # no git, no claude


def test_refresh_marketplace_no_clone_explains_in_manual_mode(tmp_path, monkeypatch, recorder):
    """Without --quiet, the no-clone path prints a hint instead of staying silent."""
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert "No marketplace clone" in _clean(result.output)
    assert recorder.calls == []


def test_refresh_marketplace_no_token_friendly_exit(with_clone, tmp_path, monkeypatch, recorder):
    """No PAT → exit 1 with friendly hint (no traceback). git/claude must
    not be called when auth fails up-front."""
    # Empty config dir → no token.json → get_token returns None.
    cfg_dir = tmp_path / "_cfg_empty"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))
    assert recorder.calls == []


def test_refresh_marketplace_calls_git_pull_with_credential_helper(
    with_clone, with_token, claude_in_path, recorder,
):
    """git pull invocation must:
      - use `--ff-only`
      - inject credential helper via `-c credential.helper=...`
      - pass PAT via env (AGNES_TOKEN), NEVER in argv
      - target the CLONE_DIR via -C
    """
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    git_calls = [c for c in recorder.calls if c.cmd and c.cmd[0] == "git"]
    assert git_calls, "expected at least one git invocation"
    pull = git_calls[0]
    # PAT not visible in argv (no element of cmd contains the token).
    for arg in pull.cmd:
        assert with_token not in arg, f"PAT leaked into argv: {arg!r}"
    # Credential helper is set inline as a `-c` override.
    assert "-c" in pull.cmd
    helper_arg_idx = pull.cmd.index("-c") + 1
    assert pull.cmd[helper_arg_idx].startswith("credential.helper=")
    # `pull --ff-only` is the action.
    assert "pull" in pull.cmd
    assert "--ff-only" in pull.cmd
    # PAT IS in env so the helper can read it.
    assert pull.env.get("AGNES_TOKEN") == with_token


def test_refresh_marketplace_calls_claude_marketplace_update_after_pull(
    with_clone, with_token, claude_in_path, recorder,
):
    """After a successful git pull, run `claude plugin marketplace update agnes`."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    claude_calls = [c for c in recorder.calls if c.cmd and c.cmd[0] == "claude"]
    assert claude_calls, "expected `claude plugin marketplace update` invocation"
    update = claude_calls[0]
    assert update.cmd[:4] == ["claude", "plugin", "marketplace", "update"]
    assert update.cmd[4] == rm_module.MARKETPLACE_NAME


def test_refresh_marketplace_skips_claude_when_not_in_path(
    with_clone, with_token, claude_not_in_path, recorder,
):
    """When `claude` isn't on PATH, git pull still runs but the claude
    step is skipped with a stderr warning. Command exits 0 — git pull
    success means the next session that does have claude picks up the
    changes naturally."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    # Git ran.
    assert any(c.cmd[:1] == ["git"] for c in recorder.calls)
    # Claude did NOT run.
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)
    # Warning surfaced. Typer's CliRunner mixes stderr into output by default.
    assert "claude" in _clean(result.output).lower()


def test_refresh_marketplace_git_pull_failure_exits_nonzero(
    with_clone, with_token, claude_in_path, recorder,
):
    """A non-zero git pull exits 1 and skips the claude update step
    (no point telling claude to re-read an unchanged manifest, and the
    pull failure is the actionable signal for the operator)."""
    recorder.script(("git",), returncode=1, stderr="fatal: unable to access ...")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    # No claude call after a pull failure.
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)


def test_refresh_marketplace_auto_upgrade_iterates_installed_agnes_plugins(
    with_clone, with_token, claude_in_path, recorder,
):
    """--auto-upgrade calls `claude plugin update <name>@agnes` for each
    plugin that came from the agnes marketplace (matched on the
    `marketplace` field in `claude plugin list --json` output)."""
    plugin_list_json = json.dumps([
        {"name": "grpn-eng", "marketplace": "agnes"},
        {"name": "store-bundle", "marketplace": "agnes"},
        {"name": "third-party-thing", "marketplace": "some-other"},
    ])
    recorder.script(("claude", "plugin", "list", "--json"),
                    returncode=0, stdout=plugin_list_json)
    result = runner.invoke(refresh_marketplace_app, ["--auto-upgrade"])
    assert result.exit_code == 0
    update_calls = [
        c for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "update"]
    ]
    # Two updates, one per agnes plugin. The third-party plugin must NOT
    # be updated through the agnes marketplace (it doesn't belong to it).
    update_targets = sorted(c.cmd[3] for c in update_calls)
    assert update_targets == [f"grpn-eng@{rm_module.MARKETPLACE_NAME}",
                              f"store-bundle@{rm_module.MARKETPLACE_NAME}"]


def test_refresh_marketplace_auto_upgrade_warns_when_list_unparseable(
    with_clone, with_token, claude_in_path, recorder,
):
    """If `claude plugin list --json` returns garbage, --auto-upgrade
    warns and exits 0 — the manifest update already happened, so the
    user just doesn't get auto-version-bumps this run."""
    recorder.script(("claude", "plugin", "list", "--json"),
                    returncode=0, stdout="not json at all")
    result = runner.invoke(refresh_marketplace_app, ["--auto-upgrade"])
    assert result.exit_code == 0
    update_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "update"]]
    assert update_calls == []
