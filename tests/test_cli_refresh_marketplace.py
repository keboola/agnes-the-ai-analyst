"""Tests for `agnes refresh-marketplace` Typer wrapper."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

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
        `prefix` get this CompletedProcess. Most-specific (longest)
        prefixes match first, so a `claude plugin list --json` script
        wins over a generic `claude` fallback."""
        self.scripts.append(
            (prefix, subprocess.CompletedProcess(args=list(prefix), returncode=returncode,
                                                 stdout=stdout, stderr=stderr))
        )

    def run(self, cmd, *args, env=None, capture_output=False, text=False, check=False, **kwargs):
        self.calls.append(_RecordedCall(cmd=list(cmd), env=dict(env) if env else {}))
        # Match longest prefix first so more specific scripts beat generic ones.
        sorted_scripts = sorted(self.scripts, key=lambda s: -len(s[0]))
        for prefix, scripted in sorted_scripts:
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
    """Materialize a fake `~/.agnes/marketplace/` with `.git/` and an empty
    marketplace.json so the auto-install reader has something to parse.
    Tests that exercise auto-install scenarios overwrite the manifest."""
    clone = tmp_path / "marketplace"
    (clone / ".git").mkdir(parents=True)
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": []}),
        encoding="utf-8",
    )
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


def _set_marketplace_manifest(clone: Path, plugin_names: list[str]) -> None:
    """Rewrite the local marketplace.json with the given plugin names."""
    manifest = {
        "name": "agnes",
        "plugins": [{"name": n, "source": f"./plugins/{n}"} for n in plugin_names],
    }
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )


def _plugin_list_json(entries: list[dict]) -> str:
    """Build a `claude plugin list --json` shaped response."""
    return json.dumps(entries)


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
    cfg_dir = tmp_path / "_cfg_empty"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))
    assert recorder.calls == []


def test_refresh_marketplace_uses_fetch_plus_reset_not_pull(
    with_clone, with_token, claude_in_path, recorder,
):
    """The marketplace bare repo on the server is rebuilt as orphan
    commits on every content change (see git_backend.build_bare_repo —
    `commit.parents = []`), so `git pull --ff-only` mathematically
    cannot reconcile when the server-side manifest changed.

    The refresh MUST use `git fetch + git reset --hard FETCH_HEAD` to
    treat the local clone as a snapshot mirror, not a history we own.
    Asserts:
      - First git invocation is `git fetch origin` with credential helper
      - Second git invocation is `git reset --hard FETCH_HEAD`
      - `git pull` is NEVER called
      - PAT lives in env, not in argv
    """
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    git_calls = [c for c in recorder.calls if c.cmd and c.cmd[0] == "git"]
    assert len(git_calls) >= 2, f"expected fetch + reset, got: {[c.cmd for c in git_calls]}"

    fetch = git_calls[0]
    # fetch invocation: credential helper inline, `fetch origin` action.
    assert "-c" in fetch.cmd
    helper_arg = fetch.cmd[fetch.cmd.index("-c") + 1]
    assert helper_arg.startswith("credential.helper=")
    assert "fetch" in fetch.cmd
    assert "origin" in fetch.cmd
    # PAT visible only via env, not argv.
    for arg in fetch.cmd:
        assert with_token not in arg, f"PAT leaked into argv: {arg!r}"
    assert fetch.env.get("AGNES_TOKEN") == with_token

    # reset invocation: hard reset to FETCH_HEAD, no helper needed.
    reset = git_calls[1]
    assert "reset" in reset.cmd
    assert "--hard" in reset.cmd
    assert "FETCH_HEAD" in reset.cmd

    # `git pull` must NOT appear anywhere — that's the bug we're avoiding.
    assert not any("pull" in c.cmd for c in git_calls)


def test_refresh_marketplace_calls_claude_marketplace_update_after_fetch(
    with_clone, with_token, claude_in_path, recorder,
):
    """After a successful fetch+reset, run `claude plugin marketplace update agnes`."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    update_calls = [c for c in recorder.calls
                    if c.cmd[:4] == ["claude", "plugin", "marketplace", "update"]]
    assert update_calls, "expected `claude plugin marketplace update` invocation"
    assert update_calls[0].cmd[4] == rm_module.MARKETPLACE_NAME


def test_refresh_marketplace_skips_claude_when_not_in_path(
    with_clone, with_token, claude_not_in_path, recorder,
):
    """When `claude` isn't on PATH, git fetch+reset still runs but the
    claude steps (marketplace update + auto-install) are skipped with a
    stderr warning. Command exits 0."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    # Git ran (fetch + reset).
    assert any(c.cmd[:1] == ["git"] for c in recorder.calls)
    # Claude did NOT run.
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)
    # Warning surfaced.
    assert "claude" in _clean(result.output).lower()


def test_refresh_marketplace_git_fetch_failure_exits_nonzero(
    with_clone, with_token, claude_in_path, recorder,
):
    """A non-zero git fetch exits 1 and skips downstream steps."""
    recorder.script(("git", "-c"), returncode=1, stderr="fatal: unable to access ...")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    # No claude call after a fetch failure.
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)


def test_refresh_marketplace_auto_installs_missing_plugins(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """The agnes marketplace is admin-curated per RBAC. After a refresh,
    any plugin in marketplace.json that ISN'T already installed in this
    workspace must auto-install via `claude plugin install <name>@agnes
    --scope project`. Plugins installed in OTHER workspaces don't count
    as "already installed" here (filtered by projectPath)."""
    # cwd matters — projectPath comparison is done against Path.cwd().
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # Marketplace lists three plugins.
    _set_marketplace_manifest(with_clone, ["grpn-eng", "grpn-fin", "store-bundle"])

    # Two installed: grpn-eng in this workspace (already installed),
    # store-bundle in a SIBLING workspace (does NOT count here).
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "projectPath": str(workspace), "scope": "project"},
            {"id": "store-bundle@agnes", "projectPath": str(sibling), "scope": "project"},
            # Plugin from a different marketplace — must be ignored entirely.
            {"id": "third-party-thing@some-other", "projectPath": str(workspace)},
        ]),
    )

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0

    install_calls = [
        c for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "install"]
    ]
    install_targets = sorted(c.cmd[3] for c in install_calls)
    # grpn-fin is missing entirely (not installed anywhere).
    # store-bundle is installed in sibling workspace, so missing HERE.
    # grpn-eng is already installed here, so NOT in the install set.
    assert install_targets == [
        f"grpn-fin@{rm_module.MARKETPLACE_NAME}",
        f"store-bundle@{rm_module.MARKETPLACE_NAME}",
    ]
    # Each install used --scope project.
    for c in install_calls:
        assert "--scope" in c.cmd and "project" in c.cmd


def test_refresh_marketplace_auto_install_noop_when_all_present(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """When every marketplace plugin is already installed in this workspace,
    no `claude plugin install` calls happen."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, ["grpn-eng"])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "projectPath": str(workspace), "scope": "project"},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    install_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"]]
    assert install_calls == []


def test_refresh_marketplace_auto_install_handles_empty_marketplace(
    with_clone, with_token, claude_in_path, recorder,
):
    """Empty marketplace.json `plugins` array (RBAC-empty user) → no
    install calls, no warning."""
    # with_clone fixture seeds an empty manifest by default.
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    install_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"]]
    assert install_calls == []


def test_refresh_marketplace_auto_upgrade_iterates_installed_agnes_plugins(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """--auto-upgrade calls `claude plugin update <name>@agnes` for each
    plugin from the agnes marketplace already installed in THIS workspace
    (filtered by projectPath; the third-party plugin and the sibling-
    workspace agnes plugin must both be skipped)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    _set_marketplace_manifest(with_clone, ["grpn-eng", "store-bundle"])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "projectPath": str(workspace)},
            {"id": "store-bundle@agnes", "projectPath": str(workspace)},
            {"id": "grpn-eng@agnes", "projectPath": str(sibling)},  # ignored
            {"id": "third-party@some-other", "projectPath": str(workspace)},  # ignored
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, ["--auto-upgrade"])
    assert result.exit_code == 0
    update_calls = [
        c for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "update"]
    ]
    update_targets = sorted(c.cmd[3] for c in update_calls)
    assert update_targets == [
        f"grpn-eng@{rm_module.MARKETPLACE_NAME}",
        f"store-bundle@{rm_module.MARKETPLACE_NAME}",
    ]


def test_refresh_marketplace_auto_upgrade_warns_when_list_unparseable(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """If `claude plugin list --json` returns garbage during --auto-upgrade,
    warn and exit 0 — the manifest update + auto-install already happened,
    so the user just doesn't get auto-version-bumps this run."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    recorder.script(("claude", "plugin", "list", "--json"),
                    returncode=0, stdout="not json at all")
    result = runner.invoke(refresh_marketplace_app, ["--auto-upgrade"])
    assert result.exit_code == 0
    update_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "update"]]
    assert update_calls == []
