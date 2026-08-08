"""`agnes global` command group (spec §6). All state isolated: AGNES_CONFIG_DIR
+ patched user paths + recorded subprocess.run — no real `claude`, no network."""

import json
import os
import subprocess
import time

import pytest
import yaml
from typer.testing import CliRunner

import cli.commands.global_scope as gs_module


class _Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.scripts: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        for prefix, scripted in sorted(self.scripts, key=lambda s: -len(s[0])):
            if tuple(cmd[: len(prefix)]) == prefix:
                return scripted
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "agnes-cfg"
    cfg_dir.mkdir()
    (cfg_dir / "token.json").write_text(json.dumps({"access_token": "pat", "email": "t@example.com"}), encoding="utf-8")
    (cfg_dir / "config.yaml").write_text(
        "server: http://localhost:9\nworkspace_root: " + str(tmp_path / "ws") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))

    claude_md = tmp_path / "user-claude" / "CLAUDE.md"
    settings = tmp_path / "user-claude" / "settings.json"
    monkeypatch.setattr("cli.lib.user_scope.user_claude_md_path", lambda: claude_md)
    monkeypatch.setattr("cli.lib.user_scope.user_settings_path", lambda: settings)
    monkeypatch.setattr(gs_module, "user_claude_md_path", lambda: claude_md)

    clone = tmp_path / "marketplace"
    (clone / ".git").mkdir(parents=True)
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": [{"name": "alpha", "source": "./plugins/alpha", "version": "1.0.0"}]}),
        encoding="utf-8",
    )
    import cli.commands.refresh_marketplace as rm_module

    monkeypatch.setattr(rm_module, "CLONE_DIR", clone)

    agnes_bin = tmp_path / "bin" / "agnes"
    agnes_bin.parent.mkdir(parents=True, exist_ok=True)
    agnes_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    rec = _Recorder()
    monkeypatch.setattr(rm_module.subprocess, "run", rec.run)
    monkeypatch.setattr(gs_module.subprocess, "run", rec.run)
    monkeypatch.setattr(rm_module.shutil, "which", lambda n: "claude" if n == "claude" else None)
    monkeypatch.setattr(gs_module.shutil, "which", lambda n: {"claude": "claude", "agnes": str(agnes_bin)}.get(n))
    rec.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        )
    )
    rec.scripts.append(
        (
            ("claude", "mcp", "get"),
            # The real not-found response, not just "rc=1". `claude mcp get`
            # exits non-zero both when the name is unknown and when the entry
            # exists but its server will not start, and `_mcp_entry_info` tells
            # those apart by this message — so a bare rc=1 with empty output
            # means "the probe did not answer", and every convergence test
            # using it was silently exercising the ambiguous branch.
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout='No MCP server named "agnes". Configured servers: other\n',
                stderr="",
            ),
        )
    )
    monkeypatch.setattr(gs_module, "_verify_credentials", lambda: True)
    return {
        "rec": rec,
        "claude_md": claude_md,
        "settings": settings,
        "cfg_dir": cfg_dir,
        "agnes_bin": agnes_bin,
        "clone": clone,
    }


def test_enable_converges_all_artifacts(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "install", "alpha@agnes", "--scope", "user"] in rec.calls
    mcp_adds = [c for c in rec.calls if c[:3] == ["claude", "mcp", "add"]]
    assert mcp_adds and "--scope" in mcp_adds[0] and "user" in mcp_adds[0] and mcp_adds[0][-1] == "mcp"
    assert env["claude_md"].exists()
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert cfg["hooks"]["SessionStart"], "hook installed by default (spec §13.2)"
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is True and conf["global_hook"] is True


def test_enable_no_hook(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--no-hook"])
    assert result.exit_code == 0, result.output
    assert not env["settings"].exists() or "hooks" not in json.loads(env["settings"].read_text(encoding="utf-8"))
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_hook"] is False


def test_enable_twice_idempotent(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    md_before = env["claude_md"].read_text(encoding="utf-8")
    settings_before = env["settings"].read_text(encoding="utf-8")
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0
    assert env["claude_md"].read_text(encoding="utf-8") == md_before
    assert env["settings"].read_text(encoding="utf-8") == settings_before


def test_enable_json_report(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    stages = {row["stage"] for row in doc["report"]}
    assert {"plugins", "mcp", "rails", "hook", "flag"} <= stages


def test_disable_reverts_exactly(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"agnes:\n  Scope: User config (available in all your projects)\n  Command: {env['agnes_bin']}\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='[{"id": "alpha@agnes", "version": "1.0.0", "projectPath": null, "scope": "user"}]',
                stderr="",
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "uninstall", "alpha@agnes", "--scope", "user"] in rec.calls
    assert any(c[:3] == ["claude", "mcp", "remove"] and "user" in c for c in rec.calls)
    assert not env["claude_md"].exists() or "agnes-global" not in env["claude_md"].read_text(encoding="utf-8")
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert not any(cfg.get("hooks", {}).get("SessionStart", []))
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is False


def test_disable_leaves_foreign_mcp_entry(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].calls.clear()
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Scope: User config (available in all your projects)\n  Command: /usr/bin/somethingelse\n  Args: serve\n", stderr=""
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in env["rec"].calls)


def test_status_reports_each_artifact(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"agnes:\n  Scope: User config (available in all your projects)\n  Command: {env['agnes_bin']}\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    rows = {row["artifact"]: row["state"] for row in doc["artifacts"]}
    assert rows["rails"] == "ok"
    assert rows["hook"] == "ok"
    assert rows["flag"] == "ok"
    assert rows["mcp"] == "ok"
    assert "plugins" in rows


def test_same_name_foreign_mcp_with_mcp_args_is_not_ours(env):
    """Review finding: the ours-detection must key on the command BASENAME,
    not on the literal 'agnes' (always present — it is the lookup name)."""
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Scope: User config (available in all your projects)\n  Command: /usr/bin/other-tool\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    state, cmd = gs_module._mcp_entry_info()
    assert state == "foreign"
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in env["rec"].calls)


def test_status_drifted_when_registered_binary_missing(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Scope: User config (available in all your projects)\n  Command: /gone/away/agnes\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["status", "--json"])
    doc = json.loads(result.output)
    rows = {row["artifact"]: row for row in doc["artifacts"]}
    assert rows["mcp"]["state"] == "drifted"
    assert "/gone/away/agnes" in rows["mcp"]["detail"]


def test_convergence_repairs_dead_binary_path(env):
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Scope: User config (available in all your projects)\n  Command: /gone/away/agnes\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    report: list[dict] = []
    gs_module.run_convergence(want_hook=False, force=False, report=report)
    mcp_rows = [r for r in report if r["stage"] == "mcp"]
    assert mcp_rows and mcp_rows[0]["status"] == "repaired"
    adds = [c for c in env["rec"].calls if c[:3] == ["claude", "mcp", "add"]]
    assert adds and adds[0][-2] == str(env["agnes_bin"])


def test_enable_json_stays_pure_on_bootstrap_path(env, tmp_path):
    """Review finding: the first-ever enable (no clone) must not leak the
    bootstrap's progress echoes into the --json stdout contract."""
    import shutil as _shutil

    _shutil.rmtree(env["clone"] / ".git")
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)  # raises if any stray echo corrupted stdout
    assert "report" in doc


# ---------------------------------------------------------------------------
# `mcp get` resolves across scopes — only a USER-scope hit is ours
# ---------------------------------------------------------------------------


def _mcp_get_stdout(scope: str, command: str = "/usr/local/bin/agnes", args: str = "mcp") -> str:
    """A faithful `claude mcp get agnes` transcript.

    Field order and wording taken from `claude` 2.1.220:

        agnes:
          Scope: User config (available in all your projects)
          Status: ✔ Connected
          Type: stdio
          Command: /usr/local/bin/agnes
          Args: mcp
    """
    return f"agnes:\n  Scope: {scope}\n  Status: ✔ Connected\n  Type: stdio\n  Command: {command}\n  Args: {args}\n"


def test_project_scoped_entry_is_not_mistaken_for_the_global_one(monkeypatch, env):
    """`claude mcp get` takes no scope flag and answers from ANY scope.

    An engineer with a per-project `agnes` entry in some repo's `.mcp.json`
    would otherwise make `enable` believe the all-repositories entry was
    already registered and skip creating it — the global layer would end up
    with no MCP server at all, silently (Devin on #1184). Scopes coexist, so
    a project hit reads as absent for the user-scope layer.
    """
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_mcp_get_stdout("Project config (shared via .mcp.json)"), stderr=""
        ),
    )
    assert gs_module._mcp_entry_info() == ("absent", None)


def test_local_scoped_entry_is_also_absent(monkeypatch, env):
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_mcp_get_stdout("Local config (private to you in this project)"), stderr=""
        ),
    )
    assert gs_module._mcp_entry_info() == ("absent", None)


def test_missing_scope_line_reads_as_unclassified_not_as_ours(monkeypatch, env):
    """A `claude` too old to print `Scope:` must not be assumed to mean user
    scope — assuming would resurrect the skip-the-registration bug.

    It must not read as *absent* either, which is what it used to do: the probe
    exited 0, so something IS registered under this name, and treating it as
    absent sent the caller into the remove-and-replace arm — destroying a
    user's own `agnes` entry without the `--force` that gates exactly that.
    `unclassified` is handled like `foreign`: left alone unless forced."""
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="agnes:\n  Command: /usr/local/bin/agnes\n  Args: mcp\n", stderr=""
        ),
    )
    assert gs_module._mcp_entry_info()[0] == "unclassified"


def test_user_scoped_agnes_entry_is_still_ours(monkeypatch, env):
    """The other half — the scope gate must not reject the real thing."""
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_mcp_get_stdout("User config (available in all your projects)", command=env["agnes_bin"]),
            stderr="",
        ),
    )
    state, cmd = gs_module._mcp_entry_info()
    assert state == "ours"
    assert cmd == str(env["agnes_bin"])


def test_user_scoped_foreign_entry_is_foreign(monkeypatch, env):
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_mcp_get_stdout("User config (available in all your projects)", command="/usr/bin/other-tool"),
            stderr="",
        ),
    )
    assert gs_module._mcp_entry_info()[0] == "foreign"


# ---------------------------------------------------------------------------
# Clone freshness gates the drift-check — not "does the user have a workspace"
# ---------------------------------------------------------------------------


def _clone_with(tmp_path, *, fetch_head_age=None, head_age=None, name="mkt-freshness"):
    import os

    clone = tmp_path / name
    (clone / ".git").mkdir(parents=True)
    now = time.time()
    for name, age in (("FETCH_HEAD", fetch_head_age), ("HEAD", head_age)):
        if age is None:
            continue
        f = clone / ".git" / name
        f.write_text("x", encoding="utf-8")
        os.utime(f, (now - age, now - age))
    return clone


def test_recently_fetched_clone_is_fresh(tmp_path):
    assert gs_module._clone_is_stale(_clone_with(tmp_path, fetch_head_age=60)) is False


def test_long_unfetched_clone_is_stale(tmp_path):
    assert gs_module._clone_is_stale(_clone_with(tmp_path, fetch_head_age=48 * 3600)) is True


def test_never_fetched_clone_falls_back_to_head_mtime(tmp_path):
    """A clone that was cloned but never fetched has no FETCH_HEAD."""
    assert gs_module._clone_is_stale(_clone_with(tmp_path, head_age=60, name="fresh")) is False
    assert gs_module._clone_is_stale(_clone_with(tmp_path, head_age=48 * 3600, name="old")) is True


def test_unreadable_clone_reads_as_stale(tmp_path):
    """Neither marker present — refresh and let it decide, rather than
    assuming fresh and freezing the stack."""
    assert gs_module._clone_is_stale(_clone_with(tmp_path)) is True


def test_an_anchored_workspace_no_longer_suppresses_the_refresh(tmp_path):
    """The persona the old gate stranded.

    The previous condition ran the drift-check only when `workspace_root` was
    unset, on the assumption that anyone with a workspace gets their clone
    refreshed by the workspace marketplace step. An engineer who anchored a
    workspace once and then works all day in other repositories never runs
    that step, so their user-scope stack froze (Devin on #1184). The gate is
    now freshness, which does not care whether a workspace is anchored.
    """
    stale = _clone_with(tmp_path, fetch_head_age=48 * 3600)
    fresh = _clone_with(tmp_path, fetch_head_age=60, name="mkt-fresh")
    # Both cases here have an anchored workspace; only staleness decides.
    assert gs_module._clone_is_stale(stale) is True
    assert gs_module._clone_is_stale(fresh) is False


# ---------------------------------------------------------------------------
# A partial disable stays ENABLED — the off flag is written last, or not at all
# ---------------------------------------------------------------------------


def _seed_one_installed_plugin(env, *, uninstall_rc=0):
    """`plugin list` reports one user-scope plugin; `plugin uninstall` returns
    `uninstall_rc`. Also pins the `mcp get` answer so the entry is ours."""
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "agnes:\n  Scope: User config (available in all your projects)\n"
                    f"  Command: {env['agnes_bin']}\n  Args: mcp\n"
                ),
                stderr="",
            ),
        ),
    )
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='[{"id": "alpha@agnes", "version": "1.0.0", "projectPath": null, "scope": "user"}]',
                stderr="",
            ),
        ),
    )
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "plugin", "uninstall"),
            subprocess.CompletedProcess(args=[], returncode=uninstall_rc, stdout="", stderr="boom"),
        ),
    )


def test_a_failed_uninstall_keeps_the_layer_marked_enabled(env):
    """Flipping the flag regardless was the hazard: the skills and the tool
    entry keep loading in EVERY repository the user opens, while the config
    says the layer is off — so `agnes update` stops converging it and nothing
    is left that would ever clean them up. A partial disable is still enabled
    (Devin on #1184).
    """
    CliRunner().invoke(gs_module.global_app, ["enable"])
    _seed_one_installed_plugin(env, uninstall_rc=1)

    result = CliRunner().invoke(gs_module.global_app, ["disable"])

    assert result.exit_code == 1, result.output
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf.get("global_scope") is not False, "the layer was marked off while its plugins are still installed"
    assert "alpha" in result.output


def test_a_clean_disable_still_writes_the_flag(env):
    """The other half — the gate must not block the ordinary path."""
    CliRunner().invoke(gs_module.global_app, ["enable"])
    _seed_one_installed_plugin(env, uninstall_rc=0)

    result = CliRunner().invoke(gs_module.global_app, ["disable"])

    assert result.exit_code == 0, result.output
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is False


def test_disable_without_the_claude_cli_does_not_claim_success(env, monkeypatch):
    """`claude` gone means nothing could be listed, let alone removed."""
    CliRunner().invoke(gs_module.global_app, ["enable"])
    monkeypatch.setattr(gs_module, "_claude_cmd", lambda: None)

    result = CliRunner().invoke(gs_module.global_app, ["disable"])

    assert result.exit_code == 1, result.output
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf.get("global_scope") is not False


# ---------------------------------------------------------------------------
# Ownership survives a change in how `claude mcp get` renders the args
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args_line",
    [
        "mcp",  # today's rendering
        '["mcp"]',  # JSON array
        '"mcp"',  # quoted scalar
        " mcp ",  # padded
        None,  # line absent — folded into Command:
    ],
)
def test_args_rendering_variants_all_read_as_ours(args_line):
    """`mcp get` has no `--json`, so this is screen-scraping. An exact
    `== "mcp"` test would flip EVERY enrolled machine to `foreign` the day
    `claude` changes the rendering — convergence reports `skipped`, `status`
    reports `drifted`, and `disable` declines to remove its own entry
    (Devin on #1184)."""
    assert gs_module._args_are_mcp(args_line) is True


@pytest.mark.parametrize("args_line", ["serve", "mcp --port 9", '["mcp","--port"]', "mcp serve"])
def test_a_genuinely_different_invocation_is_still_foreign(args_line):
    """Tolerant about rendering, not about what is being run."""
    assert gs_module._args_are_mcp(args_line) is False


def test_no_hook_removes_a_previously_installed_hook(env):
    """`run_convergence` converges — `--no-hook` is a declared state, not an
    abstention. Treating it as "do not install" left the updater firing in
    every repository while the config said `global_hook: false` and `status`
    reported it `ok`, with `agnes global disable` (which tears down the whole
    layer) as the only way to stop it (Devin on #1184)."""
    CliRunner().invoke(gs_module.global_app, ["enable"])
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert cfg.get("hooks", {}).get("SessionStart"), "precondition: the first enable installed the hook"

    result = CliRunner().invoke(gs_module.global_app, ["enable", "--no-hook"])

    assert result.exit_code == 0, result.output
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert not cfg.get("hooks", {}).get("SessionStart"), "the opted-out hook is still installed"


def test_no_hook_is_quiet_when_there_was_no_hook(env):
    """The other half — nothing installed, nothing to report as removed."""
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--no-hook"])
    assert result.exit_code == 0, result.output
    assert "removed the previously installed hook" not in result.output


# ---------------------------------------------------------------------------
# enable/disable hold the same lock `agnes update` holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [["enable"], ["disable"]])
def test_a_held_convergence_lock_refuses_instead_of_no_opping(env, cmd):
    """`agnes update` treats a held lock as skip-quietly, which is right for a
    background convergence fired from every repository. For a hand-typed
    command it is not: a silent no-op looks like it worked. Both mutate the
    same user-level files, so both take the lock (Devin on #1184)."""
    from filelock import FileLock

    from cli.config import _config_dir

    lock_file = _config_dir() / "update.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    holder = FileLock(str(lock_file))
    holder.acquire(timeout=0)
    try:
        result = CliRunner().invoke(gs_module.global_app, cmd)
    finally:
        holder.release()

    assert result.exit_code == 1, result.output
    assert "convergence lock" in result.output


def test_the_lock_is_released_so_a_second_run_succeeds(env):
    """A CLI that leaked the lock would make the NEXT run refuse — the shape
    a bare `__enter__()` without its `__exit__` produces."""
    assert CliRunner().invoke(gs_module.global_app, ["enable"]).exit_code == 0
    second = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert second.exit_code == 0, second.output
    assert "convergence lock" not in second.output


def test_enable_does_not_claim_success_when_a_stage_failed(env):
    """`run_convergence` records failures instead of raising, so the per-stage
    `error` rows have to reach the exit code — otherwise a half-installed
    layer exits 0 under "Global layer enabled" and the engineer finds out
    later, when the data tools are not there (Devin on #1184)."""
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "add"),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="nope"),
        ),
    )

    result = CliRunner().invoke(gs_module.global_app, ["enable"])

    assert result.exit_code == 1, result.output
    assert "PARTIALLY enabled" in result.output
    # The flag IS written — that is what keeps `agnes update` retrying the
    # pieces that failed. Only the claim of success is withheld.
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is True


def test_enable_reports_success_when_every_stage_worked(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0, result.output
    assert "Global layer enabled" in result.output
    assert "PARTIALLY" not in result.output


def test_global_is_a_maintenance_command(env, monkeypatch):
    """`agnes global` holds the same lock `agnes update` does, so the root
    callback must not spawn the detached updater ahead of it — that child
    would take the lock and the typed command would abort with "retry in a
    moment" on the first run after every release (Devin on #1184)."""
    import sys

    from cli.main import _MAINTENANCE_COMMANDS, _is_maintenance_command

    assert "global" in _MAINTENANCE_COMMANDS

    # It reads `sys.argv` directly — the root callback fires before parsing.
    for argv, expected in (
        (["agnes", "global", "enable"], True),
        (["agnes", "--json", "global", "status"], True),
        (["agnes", "query", "SELECT 1"], False),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        assert _is_maintenance_command() is expected, argv


# ---------------------------------------------------------------------------
# "the probe did not answer" is not "there is no entry"
# ---------------------------------------------------------------------------


def test_missing_server_exits_nonzero_and_reads_as_absent(monkeypatch, env):
    """The one non-zero exit that IS an answer.

    `claude mcp get <name>` exits 1 for a name it does not know, printing
    `No MCP server named "<name>".` — verified against `claude` 2.1.220. That
    is a real answer and must stay `absent`, or convergence would treat every
    first-ever enable as a failed probe.
    """
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout='No MCP server named "agnes". Configured servers: foo, bar\n',
            stderr="",
        ),
    )
    assert gs_module._mcp_entry_info() == ("absent", None)


def test_other_nonzero_exit_reads_as_unknown_not_absent(monkeypatch, env):
    """Any other non-zero exit is a failed probe, not an answer.

    `mcp get` connects to the server rather than only reading config, so a
    registered entry whose server fails to start exits non-zero too. Calling
    that `absent` is what let `disable` report "no entry" for an entry it had
    simply failed to see.
    """
    monkeypatch.setattr(
        gs_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: connection refused\n"
        ),
    )
    assert gs_module._mcp_entry_info() == ("unknown", None)


def test_probe_timeout_reads_as_unknown(monkeypatch, env):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude mcp get agnes", timeout=1)

    monkeypatch.setattr(gs_module.subprocess, "run", _boom)
    assert gs_module._mcp_entry_info() == ("unknown", None)


def test_probe_is_bounded_by_a_timeout(monkeypatch, env):
    """Unbounded, this blocks the convergence lock from every session start."""
    seen = {}

    def _capture(*a, **k):
        seen.update(k)
        return subprocess.CompletedProcess(args=[], returncode=1, stdout='No MCP server named "agnes".', stderr="")

    monkeypatch.setattr(gs_module.subprocess, "run", _capture)
    gs_module._mcp_entry_info()
    assert seen.get("timeout"), "`claude mcp get` must carry a timeout on the SessionStart-hook path"


def test_claude_missing_reads_as_unknown(monkeypatch, env):
    """No CLI means no answer — the same distinction, from the other end."""
    monkeypatch.setattr(gs_module, "_claude_cmd", lambda: None)
    assert gs_module._mcp_entry_info() == ("unknown", None)


def test_disable_does_not_flip_the_flag_when_the_probe_did_not_answer(env, monkeypatch):
    """The hazard the flag-last ordering exists to prevent.

    An unanswered probe reported as "no entry" adds nothing to `left_behind`,
    so the off-flag gets written while a registered user-scope entry keeps
    loading in every repository the user opens — and with the layer marked
    off, `agnes update` stops converging it, so nothing would ever remove it.
    """
    CliRunner().invoke(gs_module.global_app, ["enable"])
    monkeypatch.setattr(gs_module, "_mcp_entry_state", lambda: "unknown")

    result = CliRunner().invoke(gs_module.global_app, ["disable"])

    assert result.exit_code == 1, result.output
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf.get("global_scope") is not False


def test_status_does_not_traceback_on_an_unknown_probe(env, monkeypatch):
    """`agnes global status` reports what it saw; it must not raise."""
    monkeypatch.setattr(gs_module, "_mcp_entry_info", lambda: ("unknown", None))
    result = CliRunner().invoke(gs_module.global_app, ["status", "--json"])
    assert result.exit_code in (0, 1), result.output
    doc = json.loads(result.stdout)
    mcp = next(a for a in doc["artifacts"] if a["artifact"] == "mcp")
    assert mcp["state"] == "unknown"


def test_an_unclassifiable_entry_is_not_replaced_without_force(monkeypatch, env):
    """The `--force` guard must hold wherever ownership is in doubt.

    `run_convergence` dispatches `ours` → ok, `foreign` without force →
    skipped, everything else → remove + add against user scope. So any state
    meaning "we could not establish ownership" that is not routed away from
    that arm destroys a user's own `agnes` entry — precisely what `--force`
    exists to gate. Both such states are checked here.
    """
    for state in ("unknown", "unclassified"):
        rec = env["rec"]
        before = len(rec.calls)
        monkeypatch.setattr(gs_module, "_mcp_entry_info", lambda s=state: (s, None))
        CliRunner().invoke(gs_module.global_app, ["enable"])
        touched = [c for c in rec.calls[before:] if c[1:3] in (["mcp", "remove"], ["mcp", "add"])]
        assert not touched, f"state {state!r} reached the replace arm: {touched}"


def test_force_still_replaces_an_unclassifiable_entry(monkeypatch, env):
    """The other half — `--force` is how an operator says "do it anyway"."""
    monkeypatch.setattr(gs_module, "_mcp_entry_info", lambda: ("unclassified", "/usr/bin/other"))
    rec = env["rec"]
    before = len(rec.calls)
    CliRunner().invoke(gs_module.global_app, ["enable", "--force"])
    adds = [c for c in rec.calls[before:] if c[1:3] == ["mcp", "add"]]
    assert adds, "--force must still replace an entry whose ownership could not be established"


def test_a_drift_check_marks_the_clone_fresh(env):
    """Without this the six-hour interval is not an interval at all.

    `FETCH_HEAD` only moves on a real `git fetch`, and the drift-check path
    uses `git ls-remote`, which writes nothing. So on a clone that is simply
    up to date the mtime never advances, `_clone_is_stale` stays True forever,
    and the remote gets probed on every convergence — every session start in
    every repository, on top of the probe `_step_marketplace` already does in
    the same `agnes update` run.
    """
    import cli.commands.refresh_marketplace as rm_module

    clone = rm_module.CLONE_DIR
    marker = clone / ".git" / gs_module._FRESHNESS_MARKER
    # Age the clone past the interval so the check is reached.
    old = time.time() - (gs_module._CLONE_FRESH_SECONDS + 60)
    for name in ("FETCH_HEAD", "HEAD"):
        p = clone / ".git" / name
        p.write_text("x", encoding="utf-8")
        os.utime(p, (old, old))

    assert gs_module._clone_is_stale(clone) is True, "precondition: the clone reads as stale"

    CliRunner().invoke(gs_module.global_app, ["enable"])

    assert marker.exists(), "the drift check must stamp a freshness marker"
    assert gs_module._clone_is_stale(clone) is False, (
        "a clone checked seconds ago must not read as stale — otherwise every "
        "convergence re-probes the remote"
    )
