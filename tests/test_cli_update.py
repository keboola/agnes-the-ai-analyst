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
