"""Tests for `agnes update` — the convergence command (cli/commands/update.py)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import typer
from typer.testing import CliRunner

import cli.commands.update as upd
from cli.commands.update import update_app

runner = CliRunner()


# --- _resolve_workspace ---------------------------------------------------------


def test_resolve_workspace_prefers_agnes_local_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    assert upd._resolve_workspace() == tmp_path.resolve()


def test_resolve_workspace_falls_back_to_config_anchor(monkeypatch, tmp_path):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(upd, "get_workspace_root", lambda: str(tmp_path))
    assert upd._resolve_workspace() == tmp_path.resolve()


def test_resolve_workspace_none_when_uninitialised(monkeypatch, tmp_path):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(upd, "get_workspace_root", lambda: None)
    monkeypatch.chdir(tmp_path)  # bare dir, no .claude/
    assert upd._resolve_workspace() is None


def test_resolve_workspace_cwd_when_initialised(monkeypatch, tmp_path):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(upd, "get_workspace_root", lambda: None)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert upd._resolve_workspace() == tmp_path.resolve()


# --- _run_step isolation --------------------------------------------------------


def test_run_step_swallows_exception_into_report():
    report: list[dict] = []

    def boom():
        raise RuntimeError("kaboom")

    upd._run_step("demo", boom, report)  # must NOT raise
    assert report == [{"stage": "demo", "status": "error", "detail": "RuntimeError: kaboom"}]


def test_run_step_ignores_clean_typer_exit():
    report: list[dict] = []
    upd._run_step("demo", lambda: (_ for _ in ()).throw(typer.Exit(0)), report)
    assert report == []  # exit 0 is not an error


def test_run_step_records_nonzero_typer_exit():
    report: list[dict] = []
    upd._run_step("demo", lambda: (_ for _ in ()).throw(typer.Exit(1)), report)
    assert report == [{"stage": "demo", "status": "error", "detail": "exit_code=1"}]


# --- lock: single-runner --------------------------------------------------------


def test_update_exits_zero_when_lock_already_held(monkeypatch, tmp_path):
    """A second `agnes update` while one holds the lock must exit 0 and run no steps."""
    monkeypatch.setattr(upd, "_config_dir", lambda: tmp_path)

    @contextmanager
    def _held(_path):
        yield None  # lock unavailable

    monkeypatch.setattr("cli.lib.push_lock.acquire_path_or_skip", _held)

    called = {"cli": False}
    monkeypatch.setattr(upd, "_step_cli", lambda **k: called.__setitem__("cli", True))

    result = runner.invoke(update_app, [])
    assert result.exit_code == 0
    assert called["cli"] is False
    assert "already running" in result.output


# --- marketplace drift branching ------------------------------------------------


def test_marketplace_bootstraps_when_clone_missing(monkeypatch, tmp_path):
    import cli.commands.refresh_marketplace as rm
    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "nope")

    calls = []
    monkeypatch.setattr(rm, "refresh_marketplace",
                        lambda *, check, bootstrap: calls.append((check, bootstrap)))
    report: list[dict] = []
    upd._step_marketplace(report=report)
    assert calls == [(False, True)]
    assert report[0]["status"] == "bootstrapped"


def test_marketplace_reconciles_only_on_drift(monkeypatch, tmp_path):
    import cli.commands.refresh_marketplace as rm
    clone = tmp_path / "clone" / ".git"
    clone.mkdir(parents=True)
    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "clone")

    seq = []

    def fake_refresh(*, check, bootstrap):
        seq.append((check, bootstrap))
        if check:
            raise typer.Exit(rm._EXIT_MARKETPLACE_DRIFT)  # drift
        raise typer.Exit(0)

    monkeypatch.setattr(rm, "refresh_marketplace", fake_refresh)
    report: list[dict] = []
    upd._step_marketplace(report=report)
    assert seq == [(True, False), (False, False)]  # check, then full on drift
    assert report[0]["status"] == "reconciled"


def test_marketplace_skips_full_when_no_drift(monkeypatch, tmp_path):
    import cli.commands.refresh_marketplace as rm
    clone = tmp_path / "clone" / ".git"
    clone.mkdir(parents=True)
    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "clone")

    seq = []

    def fake_refresh(*, check, bootstrap):
        seq.append((check, bootstrap))
        raise typer.Exit(0)  # no drift

    monkeypatch.setattr(rm, "refresh_marketplace", fake_refresh)
    report: list[dict] = []
    upd._step_marketplace(report=report)
    assert seq == [(True, False)]  # check only
    assert report[0]["status"] == "ok"


# --- DEFAULT-mode end-to-end (no IWT) -------------------------------------------


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"content": self._content}


class _FakePull:
    errors: list = []
    tables_updated = 0
    parquets_total = 0


def test_default_mode_converges_and_writes_report(monkeypatch, tmp_path):
    """DEFAULT mode (no template): CLI no-op, CLAUDE.md backed-up-then-written,
    Agnes-owned reasserted, marketplace + pull run, report log written."""
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("OLD analyst-edited content\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))

    cfg = tmp_path / "cfg"
    monkeypatch.setattr(upd, "_config_dir", lambda: cfg)
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://server")
    monkeypatch.setattr(upd, "get_token", lambda: "tok")

    # Step 1: CLI already current.
    import cli.commands.self_upgrade as su
    monkeypatch.setattr(su, "_resolve_info", lambda force=False: None)

    # Step 2: DEFAULT (probe returns None = no IWT). welcome returns new content.
    import cli.lib.initial_workspace as iw
    monkeypatch.setattr(iw, "probe_status", lambda *a, **k: None)
    monkeypatch.setattr("cli.client.api_get", lambda *a, **k: _FakeResp("NEW server content\n"))

    # Step 3: Agnes-owned (no-op the writers).
    import cli.lib.hooks as hooks
    import cli.lib.commands as cmds
    monkeypatch.setattr(hooks, "install_claude_hooks", lambda ws: None)
    monkeypatch.setattr(cmds, "install_claude_commands", lambda ws: None)

    # Step 4: marketplace clone missing → bootstrap stub.
    import cli.commands.refresh_marketplace as rm
    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "no-clone")
    monkeypatch.setattr(rm, "refresh_marketplace", lambda *, check, bootstrap: None)

    # Step 5: pull stub.
    import cli.lib.pull as pull
    monkeypatch.setattr(pull, "run_pull", lambda *a, **k: _FakePull())

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output

    # CLAUDE.md overwritten with server content; old content preserved in a .bak.
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == "NEW server content\n"
    baks = list(workspace.glob("CLAUDE.md.bak.*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "OLD analyst-edited content\n"

    # Report log written with one JSON line covering all stages.
    log = workspace / ".claude" / "agnes" / "update.log"
    assert log.exists()
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    stages = {s["stage"] for s in entry["steps"]}
    assert {"cli", "workspace", "agnes-owned", "marketplace", "pull"} <= stages
    ws_step = next(s for s in entry["steps"] if s["stage"] == "workspace")
    assert ws_step["status"] == "refreshed"


# --- settings.json self-heal ----------------------------------------------------


def test_install_claude_hooks_self_heals_corrupt_settings(tmp_path):
    """A corrupt settings.json must be backed up and rebuilt (not skipped),
    so `agnes update`/`self-upgrade` can repair a mangled file."""
    from cli.lib.hooks import install_claude_hooks

    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings = ws / ".claude" / "settings.json"
    settings.write_text("{ this is : not valid json ", encoding="utf-8")

    install_claude_hooks(ws)

    baks = list((ws / ".claude").glob("settings.json.corrupt.*"))
    assert len(baks) == 1, "corrupt settings.json must be backed up"
    assert baks[0].read_text(encoding="utf-8") == "{ this is : not valid json "
    cfg = json.loads(settings.read_text(encoding="utf-8"))  # now valid JSON
    assert "SessionStart" in cfg.get("hooks", {})


# --- OVERRIDE-template convergence branch (_step_workspace) ---------------------


class _FakeUpdateResult:
    created = ["new/skill.md"]
    updated = ["docs/handbook.md"]
    backed_up = [("library/metrics.md", "library/metrics.md.bak.20260101T000000Z")]


def test_step_workspace_override_merges_on_sha_change(monkeypatch, tmp_path):
    """OVERRIDE mode, server template_sha newer than the sentinel → download +
    backup-aware merge, report 'merged', and refresh .claude/agnes/.env."""
    from cli.lib.initial_workspace import StatusInfo

    workspace = tmp_path / "ws"
    workspace.mkdir()
    status = StatusInfo(configured=True, synced=True, template_source="repo",
                        template_sha="newsha1234567890", synced_at="t", files=[])
    applied = {}
    env_calls = []

    def fake_apply(*a, **k):
        applied["called"] = True
        return _FakeUpdateResult()

    monkeypatch.setattr("cli.lib.initial_workspace.probe_status", lambda *a, **k: status)
    monkeypatch.setattr("src.initial_workspace.is_override_workspace", lambda ws: True)
    monkeypatch.setattr("cli.lib.override.read_override_metadata", lambda ws: {"template_sha": "OLDsha"})
    monkeypatch.setattr("cli.lib.initial_workspace.load_template_baseline", lambda ws: b"baseline-zip")
    monkeypatch.setattr("cli.lib.initial_workspace.download_zip", lambda *a, **k: b"new-zip")
    monkeypatch.setattr("cli.lib.initial_workspace.apply_update", fake_apply)
    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", lambda *a, **k: env_calls.append(True))

    report: list[dict] = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)

    ws_step = next(s for s in report if s["stage"] == "workspace")
    assert ws_step["status"] == "merged", report
    assert ws_step["detail"]["template_sha"] == "newsha1234"  # status.template_sha[:10]
    assert applied.get("called") is True
    assert env_calls, "write_agnes_env should be refreshed in override mode"


def test_step_workspace_override_skips_when_sha_matches(monkeypatch, tmp_path):
    """Sentinel template_sha == server SHA → no download/merge (cheap), but
    .env is still refreshed."""
    from cli.lib.initial_workspace import StatusInfo

    workspace = tmp_path / "ws"
    workspace.mkdir()
    status = StatusInfo(configured=True, synced=True, template_source="repo",
                        template_sha="samesha", synced_at="t", files=[])
    dl: list = []

    def must_not_apply(*a, **k):
        raise AssertionError("apply_update must not run when SHA matches")

    monkeypatch.setattr("cli.lib.initial_workspace.probe_status", lambda *a, **k: status)
    monkeypatch.setattr("src.initial_workspace.is_override_workspace", lambda ws: True)
    monkeypatch.setattr("cli.lib.override.read_override_metadata", lambda ws: {"template_sha": "samesha"})
    monkeypatch.setattr("cli.lib.initial_workspace.load_template_baseline", lambda ws: b"baseline")
    monkeypatch.setattr("cli.lib.initial_workspace.download_zip", lambda *a, **k: dl.append(True) or b"z")
    monkeypatch.setattr("cli.lib.initial_workspace.apply_update", must_not_apply)
    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", lambda *a, **k: None)

    report: list[dict] = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)
    ws_step = next(s for s in report if s["stage"] == "workspace")
    assert ws_step["status"] == "ok"
    assert dl == [], "no zip download when template SHA matches"


# --- chdir-failure guard --------------------------------------------------------


def test_update_skips_workspace_steps_when_chdir_fails(monkeypatch, tmp_path):
    """If the resolved workspace can't be entered, workspace-relative steps are
    skipped (never run from the launching cwd); the CLI step still runs."""
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setattr(upd, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://s")
    monkeypatch.setattr(upd, "get_token", lambda: "tok")

    cli_ran = []
    ran = []
    monkeypatch.setattr(upd, "_step_cli", lambda **k: cli_ran.append(True))
    monkeypatch.setattr(upd, "_step_workspace", lambda *a, **k: ran.append("workspace"))
    monkeypatch.setattr(upd, "_step_agnes_owned", lambda *a, **k: ran.append("agnes-owned"))
    monkeypatch.setattr(upd, "_step_marketplace", lambda *a, **k: ran.append("marketplace"))
    monkeypatch.setattr(upd, "_step_pull", lambda *a, **k: ran.append("pull"))

    def boom_chdir(_path):
        raise OSError("cannot enter")

    monkeypatch.setattr(upd.os, "chdir", boom_chdir)

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output
    assert cli_ran == [True], "CLI step must still run"
    assert ran == [], "workspace-relative steps must be skipped on chdir failure"
    entry = json.loads(result.output)
    assert any(s["stage"] == "workspace" and s["status"] == "error"
               and "cannot enter workspace" in s["detail"] for s in entry["steps"]), entry


# --- _step_cli updated / error branches -----------------------------------------


def _update_info():
    from cli.update_check import UpdateInfo
    return UpdateInfo(installed="2.0.0", latest="2.1.0",
                      download_url="http://s/cli/wheel/x.whl")


def test_step_cli_reports_updated_and_records_success(monkeypatch):
    """rc == 0 → status 'updated', record_outcome(success=True)."""
    import cli.commands.self_upgrade as su
    import cli.upgrade_status as us

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback",
                        lambda info, quiet=False: 0)
    outcomes: list[bool] = []
    monkeypatch.setattr(us, "record_outcome", lambda *, success: outcomes.append(success))

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)

    assert outcomes == [True]
    assert report == [{"stage": "cli", "status": "updated",
                       "detail": "2.0.0 -> 2.1.0 (active next run)"}]


def test_step_cli_reports_error_and_records_failure(monkeypatch):
    """rc != 0 → status 'error', record_outcome(success=False); never raises."""
    import cli.commands.self_upgrade as su
    import cli.upgrade_status as us

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback",
                        lambda info, quiet=False: 1)
    outcomes: list[bool] = []
    monkeypatch.setattr(us, "record_outcome", lambda *, success: outcomes.append(success))

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)  # must NOT raise

    assert outcomes == [False]
    assert report[0]["stage"] == "cli"
    assert report[0]["status"] == "error"


# --- _write_report rotation -----------------------------------------------------


def test_write_report_rotates_when_oversized(monkeypatch, tmp_path):
    """Past the cap, _write_report keeps the tail (last 200 lines) + the new
    entry, so update.log stays bounded over a workspace's lifetime."""
    monkeypatch.setattr(upd, "_REPORT_MAX_BYTES", 50)
    workspace = tmp_path / "ws"
    log = workspace / ".claude" / "agnes" / "update.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f'{{"old": {i}}}' for i in range(500)) + "\n",
                   encoding="utf-8")

    out = upd._write_report(workspace, {"ts": "newest"})

    assert out == log
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 201, len(lines)
    assert json.loads(lines[-1]) == {"ts": "newest"}


def test_write_report_returns_none_on_oserror(tmp_path):
    """A filesystem error degrades to None instead of raising (best-effort)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # .claude is a FILE, so mkdir(.claude/agnes) raises OSError → swallowed.
    (workspace / ".claude").write_text("not a dir", encoding="utf-8")
    assert upd._write_report(workspace, {"ts": "x"}) is None
