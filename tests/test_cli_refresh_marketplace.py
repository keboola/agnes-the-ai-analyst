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

    def script(self, prefix: tuple[str, ...], returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Register a scripted response. Calls whose cmd starts with
        ``prefix`` get this CompletedProcess. Most-specific (longest)
        prefixes match first, so a ``claude plugin list --json`` script
        wins over a generic ``claude`` fallback."""
        self.scripts.append(
            (
                prefix,
                subprocess.CompletedProcess(args=list(prefix), returncode=returncode, stdout=stdout, stderr=stderr),
            )
        )

    def run(self, cmd, *args, env=None, capture_output=False, text=False, check=False, **kwargs):
        self.calls.append(_RecordedCall(cmd=list(cmd), env=dict(env) if env else {}))
        # Match longest prefix first so more specific scripts beat generic ones.
        sorted_scripts = sorted(self.scripts, key=lambda s: -len(s[0]))
        for prefix, scripted in sorted_scripts:
            if tuple(cmd[: len(prefix)]) == prefix:
                return scripted
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")


@pytest.fixture
def recorder(monkeypatch) -> _SubprocessRecorder:
    rec = _SubprocessRecorder()
    monkeypatch.setattr(rm_module.subprocess, "run", rec.run)
    return rec


@pytest.fixture
def with_clone(tmp_path, monkeypatch) -> Path:
    """Materialize a fake `~/.agnes/marketplace/` with `.git/` and an empty
    marketplace.json so the reconcile step has something to parse."""
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
    # `shutil.which` returns the bare name (not a `/fake/...` path) so that on
    # POSIX `_claude_base_cmd()` yields `["claude", ...]` — the argv prefix the
    # scripts/assertions below match against. (The Windows `.cmd`-shim wrapping
    # is covered directly in test_claude_base_cmd_* below.)
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: "claude" if name == "claude" else None)


@pytest.fixture
def claude_not_in_path(monkeypatch):
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: None)


def _set_marketplace_manifest(clone: Path, plugins: list[dict]) -> None:
    """Rewrite the local marketplace.json with the given plugin list.
    Each entry must have at least ``name`` and ``version`` (the reconcile
    flow ignores entries without a version since it can't compare)."""
    manifest = {"name": "agnes", "plugins": plugins}
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _plugin_list_json(entries: list[dict]) -> str:
    return json.dumps(entries)


# --- Tests ----------------------------------------------------------------------


def test_refresh_marketplace_help():
    result = runner.invoke(refresh_marketplace_app, ["--help"])
    assert result.exit_code == 0
    cleaned = _clean(result.output)
    # --check is the SessionStart-hook-friendly detector mode (replaced
    # --quiet, which used to perform a full reconcile silently).
    assert "--check" in cleaned
    assert "--bootstrap" in cleaned
    # --quiet was removed in favour of --check + the /update-agnes-plugins
    # slash command. --auto-upgrade was removed earlier (version-aware
    # reconcile is the default).
    assert "--quiet" not in cleaned
    assert "--auto-upgrade" not in cleaned


def test_refresh_marketplace_no_clone_is_silent_noop_with_check(tmp_path, monkeypatch, recorder):
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 0
    assert _clean(result.output) == ""
    assert recorder.calls == []


def test_refresh_marketplace_no_clone_explains_in_manual_mode(tmp_path, monkeypatch, recorder):
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert "No marketplace clone" in _clean(result.output)
    assert recorder.calls == []


def test_no_clone_short_circuits_before_token_check(tmp_path, monkeypatch, recorder):
    """The no-clone no-op path must NOT require a token.

    The SessionStart hook (`agnes refresh-marketplace --check`) runs in
    every workspace that has the hook installed, including ones where no
    agnes token is configured (e.g. a fresh CI checkout, a workspace
    that never went through `agnes init`, a project sharing the user's
    SessionStart settings.json without sharing their agnes config dir).
    Forcing token resolution before the no-op short-circuit would surface
    spurious auth_failed errors on those legitimate no-marketplace setups.

    Regression: an earlier rev moved the token check above the clone-
    exists check (needed it for --bootstrap), which broke CI on the
    silent-noop tests that don't seed a token.
    """
    # No token on disk, no AGNES_TOKEN env var, no clone.
    cfg_dir = tmp_path / "_cfg_empty"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")

    # --check (hook context).
    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 0, (
        f"hook context should silent-noop without a token; got exit {result.exit_code} and output {result.output!r}"
    )
    assert _clean(result.output) == ""
    assert recorder.calls == []

    # Manual mode (no flags): hint, but still exit 0 + no token resolution.
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert "No marketplace clone" in _clean(result.output)
    assert recorder.calls == []


def test_refresh_marketplace_no_token_friendly_exit(with_clone, tmp_path, monkeypatch, recorder):
    cfg_dir = tmp_path / "_cfg_empty"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))
    assert recorder.calls == []


def test_refresh_marketplace_uses_fetch_plus_reset_not_pull(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
):
    """Server-side bare repos rebuild as orphan commits, so `git pull --ff-only`
    cannot reconcile. Refresh must `git fetch + reset --hard FETCH_HEAD`."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    git_calls = [c for c in recorder.calls if c.cmd and c.cmd[0] == "git"]
    assert len(git_calls) >= 2

    fetch = git_calls[0]
    assert "-c" in fetch.cmd
    assert fetch.cmd[fetch.cmd.index("-c") + 1].startswith("credential.helper=")
    assert "fetch" in fetch.cmd and "origin" in fetch.cmd
    for arg in fetch.cmd:
        assert with_token not in arg
    assert fetch.env.get("AGNES_TOKEN") == with_token

    reset = git_calls[1]
    assert "reset" in reset.cmd and "--hard" in reset.cmd and "FETCH_HEAD" in reset.cmd

    assert not any("pull" in c.cmd for c in git_calls)


def test_refresh_marketplace_calls_claude_marketplace_update_after_fetch(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
):
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    update_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "update"]]
    assert update_calls
    assert update_calls[0].cmd[4] == rm_module.MARKETPLACE_NAME


def test_refresh_marketplace_skips_claude_when_not_in_path(
    with_clone,
    with_token,
    claude_not_in_path,
    recorder,
):
    """Claude not on PATH → git fetch+reset still runs, claude steps skipped
    with stderr warning, exit 0."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert any(c.cmd[:1] == ["git"] for c in recorder.calls)
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)
    assert "claude" in _clean(result.output).lower()


def test_refresh_marketplace_git_fetch_failure_exits_nonzero(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
):
    recorder.script(("git", "-c"), returncode=1, stderr="fatal: unable to access ...")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)


# --- Version-aware reconciliation -----------------------------------------------


def test_reconcile_installs_missing_plugins(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Plugin in manifest but not installed in this workspace → install."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn-eng", "version": "1.0.0"},
            {"name": "grpn-fin", "version": "0.5.0"},  # new
        ],
    )
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0

    install_targets = sorted(c.cmd[3] for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"])
    assert install_targets == [f"grpn-fin@{rm_module.MARKETPLACE_NAME}"]
    # No update calls (version of grpn-eng matches).
    update_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "update"]]
    assert update_calls == []


def test_reconcile_updates_when_manifest_version_differs(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Plugin already installed but at older version than the manifest →
    update. Critical for the /store skill+agent bundle whose version is
    a content hash that bumps on every skill add/remove without changing
    the plugin set."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn-eng", "version": "1.1.0"},  # admin pushed new version
            {"name": "flea", "version": "deadbeefcafef00d"},  # bundle bumped
        ],
    )
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
                {"id": "flea@agnes", "version": "0123456789abcdef", "projectPath": str(workspace)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0

    update_targets = sorted(c.cmd[3] for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "update"])
    assert update_targets == [
        f"flea@{rm_module.MARKETPLACE_NAME}",
        f"grpn-eng@{rm_module.MARKETPLACE_NAME}",
    ]
    # No installs (both already present).
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)


def test_reconcile_noop_when_versions_match(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Versions all match → no install/update calls (just fetch + claude
    marketplace update)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn-eng", "version": "1.0.0"},
        ],
    )
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


def test_reconcile_filters_by_project_path(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """A plugin installed in a SIBLING workspace doesn't count as installed
    here — must trigger install in this workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn-eng", "version": "1.0.0"},
        ],
    )
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(sibling)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    install_targets = sorted(c.cmd[3] for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"])
    assert install_targets == [f"grpn-eng@{rm_module.MARKETPLACE_NAME}"]


def test_reconcile_skips_third_party_marketplace(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Plugins from non-agnes marketplaces must be ignored entirely
    (not counted as installed, not considered for install/update)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn-eng", "version": "1.0.0"},
        ],
    )
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "third-party-thing@some-other", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    # grpn-eng must be installed (not seen as already-present).
    install_targets = sorted(c.cmd[3] for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"])
    assert install_targets == [f"grpn-eng@{rm_module.MARKETPLACE_NAME}"]
    # third-party plugin must NOT be touched in any way.
    assert not any(
        c.cmd[:3] == ["claude", "plugin", "update"] and c.cmd[3].startswith("third-party-thing") for c in recorder.calls
    )


def test_reconcile_handles_empty_marketplace(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
):
    """Empty manifest plugins array → no install/update calls, no warning."""
    # with_clone fixture seeds an empty manifest by default.
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


def test_reconcile_warns_when_plugin_list_unparseable(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """If `claude plugin list --json` returns garbage, warn and skip
    reconcile rather than fail. The fetch+reset already happened, so
    Claude Code will pick up the changes naturally on next session."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-eng", "version": "1.0.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), returncode=0, stdout="not json at all")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


# --- Reload hint (default + slash-command chatty path) -------------------------


def test_manual_mode_prints_reload_hint_when_anything_changed(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """When `agnes refresh-marketplace` runs without --quiet AND something
    actually got installed/updated, the operator needs to know they should
    `/reload-plugins` in Claude Code to pick up the change. Print the hint
    at end of run."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-fin", "version": "0.5.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "/reload-plugins" in out


def test_manual_mode_no_change_does_not_print_reload_hint(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Manual `agnes refresh-marketplace` over an already-up-to-date stack
    must NOT spam the reload hint — there's nothing to reload for.

    "Up to date" now also means the workspace `enabledPlugins` map already
    matches the stack; without that seed the enable step would otherwise
    flip a missing entry to `true` and legitimately request a reload.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"grpn-eng@agnes": True}}),
        encoding="utf-8",
    )
    _set_marketplace_manifest(with_clone, [{"name": "grpn-eng", "version": "1.0.0"}])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "/reload-plugins" not in out


def test_manual_mode_does_not_emit_hook_json(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Default mode (no flags) emits human-readable text — never a JSON envelope.

    Hook JSON is reserved for `--check`. The slash command runs the
    default chatty path, so its output is plain prose for the user."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-fin", "version": "0.5.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "grpn-fin" in out
    assert not out.strip().startswith("{"), f"manual mode should not emit JSON envelope; got: {out.strip()[:200]!r}"


# --- --bootstrap flag (initial install path) ------------------------------------


def test_bootstrap_flag_appears_in_help():
    result = runner.invoke(refresh_marketplace_app, ["--help"])
    assert result.exit_code == 0
    assert "--bootstrap" in _clean(result.output)


def test_no_bootstrap_no_clone_is_noop_default(
    tmp_path,
    monkeypatch,
    with_token,
    recorder,
):
    """Without --bootstrap, missing clone → silent no-op (manual mode hint).
    No git/claude calls happen."""
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert "No marketplace clone" in _clean(result.output)
    # No subprocess calls — we exited before fetch+reset.
    assert recorder.calls == []


def test_bootstrap_with_no_existing_clone_clones_and_registers(
    tmp_path,
    monkeypatch,
    with_token,
    claude_in_path,
    recorder,
):
    """--bootstrap on a fresh machine (no clone yet) must:
      1. git clone https://x:<PAT>@host/marketplace.git/ to CLONE_DIR
      2. git remote set-url origin <token-stripped URL>
      3. claude plugin marketplace add <CLONE_DIR>
      4. then proceed to the normal fetch+reset+reconcile flow

    PAT must be in the clone URL (HTTP Basic in user-info, the only
    auth path raw `git clone` understands), but stripped from the
    origin URL after the clone so it doesn't sit at rest in
    .git/config."""
    # `with_token` fixture already wrote token.json + set AGNES_CONFIG_DIR;
    # just append the server URL config so bootstrap can read it.
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n",
        encoding="utf-8",
    )

    clone_target = tmp_path / "fresh_marketplace"
    monkeypatch.setattr(rm_module, "CLONE_DIR", clone_target)

    # Create the .git/ dir as a side effect of the scripted clone so the
    # subsequent fetch+reset path sees a "cloned" state.
    real_run = recorder.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            (clone_target / ".git").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin" / "marketplace.json").write_text(
                json.dumps({"name": "agnes", "plugins": []}),
                encoding="utf-8",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(rm_module.subprocess, "run", fake_run)

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0, result.output

    # 1. git clone with embedded PAT.
    clone_calls = [c for c in recorder.calls if c.cmd[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    clone = clone_calls[0]
    assert any(with_token in arg and "agnes.example.com/marketplace.git/" in arg for arg in clone.cmd), (
        f"PAT-bearing clone URL must be in argv, got: {clone.cmd}"
    )
    assert str(clone_target) in clone.cmd

    # 2. remote set-url (PAT-stripped URL).
    set_url_calls = [c for c in recorder.calls if c.cmd[:5] == ["git", "-C", str(clone_target), "remote", "set-url"]]
    assert len(set_url_calls) == 1
    new_url = set_url_calls[0].cmd[6]
    assert "agnes.example.com/marketplace.git/" in new_url
    assert with_token not in new_url
    assert "x:" not in new_url

    # 3. claude plugin marketplace add <clone_target>.
    add_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "add"]]
    assert len(add_calls) == 1
    assert add_calls[0].cmd[4] == str(clone_target)


def test_bootstrap_honors_marketplace_url_env_override(
    tmp_path,
    monkeypatch,
    with_token,
    claude_in_path,
    recorder,
):
    """``AGNES_MARKETPLACE_URL`` overrides the derived ``server_host/marketplace.git/``
    base for deployments that serve the marketplace from a different host
    than the API — reverse-proxy split, CDN-fronted marketplace, etc.
    Issue #345 A.
    """
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGNES_MARKETPLACE_URL", "https://plugins.example.com/marketplace.git/")

    clone_target = tmp_path / "fresh_marketplace"
    monkeypatch.setattr(rm_module, "CLONE_DIR", clone_target)

    real_run = recorder.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            (clone_target / ".git").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin" / "marketplace.json").write_text(
                json.dumps({"name": "agnes", "plugins": []}),
                encoding="utf-8",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(rm_module.subprocess, "run", fake_run)

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0, result.output

    # Clone URL must point at the env-override host, NOT at the
    # api server's hostname, and must carry the PAT.
    clone_calls = [c for c in recorder.calls if c.cmd[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    url_arg = next(a for a in clone_calls[0].cmd if a.startswith("https://"))
    assert "plugins.example.com/marketplace.git/" in url_arg
    assert "agnes.example.com" not in url_arg
    assert with_token in url_arg

    # PAT-stripped URL after clone is also the override host.
    set_url_calls = [c for c in recorder.calls if c.cmd[:5] == ["git", "-C", str(clone_target), "remote", "set-url"]]
    assert len(set_url_calls) == 1
    new_url = set_url_calls[0].cmd[6]
    assert "plugins.example.com/marketplace.git/" in new_url
    assert with_token not in new_url


def test_bootstrap_rejects_invalid_marketplace_url_env(
    tmp_path,
    monkeypatch,
    with_token,
    claude_in_path,
):
    """``AGNES_MARKETPLACE_URL`` without scheme is rejected with a clear
    error — silent fallback to the derived URL would hide an operator
    misconfiguration. Issue #345 A.
    """
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGNES_MARKETPLACE_URL", "plugins.example.com/marketplace.git/")
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "fresh_marketplace")

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 1
    assert "AGNES_MARKETPLACE_URL" in result.output


def test_bootstrap_clone_failure_exits_nonzero(
    tmp_path,
    monkeypatch,
    with_token,
    claude_in_path,
    recorder,
):
    """If `git clone` fails during bootstrap, exit non-zero and don't
    proceed to fetch+reset."""
    # `with_token` fixture already created _cfg + token.json; just add
    # the server URL config so the bootstrap path can read it.
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "fresh_marketplace")
    recorder.script(("git", "clone"), returncode=1, stderr="fatal: TLS error")

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 1
    # The fetch+reset step should NOT have run (we exit on bootstrap failure).
    fetch_calls = [c for c in recorder.calls if "fetch" in c.cmd and "origin" in c.cmd]
    assert fetch_calls == []


def test_bootstrap_with_existing_clone_skips_clone_proceeds_to_refresh(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """--bootstrap on a machine that already has a clone must NOT re-clone
    (idempotent). It just falls through to the normal fetch+reset path."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0

    # No git clone (clone already existed).
    clone_calls = [c for c in recorder.calls if c.cmd[:2] == ["git", "clone"]]
    assert clone_calls == []
    # But fetch+reset DID happen.
    fetch_calls = [c for c in recorder.calls if "fetch" in c.cmd and "origin" in c.cmd]
    assert fetch_calls
    reset_calls = [c for c in recorder.calls if "reset" in c.cmd and "--hard" in c.cmd]
    assert reset_calls


# --- --check flag (SessionStart-hook detector mode) -----------------------------


def _stage_rev_parse(monkeypatch, recorder, *, head: str, remote_head: str) -> None:
    """Wrap recorder.run so `git rev-parse HEAD` returns the local SHA
    and `git ls-remote origin HEAD` returns the remote SHA, while every
    other command falls through to the recorder's normal handling.

    Used by --check tests to drive the local-HEAD vs remote-HEAD
    comparison independently of the (mocked) git invocation.
    """
    real_run = recorder.run

    def staged_run(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            recorder.calls.append(_RecordedCall(cmd=list(cmd), env=dict(kwargs.get("env") or {})))
            return subprocess.CompletedProcess(
                args=list(cmd),
                returncode=0,
                stdout=head + "\n",
                stderr="",
            )
        if "ls-remote" in cmd:
            recorder.calls.append(_RecordedCall(cmd=list(cmd), env=dict(kwargs.get("env") or {})))
            return subprocess.CompletedProcess(
                args=list(cmd),
                returncode=0,
                stdout=f"{remote_head}\tHEAD\n",
                stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(rm_module.subprocess, "run", staged_run)


def test_check_emits_hook_json_when_remote_changed(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`--check` + local HEAD differs from remote HEAD →
    Claude Code hook JSON on stdout pointing the user at
    `/update-agnes-plugins`. The hook never installs anything itself."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _stage_rev_parse(monkeypatch, recorder, head="abc123", remote_head="def456")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    # Drift → dedicated exit code so in-process callers (`agnes update`, which
    # runs from the detached SessionStart hook and invokes this `--check`
    # in-process) can branch on it.
    assert result.exit_code == rm_module._EXIT_MARKETPLACE_DRIFT

    out = _clean(result.output).strip()
    assert out, "--check must emit hook JSON when remote has changes"
    payload = json.loads(out)
    assert "/update-agnes-plugins" in payload["systemMessage"], payload
    assert "marketplace" in payload["systemMessage"].lower(), payload
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "/update-agnes-plugins" in payload["hookSpecificOutput"]["additionalContext"]


def test_check_silent_when_remote_unchanged(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`--check` + local HEAD == remote HEAD → silent exit 0, no JSON
    output. Avoids spamming the user with "updates available" on every
    session start when nothing actually changed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _stage_rev_parse(monkeypatch, recorder, head="samehash", remote_head="samehash")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 0
    assert _clean(result.output).strip() == ""


def test_check_does_not_call_claude_plugin_anything(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`--check` must NOT call `claude plugin install/update` or
    `claude plugin marketplace update`. Those side effects belong to
    the `/update-agnes-plugins` slash command, which the user runs
    interactively when they're ready."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    # Even WITH a remote diff, --check must stay read-only.
    _stage_rev_parse(monkeypatch, recorder, head="abc", remote_head="def")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    # Drift exit code (remote diff present), but still read-only.
    assert result.exit_code == rm_module._EXIT_MARKETPLACE_DRIFT

    forbidden_prefixes = (
        ["claude", "plugin", "install"],
        ["claude", "plugin", "update"],
        ["claude", "plugin", "marketplace", "update"],
    )
    for prefix in forbidden_prefixes:
        assert not any(c.cmd[: len(prefix)] == prefix for c in recorder.calls), (
            f"--check must not invoke {' '.join(prefix)}; got: {[c.cmd for c in recorder.calls]!r}"
        )


def test_check_does_not_git_reset(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`--check` is read-only against the git tree. Must NOT call
    `git reset --hard` — that would silently apply remote changes the
    user hasn't agreed to yet."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _stage_rev_parse(monkeypatch, recorder, head="abc", remote_head="def")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    # Drift exit code, but the tree must stay untouched (no reset).
    assert result.exit_code == rm_module._EXIT_MARKETPLACE_DRIFT

    reset_calls = [c for c in recorder.calls if "reset" in c.cmd]
    assert reset_calls == [], f"--check must not call git reset; got: {[c.cmd for c in reset_calls]!r}"


def test_check_runs_git_ls_remote_not_fetch(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`--check` must use `git ls-remote origin HEAD` — one HTTPS
    round-trip, no objects downloaded — and must NOT run `git fetch`.
    This is the whole point of the SessionStart-hook detector: ~0.5–1 s
    instead of ~8 s. If somebody regresses this back to fetch, this
    test catches it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _stage_rev_parse(monkeypatch, recorder, head="abc", remote_head="abc")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 0

    ls_remote_calls = [
        c
        for c in recorder.calls
        if c.cmd and c.cmd[0] == "git" and "ls-remote" in c.cmd and "origin" in c.cmd and "HEAD" in c.cmd
    ]
    assert ls_remote_calls, f"--check must run `git ls-remote origin HEAD`; got: {[c.cmd for c in recorder.calls]!r}"
    # Same credential helper wiring as the default mode — PAT in env, not argv.
    ls_remote = ls_remote_calls[0]
    assert "-c" in ls_remote.cmd
    assert ls_remote.cmd[ls_remote.cmd.index("-c") + 1].startswith("credential.helper=")
    assert ls_remote.env.get("AGNES_TOKEN") == with_token

    # No `git fetch` — that's the slow path we replaced.
    fetch_calls = [c for c in recorder.calls if c.cmd and c.cmd[0] == "git" and "fetch" in c.cmd]
    assert fetch_calls == [], f"--check must NOT run `git fetch` (slow path); got: {[c.cmd for c in fetch_calls]!r}"


def test_check_no_clone_silent_exit_zero(tmp_path, monkeypatch, with_token, recorder):
    """`--check` on a workspace without a marketplace clone → silent
    exit 0 (matches the old --quiet hook no-op semantics, so workspaces
    that never bootstrapped don't spam "no clone" warnings on every
    session start)."""
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 0
    assert _clean(result.output).strip() == ""
    assert recorder.calls == []


def test_check_ls_remote_failure_exits_one(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """A failed `git ls-remote` (network down, auth rejected, etc.) →
    exit 1 so the surrounding `|| true` in the hook command swallows it
    cleanly. No hook JSON is emitted (we don't know if the remote
    changed)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    # `("git", "-c")` matches the credential-helper wiring shared by
    # ls-remote and fetch — fine here since ls-remote is the only git
    # subprocess --check runs.
    recorder.script(("git", "-c"), returncode=1, stderr="fatal: unable to access ...")

    result = runner.invoke(refresh_marketplace_app, ["--check"])
    assert result.exit_code == 1
    # No hook JSON on failure — the hook surrounding `|| true` swallows
    # the non-zero exit so users don't see a half-written message.
    assert not _clean(result.output).strip().startswith("{")


def test_check_and_bootstrap_are_mutually_exclusive(
    tmp_path,
    monkeypatch,
    with_token,
    recorder,
):
    """Mixing the two modes makes no sense (one is read-only detector,
    the other is destructive clone-and-reconcile). Reject the combo
    with a non-zero exit instead of silently picking one."""
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "fresh_marketplace")
    result = runner.invoke(refresh_marketplace_app, ["--check", "--bootstrap"])
    assert result.exit_code == 2
    assert recorder.calls == []


# --- --bootstrap recovery: clone-exists-but-CC-not-registered -------------------


def test_bootstrap_recovers_when_clone_exists_but_cc_marketplace_missing(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Clone survived but Claude Code's registry doesn't list `agnes`
    (fresh Claude Code install on the same box, manual remove, etc.).
    `--bootstrap` must re-register the clone with `claude plugin
    marketplace add CLONE_DIR` BEFORE falling through to fetch+reset+
    `marketplace update agnes` — otherwise the update fails with
    "Marketplace 'agnes' not found", which is the bug from David's
    2026-05-10 init report."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # `claude plugin marketplace list` returns ONLY the upstream Anthropic
    # marketplace — no `agnes` entry. This is the state on a clean Claude
    # Code install where the prior `agnes` registration got wiped.
    recorder.script(
        ("claude", "plugin", "marketplace", "list"),
        stdout=(
            "Configured marketplaces:\n"
            "\n"
            "  ❯ claude-plugins-official\n"
            "    Source: GitHub (anthropics/claude-plugins-official)\n"
        ),
    )

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0, result.output

    add_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "add"]]
    assert len(add_calls) == 1, (
        f"--bootstrap with existing clone but missing CC registration must "
        f"call `claude plugin marketplace add`; got: {[c.cmd for c in recorder.calls]!r}"
    )
    assert add_calls[0].cmd[4] == str(with_clone)


def test_bootstrap_skips_register_when_cc_marketplace_already_present(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Clone exists AND Claude Code already has `agnes` registered →
    `--bootstrap` must NOT re-add (idempotent). A redundant add would
    surface the `Marketplace 'agnes' already exists` error and abort
    the recovery path uselessly."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    recorder.script(
        ("claude", "plugin", "marketplace", "list"),
        stdout=(
            "Configured marketplaces:\n"
            "\n"
            "  ❯ agnes\n"
            "    Source: Local path (/Users/x/.agnes/marketplace)\n"
            "  ❯ claude-plugins-official\n"
            "    Source: GitHub (anthropics/claude-plugins-official)\n"
        ),
    )

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0, result.output

    add_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "add"]]
    assert add_calls == [], (
        f"--bootstrap must not re-add when `agnes` is already registered; got: {[c.cmd for c in add_calls]!r}"
    )


def test_bootstrap_does_not_false_positive_on_source_path_substring(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Regression: registry detector must not match the marketplace name
    when it appears only inside a `Source: …` line of an UNRELATED
    marketplace. Real-world trigger: an earlier `claude plugin marketplace
    add ~/.agnes/some-other-clone` registers a different marketplace whose
    Source line still mentions `.agnes`, which a naive `\\bagnes\\b` over
    the full stdout would treat as `agnes` already registered. Recovery
    path then skips the add and falls through to a guaranteed-broken
    `marketplace update agnes`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # `agnes` (our marketplace name) appears ONLY in the Source path,
    # never as a registered marketplace header. Recovery must add it.
    recorder.script(
        ("claude", "plugin", "marketplace", "list"),
        stdout=(
            "Configured marketplaces:\n"
            "\n"
            "  ❯ third-party-fork\n"
            "    Source: Local path (/Users/x/.agnes-related/marketplace)\n"
            "  ❯ claude-plugins-official\n"
            "    Source: GitHub (anthropics/claude-plugins-official)\n"
        ),
    )

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 0, result.output

    add_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "add"]]
    assert len(add_calls) == 1, (
        f"--bootstrap must not be fooled by `agnes` substring inside an "
        f"unrelated `Source:` line; expected one add call, got: "
        f"{[c.cmd for c in recorder.calls]!r}"
    )
    assert add_calls[0].cmd[4] == str(with_clone)


def test_bootstrap_marketplace_add_failure_is_fatal_on_fresh_clone(
    tmp_path,
    monkeypatch,
    with_token,
    claude_in_path,
    recorder,
):
    """`claude plugin marketplace add` failure during fresh-clone bootstrap
    must be fatal — silent warn-and-continue is the bug that caused David's
    init report to cascade into 4× `Marketplace 'agnes' not found` plugin
    install errors. Returning non-zero with the actual `add` stderr is the
    signal operators need to fix their machine state."""
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n",
        encoding="utf-8",
    )

    clone_target = tmp_path / "fresh_marketplace"
    monkeypatch.setattr(rm_module, "CLONE_DIR", clone_target)

    real_run = recorder.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            (clone_target / ".git").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
            (clone_target / ".claude-plugin" / "marketplace.json").write_text(
                json.dumps({"name": "agnes", "plugins": []}),
                encoding="utf-8",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(rm_module.subprocess, "run", fake_run)

    recorder.script(
        ("claude", "plugin", "marketplace", "add"),
        returncode=1,
        stderr="error: filesystem path is not readable",
    )

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 1, result.output
    # Fetch+reset must NOT have run after the fatal add failure.
    fetch_calls = [c for c in recorder.calls if "fetch" in c.cmd and "origin" in c.cmd]
    assert fetch_calls == [], (
        f"bootstrap must abort on `add` failure; fetch should not run, got: {[c.cmd for c in fetch_calls]!r}"
    )


def test_bootstrap_recovery_add_failure_is_fatal_on_existing_clone(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """When the recovery path (clone exists, CC registry empty) tries to
    re-add and `claude plugin marketplace add` fails, exit non-zero
    instead of pressing on to a guaranteed-broken `marketplace update`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    recorder.script(
        ("claude", "plugin", "marketplace", "list"),
        stdout="Configured marketplaces:\n\n  ❯ claude-plugins-official\n",
    )
    recorder.script(
        ("claude", "plugin", "marketplace", "add"),
        returncode=1,
        stderr="error: not a directory",
    )

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 1
    # `marketplace update agnes` must NOT have run — that's the cascade we're
    # cutting off.
    update_calls = [c for c in recorder.calls if c.cmd[:4] == ["claude", "plugin", "marketplace", "update"]]
    assert update_calls == [], (
        f"recovery must abort before `marketplace update` when add fails; got: {[c.cmd for c in update_calls]!r}"
    )


# --- enabledPlugins workspace-settings write -----------------------------------
#
# Refresh's reconcile step doesn't just register plugins in the global
# `~/.claude/plugins/installed_plugins.json`; it also has to write
# `enabledPlugins["<name>@agnes"] = true` into the workspace
# `.claude/settings.json`. Without that entry, Claude Code treats the
# plugin as disabled regardless of registry presence. These tests pin the
# helper's contract end-to-end through the Typer command, since the helper
# touches the filesystem and is easier to verify via the real settings.json
# state than via additional mocking.


def _read_workspace_settings(workspace: Path) -> dict:
    settings_path = workspace / ".claude" / "settings.json"
    return json.loads(settings_path.read_text(encoding="utf-8"))


def test_enable_writes_missing_key_to_workspace_settings(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Fresh workspace with no `.claude/settings.json` → refresh creates the
    file with `enabledPlugins` populated from the manifest."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(
        with_clone,
        [
            {"name": "grpn", "version": "1.0.0"},
            {"name": "grpn-data", "version": "1.1.0"},
        ],
    )
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings.get("enabledPlugins") == {
        "grpn@agnes": True,
        "grpn-data@agnes": True,
    }


def test_enable_writes_to_existing_settings_preserving_other_keys(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Workspace already has settings.json with hooks/model/permissions.
    Refresh must add `enabledPlugins` without disturbing existing keys."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    pre_existing = {
        "model": "sonnet",
        "permissions": {"allow": ["Read", "Bash"]},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
    }
    (settings_dir / "settings.json").write_text(
        json.dumps(pre_existing, indent=2),
        encoding="utf-8",
    )

    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings["model"] == "sonnet"
    assert settings["permissions"] == {"allow": ["Read", "Bash"]}
    assert settings["hooks"] == pre_existing["hooks"]
    assert settings["enabledPlugins"] == {"grpn@agnes": True}


def test_enable_overrides_local_false_back_to_true(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """User locally `claude plugin disable`-d a stack plugin (enabledPlugins
    has `false`). Stack is source of truth → refresh re-enables it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"grpn@agnes": False}}),
        encoding="utf-8",
    )

    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings["enabledPlugins"] == {"grpn@agnes": True}
    # Re-enabled → reload hint should fire (even though no install/update).
    assert "/reload-plugins" in _clean(result.output)


def test_enable_is_idempotent_when_already_true(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Every plugin in manifest already `true` in settings → refresh must
    not rewrite the file (mtime stable) and must not advertise enable
    events."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"enabledPlugins": {"grpn@agnes": True}}, indent=2),
        encoding="utf-8",
    )
    mtime_before = settings_path.stat().st_mtime_ns

    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings["enabledPlugins"] == {"grpn@agnes": True}
    assert settings_path.stat().st_mtime_ns == mtime_before, "no-op refresh must not rewrite settings.json"
    # No install/update/enable changes → no reload hint.
    assert "/reload-plugins" not in _clean(result.output)


def test_enable_preserves_non_agnes_plugins_in_map(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Workspace's `enabledPlugins` contains entries from other marketplaces
    (e.g. coupons-team-skills). Refresh must not touch those keys; it only
    adds/sets `@agnes` entries."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "coupons-skills@coupons-team-skills": True,
                    "platform-tools@coupons-team-skills": False,  # user disabled
                }
            }
        ),
        encoding="utf-8",
    )

    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings["enabledPlugins"] == {
        "coupons-skills@coupons-team-skills": True,
        "platform-tools@coupons-team-skills": False,
        "grpn@agnes": True,
    }


def test_enable_runs_regardless_of_override_sentinel(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """`refresh-marketplace` is a runtime command — it ignores the
    Initial Workspace Template sentinel and updates `enabledPlugins`
    even in admin-templated (override: true) workspaces. The sentinel
    governs `agnes init` skip only; runtime must keep the workspace in
    sync with the user's current marketplace stack."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    # Admin-managed sentinel — must NOT block runtime enable.
    (settings_dir / "init-complete").write_text(
        "completed_at: 2026-05-13T14:32:00Z\nagnes_version: 0.53.0\noverride: true\n",
        encoding="utf-8",
    )
    # No pre-existing settings.json — refresh creates one with enabledPlugins.

    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(("claude", "plugin", "list", "--json"), stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output

    settings = _read_workspace_settings(workspace)
    assert settings.get("enabledPlugins") == {"grpn@agnes": True}


def test_reload_hint_printed_when_only_enable_changes(
    with_clone,
    with_token,
    claude_in_path,
    recorder,
    monkeypatch,
    tmp_path,
):
    """Nothing to install/update, but enable map had a stale `false` entry
    → refresh flips it to `true` and prints the /reload-plugins hint so
    the user knows to reload the running session."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"grpn@agnes": False}}),
        encoding="utf-8",
    )
    _set_marketplace_manifest(with_clone, [{"name": "grpn", "version": "1.0.0"}])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json(
            [
                {"id": "grpn@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            ]
        ),
    )

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "/reload-plugins" in out
    # No install or update should have been triggered.
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


# --- _claude_base_cmd: how the `claude` CLI gets launched cross-platform --------


def test_claude_base_cmd_returns_none_when_claude_missing(monkeypatch):
    """`claude` not on PATH → None, so callers hit their claude-missing path."""
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: None)
    assert rm_module._claude_base_cmd() is None


def test_claude_base_cmd_posix_returns_bare_exe(monkeypatch):
    """POSIX: launch the resolved executable directly, no shell wrapper."""
    monkeypatch.setattr(rm_module.os, "name", "posix")
    monkeypatch.setattr(
        rm_module.shutil,
        "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    assert rm_module._claude_base_cmd() == ["/usr/local/bin/claude"]


def test_claude_base_cmd_windows_cmd_shim_routes_through_cmd(monkeypatch):
    """Windows: a `.cmd`/`.bat` npm shim can't be CreateProcess'd directly even
    with its full path — it must go through `cmd /c`."""
    monkeypatch.setattr(rm_module.os, "name", "nt")
    monkeypatch.setattr(
        rm_module.shutil,
        "which",
        lambda name: "C:\\path\\claude.cmd" if name == "claude" else None,
    )
    assert rm_module._claude_base_cmd() == ["cmd", "/c", "C:\\path\\claude.cmd"]
