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
        ``prefix`` get this CompletedProcess. Most-specific (longest)
        prefixes match first, so a ``claude plugin list --json`` script
        wins over a generic ``claude`` fallback."""
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
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: "/fake/claude" if name == "claude" else None)


@pytest.fixture
def claude_not_in_path(monkeypatch):
    monkeypatch.setattr(rm_module.shutil, "which", lambda name: None)


def _set_marketplace_manifest(clone: Path, plugins: list[dict]) -> None:
    """Rewrite the local marketplace.json with the given plugin list.
    Each entry must have at least ``name`` and ``version`` (the reconcile
    flow ignores entries without a version since it can't compare)."""
    manifest = {"name": "agnes", "plugins": plugins}
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )


def _plugin_list_json(entries: list[dict]) -> str:
    return json.dumps(entries)


# --- Tests ----------------------------------------------------------------------


def test_refresh_marketplace_help():
    result = runner.invoke(refresh_marketplace_app, ["--help"])
    assert result.exit_code == 0
    cleaned = _clean(result.output)
    assert "--quiet" in cleaned
    # --auto-upgrade is gone — version-aware reconcile is now the default.
    assert "--auto-upgrade" not in cleaned


def test_refresh_marketplace_no_clone_is_silent_noop_with_quiet(tmp_path, monkeypatch, recorder):
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
    result = runner.invoke(refresh_marketplace_app, ["--quiet"])
    assert result.exit_code == 0
    assert _clean(result.output) == ""
    assert recorder.calls == []


def test_refresh_marketplace_no_clone_explains_in_manual_mode(tmp_path, monkeypatch, recorder):
    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "nonexistent")
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
    with_clone, with_token, claude_in_path, recorder,
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
    with_clone, with_token, claude_in_path, recorder,
):
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    update_calls = [c for c in recorder.calls
                    if c.cmd[:4] == ["claude", "plugin", "marketplace", "update"]]
    assert update_calls
    assert update_calls[0].cmd[4] == rm_module.MARKETPLACE_NAME


def test_refresh_marketplace_skips_claude_when_not_in_path(
    with_clone, with_token, claude_not_in_path, recorder,
):
    """Claude not on PATH → git fetch+reset still runs, claude steps skipped
    with stderr warning, exit 0."""
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert any(c.cmd[:1] == ["git"] for c in recorder.calls)
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)
    assert "claude" in _clean(result.output).lower()


def test_refresh_marketplace_git_fetch_failure_exits_nonzero(
    with_clone, with_token, claude_in_path, recorder,
):
    recorder.script(("git", "-c"), returncode=1, stderr="fatal: unable to access ...")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 1
    assert not any(c.cmd[:1] == ["claude"] for c in recorder.calls)


# --- Version-aware reconciliation -----------------------------------------------


def test_reconcile_installs_missing_plugins(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """Plugin in manifest but not installed in this workspace → install."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "grpn-eng", "version": "1.0.0"},
        {"name": "grpn-fin", "version": "0.5.0"},  # new
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0

    install_targets = sorted(
        c.cmd[3] for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "install"]
    )
    assert install_targets == [f"grpn-fin@{rm_module.MARKETPLACE_NAME}"]
    # No update calls (version of grpn-eng matches).
    update_calls = [c for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "update"]]
    assert update_calls == []


def test_reconcile_updates_when_manifest_version_differs(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """Plugin already installed but at older version than the manifest →
    update. Critical for the /store skill+agent bundle whose version is
    a content hash that bumps on every skill add/remove without changing
    the plugin set."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "grpn-eng", "version": "1.1.0"},  # admin pushed new version
        {"name": "agnes-store-bundle", "version": "deadbeefcafef00d"},  # bundle bumped
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
            {"id": "agnes-store-bundle@agnes", "version": "0123456789abcdef",
             "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0

    update_targets = sorted(
        c.cmd[3] for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "update"]
    )
    assert update_targets == [
        f"agnes-store-bundle@{rm_module.MARKETPLACE_NAME}",
        f"grpn-eng@{rm_module.MARKETPLACE_NAME}",
    ]
    # No installs (both already present).
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)


def test_reconcile_noop_when_versions_match(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """Versions all match → no install/update calls (just fetch + claude
    marketplace update)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "grpn-eng", "version": "1.0.0"},
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


def test_reconcile_filters_by_project_path(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """A plugin installed in a SIBLING workspace doesn't count as installed
    here — must trigger install in this workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "grpn-eng", "version": "1.0.0"},
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(sibling)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    install_targets = sorted(
        c.cmd[3] for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "install"]
    )
    assert install_targets == [f"grpn-eng@{rm_module.MARKETPLACE_NAME}"]


def test_reconcile_skips_third_party_marketplace(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """Plugins from non-agnes marketplaces must be ignored entirely
    (not counted as installed, not considered for install/update)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "grpn-eng", "version": "1.0.0"},
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "third-party-thing@some-other", "version": "1.0.0",
             "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    # grpn-eng must be installed (not seen as already-present).
    install_targets = sorted(
        c.cmd[3] for c in recorder.calls
        if c.cmd[:3] == ["claude", "plugin", "install"]
    )
    assert install_targets == [f"grpn-eng@{rm_module.MARKETPLACE_NAME}"]
    # third-party plugin must NOT be touched in any way.
    assert not any(
        c.cmd[:3] == ["claude", "plugin", "update"]
        and c.cmd[3].startswith("third-party-thing")
        for c in recorder.calls
    )


def test_reconcile_handles_empty_marketplace(
    with_clone, with_token, claude_in_path, recorder,
):
    """Empty manifest plugins array → no install/update calls, no warning."""
    # with_clone fixture seeds an empty manifest by default.
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


def test_reconcile_warns_when_plugin_list_unparseable(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """If `claude plugin list --json` returns garbage, warn and skip
    reconcile rather than fail. The fetch+reset already happened, so
    Claude Code will pick up the changes naturally on next session."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-eng", "version": "1.0.0"}])
    recorder.script(("claude", "plugin", "list", "--json"),
                    returncode=0, stdout="not json at all")
    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    assert not any(c.cmd[:3] == ["claude", "plugin", "install"] for c in recorder.calls)
    assert not any(c.cmd[:3] == ["claude", "plugin", "update"] for c in recorder.calls)


# --- Hook JSON output -----------------------------------------------------------


def test_quiet_emits_hook_json_when_plugin_installed(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """--quiet + new install → hook JSON on stdout with systemMessage +
    additionalContext (the 3-source explanation)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-fin", "version": "0.5.0"}])
    recorder.script(("claude", "plugin", "list", "--json"),
                    stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, ["--quiet"])
    assert result.exit_code == 0

    out = _clean(result.output).strip()
    assert out, "expected hook JSON on stdout when a plugin was installed"
    payload = json.loads(out)
    assert "grpn-fin" in payload["systemMessage"]
    assert "Agnes stack" in payload["systemMessage"]
    assert "installed" in payload["systemMessage"]

    hook_specific = payload.get("hookSpecificOutput", {})
    assert hook_specific.get("hookEventName") == "SessionStart"
    additional = hook_specific.get("additionalContext", "")
    # Must reflect the three-source model so the model knows the change
    # could've come from any of those, not just admin grants.
    assert "RBAC" in additional
    assert "MyAIStack" in additional
    assert "/store" in additional
    # Must explain the bundle quirk so the model understands why a
    # skill/agent change shows up as "updated", not "installed".
    assert "agnes-store-bundle" in additional


def test_quiet_emits_hook_json_when_plugin_updated(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """--quiet + version-mismatch update (e.g. /store skill add bumping
    the bundle) → hook JSON with `updated` count in systemMessage."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [
        {"name": "agnes-store-bundle", "version": "newhash"},
    ])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "agnes-store-bundle@agnes", "version": "oldhash",
             "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, ["--quiet"])
    assert result.exit_code == 0
    out = _clean(result.output).strip()
    assert out
    payload = json.loads(out)
    assert "updated" in payload["systemMessage"]
    assert "agnes-store-bundle" in payload["systemMessage"]


def test_quiet_emits_no_hook_json_when_nothing_changed(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """--quiet + everything in sync → silent stdout (no spurious
    notification on every session start)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-eng", "version": "1.0.0"}])
    recorder.script(
        ("claude", "plugin", "list", "--json"),
        stdout=_plugin_list_json([
            {"id": "grpn-eng@agnes", "version": "1.0.0", "projectPath": str(workspace)},
        ]),
    )
    result = runner.invoke(refresh_marketplace_app, ["--quiet"])
    assert result.exit_code == 0
    assert _clean(result.output).strip() == ""


def test_manual_mode_does_not_emit_hook_json(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
):
    """Without --quiet, output is human-readable text — no JSON envelope."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _set_marketplace_manifest(with_clone, [{"name": "grpn-fin", "version": "0.5.0"}])
    recorder.script(("claude", "plugin", "list", "--json"),
                    stdout=_plugin_list_json([]))

    result = runner.invoke(refresh_marketplace_app, [])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "grpn-fin" in out
    assert not out.strip().startswith("{"), \
        f"manual mode should not emit JSON envelope; got: {out.strip()[:200]!r}"


# --- --bootstrap flag (initial install path) ------------------------------------


def test_bootstrap_flag_appears_in_help():
    result = runner.invoke(refresh_marketplace_app, ["--help"])
    assert result.exit_code == 0
    assert "--bootstrap" in _clean(result.output)


def test_no_bootstrap_no_clone_is_noop_default(
    tmp_path, monkeypatch, with_token, recorder,
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
    tmp_path, monkeypatch, with_token, claude_in_path, recorder,
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
        "server: https://agnes.example.com\n", encoding="utf-8",
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
    assert any(
        with_token in arg and "agnes.example.com/marketplace.git/" in arg
        for arg in clone.cmd
    ), f"PAT-bearing clone URL must be in argv, got: {clone.cmd}"
    assert str(clone_target) in clone.cmd

    # 2. remote set-url (PAT-stripped URL).
    set_url_calls = [
        c for c in recorder.calls
        if c.cmd[:5] == ["git", "-C", str(clone_target), "remote", "set-url"]
    ]
    assert len(set_url_calls) == 1
    new_url = set_url_calls[0].cmd[6]
    assert "agnes.example.com/marketplace.git/" in new_url
    assert with_token not in new_url
    assert "x:" not in new_url

    # 3. claude plugin marketplace add <clone_target>.
    add_calls = [
        c for c in recorder.calls
        if c.cmd[:4] == ["claude", "plugin", "marketplace", "add"]
    ]
    assert len(add_calls) == 1
    assert add_calls[0].cmd[4] == str(clone_target)


def test_bootstrap_clone_failure_exits_nonzero(
    tmp_path, monkeypatch, with_token, claude_in_path, recorder,
):
    """If `git clone` fails during bootstrap, exit non-zero and don't
    proceed to fetch+reset."""
    # `with_token` fixture already created _cfg + token.json; just add
    # the server URL config so the bootstrap path can read it.
    cfg_dir = tmp_path / "_cfg"
    (cfg_dir / "config.yaml").write_text(
        "server: https://agnes.example.com\n", encoding="utf-8",
    )

    monkeypatch.setattr(rm_module, "CLONE_DIR", tmp_path / "fresh_marketplace")
    recorder.script(("git", "clone"), returncode=1, stderr="fatal: TLS error")

    result = runner.invoke(refresh_marketplace_app, ["--bootstrap"])
    assert result.exit_code == 1
    # The fetch+reset step should NOT have run (we exit on bootstrap failure).
    fetch_calls = [c for c in recorder.calls if "fetch" in c.cmd and "origin" in c.cmd]
    assert fetch_calls == []


def test_bootstrap_with_existing_clone_skips_clone_proceeds_to_refresh(
    with_clone, with_token, claude_in_path, recorder, monkeypatch, tmp_path,
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
