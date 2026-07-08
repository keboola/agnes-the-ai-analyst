"""Tests for `agnes update` — the convergence command (cli/commands/update.py)."""

from __future__ import annotations

import json
from contextlib import contextmanager

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
    monkeypatch.setattr(rm, "refresh_marketplace", lambda *, check, bootstrap: calls.append((check, bootstrap)))
    report: list[dict] = []
    upd._step_marketplace(report=report)
    assert calls == [(False, True)]
    assert report[0]["status"] == "bootstrapped"


def test_step_marketplace_quiet_swallows_refresh_stdout(monkeypatch, tmp_path, capsys):
    """Under quiet (the --json / SessionStart-hook path), refresh_marketplace's
    progress output must NOT reach stdout — otherwise it corrupts the --json
    single-object contract."""
    import cli.commands.refresh_marketplace as rm

    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "no-clone")

    def noisy(*, check, bootstrap):
        print("MARKETPLACE PROGRESS NOISE")

    monkeypatch.setattr(rm, "refresh_marketplace", noisy)
    report: list[dict] = []
    upd._step_marketplace(report=report, quiet=True)
    out = capsys.readouterr().out
    assert "MARKETPLACE PROGRESS NOISE" not in out
    assert report[0]["stage"] == "marketplace"


def test_step_marketplace_prints_refresh_stdout_when_not_quiet(monkeypatch, tmp_path, capsys):
    """Interactive (non-quiet) runs still show refresh progress."""
    import cli.commands.refresh_marketplace as rm

    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "no-clone")

    def noisy(*, check, bootstrap):
        print("MARKETPLACE PROGRESS NOISE")

    monkeypatch.setattr(rm, "refresh_marketplace", noisy)
    report: list[dict] = []
    upd._step_marketplace(report=report, quiet=False)
    assert "MARKETPLACE PROGRESS NOISE" in capsys.readouterr().out


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
    # No local manifest to reassert from → the no-drift path stays a clean no-op.
    monkeypatch.setattr(rm, "_read_marketplace_plugin_versions", lambda: None)
    report: list[dict] = []
    upd._step_marketplace(report=report)
    assert seq == [(True, False)]  # check only
    assert report[0]["status"] == "ok"


def test_marketplace_reenables_plugins_when_settings_reset_no_drift(monkeypatch, tmp_path):
    # No marketplace drift, but the workspace settings.json lost its
    # enabledPlugins (step 2's template merge backed it up + rewrote it). The
    # no-drift path must STILL reassert them from the local manifest, so the
    # stack's installed plugins stay enabled in the workspace.
    import json

    import cli.commands.refresh_marketplace as rm

    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", clone)

    def fake_refresh(*, check, bootstrap):
        raise typer.Exit(0)  # no drift

    monkeypatch.setattr(rm, "refresh_marketplace", fake_refresh)
    monkeypatch.setattr(rm, "_read_marketplace_plugin_versions", lambda: {"flea": "1.0", "finance-common": "2.0"})

    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    # settings.json as left by the template merge: hooks/statusline, NO enabledPlugins.
    (ws / ".claude" / "settings.json").write_text(
        '{"hooks": {}, "statusLine": {"type": "command", "command": "agnes statusline"}}', encoding="utf-8"
    )
    monkeypatch.chdir(ws)  # _enable_plugins_in_workspace_settings writes to cwd/.claude/settings.json

    report: list[dict] = []
    upd._step_marketplace(report=report)

    assert report[0]["status"] == "enabled"
    cfg = json.loads((ws / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert cfg["enabledPlugins"] == {"flea@agnes": True, "finance-common@agnes": True}
    assert "hooks" in cfg and "statusLine" in cfg  # existing keys preserved


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


def test_update_backfills_workspace_root_for_legacy_workspace(monkeypatch, tmp_path):
    """`agnes update` (now the sole SessionStart entry) must backfill the
    `workspace_root` anchor that the retired `agnes self-upgrade` hook used to
    write — otherwise SessionEnd `agnes push --quiet` silently uploads nothing
    on legacy workspaces initialized before the config key existed."""
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "init-complete").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "cfg"))

    # CLI already current; no token → workspace steps skip. The backfill runs
    # before step 1 regardless of token.
    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: None)
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://server")
    monkeypatch.setattr(upd, "get_token", lambda: None)

    from cli.config import get_workspace_root

    assert get_workspace_root() is None  # precondition: anchor missing

    result = runner.invoke(update_app, ["--quiet"])
    assert result.exit_code == 0, result.output
    assert get_workspace_root() == str(workspace.resolve())  # backfilled


def test_json_output_is_single_object_despite_step_noise(monkeypatch, tmp_path):
    """`agnes update --json` must emit EXACTLY one JSON object on stdout, even
    when a step (marketplace reconcile) would otherwise print progress."""
    import typer as _typer

    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("OLD\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setattr(upd, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://server")
    monkeypatch.setattr(upd, "get_token", lambda: "tok")

    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: None)
    import cli.lib.initial_workspace as iw

    monkeypatch.setattr(iw, "probe_status", lambda *a, **k: None)
    monkeypatch.setattr("cli.client.api_get", lambda *a, **k: _FakeResp("NEW\n"))
    import cli.lib.hooks as hooks
    import cli.lib.commands as cmds

    monkeypatch.setattr(hooks, "install_claude_hooks", lambda ws: None)
    monkeypatch.setattr(cmds, "install_claude_commands", lambda ws: None)

    import cli.commands.refresh_marketplace as rm

    monkeypatch.setattr("cli.lib.marketplace.CLONE_DIR", tmp_path / "no-clone")
    monkeypatch.setattr(
        rm, "refresh_marketplace", lambda *, check, bootstrap: _typer.echo("NOISE that would break json.loads")
    )
    import cli.lib.pull as pull

    monkeypatch.setattr(pull, "run_pull", lambda *a, **k: _FakePull())

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output
    # Whole stdout parses as ONE json object — no leaked step noise.
    entry = json.loads(result.stdout.strip())
    assert "NOISE" not in result.stdout
    assert "marketplace" in {s["stage"] for s in entry["steps"]}


def test_update_exits_zero_when_config_dir_unwritable(monkeypatch):
    """A config dir that can't be created/accessed (read-only FS, permissions)
    fails BEFORE the lock guard. It must degrade to a config-error report and a
    clean exit, not a raw traceback out of the repair command."""

    def boom():
        raise OSError("read-only file system")

    monkeypatch.setattr(upd, "_config_dir", boom)
    ran: list[str] = []
    monkeypatch.setattr(upd, "_step_cli", lambda **k: ran.append("cli"))

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output
    entry = json.loads(result.stdout.strip())
    assert [(s["stage"], s["status"]) for s in entry["steps"]] == [("config", "error")]
    assert ran == []  # bailed before running any step


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
    status = StatusInfo(
        configured=True, synced=True, template_source="repo", template_sha="newsha1234567890", synced_at="t", files=[]
    )
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
    status = StatusInfo(
        configured=True, synced=True, template_source="repo", template_sha="samesha", synced_at="t", files=[]
    )
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


def _override_status_samesha(monkeypatch):
    """Wire OVERRIDE mode, SHA-match (no download/merge), so tests can focus on
    the .env refresh outcome."""
    from cli.lib.initial_workspace import StatusInfo

    status = StatusInfo(
        configured=True, synced=True, template_source="repo", template_sha="samesha", synced_at="t", files=[]
    )
    monkeypatch.setattr("cli.lib.initial_workspace.probe_status", lambda *a, **k: status)
    monkeypatch.setattr("src.initial_workspace.is_override_workspace", lambda ws: True)
    monkeypatch.setattr("cli.lib.override.read_override_metadata", lambda ws: {"template_sha": "samesha"})


def test_step_workspace_reports_env_write_failure(monkeypatch, tmp_path):
    """A raised write_agnes_env is a real failure — it must surface as an `env`
    error row, not be silently swallowed (the whole point of the report)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _override_status_samesha(monkeypatch)

    def boom_env(*a, **k):
        raise OSError("cannot write .env")

    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", boom_env)
    report: list[dict] = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)
    env_step = next(s for s in report if s["stage"] == "env")
    assert env_step["status"] == "error"
    assert "cannot write .env" in env_step["detail"]


def test_step_workspace_reports_env_write_ok(monkeypatch, tmp_path):
    """A successful write reports `env ok`; a soft None (older server / empty
    overlay) reports `skipped`, so a genuine failure stands out."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _override_status_samesha(monkeypatch)

    env_path = workspace / ".claude" / "agnes" / ".env"
    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", lambda *a, **k: env_path)
    report: list[dict] = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)
    env_step = next(s for s in report if s["stage"] == "env")
    assert env_step["status"] == "ok"
    assert ".env" in env_step["detail"]

    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", lambda *a, **k: None)
    report = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)
    env_step = next(s for s in report if s["stage"] == "env")
    assert env_step["status"] == "skipped"


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
    assert any(
        s["stage"] == "workspace" and s["status"] == "error" and "cannot enter workspace" in s["detail"]
        for s in entry["steps"]
    ), entry


# --- _step_cli updated / error branches -----------------------------------------


def _update_info():
    from cli.update_check import UpdateInfo

    return UpdateInfo(installed="2.0.0", latest="2.1.0", download_url="http://s/cli/wheel/x.whl")


# --- _step_token (#477) --------------------------------------------------------


def _make_token(days_left):
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone

    exp = datetime.now(timezone.utc) + timedelta(days=days_left)
    return pyjwt.encode({"email": "a@b.c", "exp": int(exp.timestamp())}, "s", algorithm="HS256")


def test_step_token_skipped_when_no_token():
    report: list[dict] = []
    upd._step_token(None, report)
    assert report == [{"stage": "token", "status": "skipped", "detail": "no token configured"}]


def test_step_token_ok_when_far_from_expiry(monkeypatch):
    monkeypatch.delenv("AGNES_TOKEN_RENEW_DAYS", raising=False)
    report: list[dict] = []
    upd._step_token(_make_token(60), report)
    assert report[0]["stage"] == "token"
    assert report[0]["status"] == "ok"
    assert "valid until" in report[0]["detail"]


def test_step_token_renew_soon_inside_window(monkeypatch):
    monkeypatch.delenv("AGNES_TOKEN_RENEW_DAYS", raising=False)
    report: list[dict] = []
    upd._step_token(_make_token(3), report)
    assert report == [{"stage": "token", "status": "renew-soon", "detail": report[0]["detail"]}]
    assert "valid until" in report[0]["detail"]


def test_step_token_ok_when_renew_disabled(monkeypatch):
    monkeypatch.setenv("AGNES_TOKEN_RENEW_DAYS", "0")
    report: list[dict] = []
    upd._step_token(_make_token(3), report)
    assert report[0]["status"] == "ok"  # nudge window disabled → never "renew-soon"


def test_step_cli_reports_updated(monkeypatch):
    """rc == 0 → status 'updated'. Outcome recording lives inside
    `_do_install_with_smoke_and_rollback` now, so `_step_cli` only shapes the
    report line."""
    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback", lambda info, quiet=False: 0)

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)

    assert report == [{"stage": "cli", "status": "updated", "detail": "2.0.0 -> 2.1.0 (active next run)"}]


def test_step_cli_reports_error(monkeypatch):
    """rc != 0 → status 'error'; never raises."""
    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback", lambda info, quiet=False: 1)

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)  # must NOT raise

    assert report[0]["stage"] == "cli"
    assert report[0]["status"] == "error"


def test_step_cli_defers_on_preflight(monkeypatch):
    """rc == _INSTALL_DEFERRED → status 'deferred'."""
    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback", lambda info, quiet=False: su._INSTALL_DEFERRED)

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)

    assert report[0]["stage"] == "cli"
    assert report[0]["status"] == "deferred"
    assert "2.0.0 -> 2.1.0" in report[0]["detail"]  # names the version it would install


def test_step_cli_reports_staged_with_version(monkeypatch):
    """rc == _INSTALL_STAGED (Windows deferred) → status 'staged', and the detail
    names the target version so the log says WHAT is being installed."""
    import cli.commands.self_upgrade as su

    monkeypatch.setattr(su, "_resolve_info", lambda force=False: _update_info())
    monkeypatch.setattr(su, "_do_install_with_smoke_and_rollback", lambda info, quiet=False: su._INSTALL_STAGED)

    report: list[dict] = []
    upd._step_cli(quiet=True, report=report)

    assert report[0]["stage"] == "cli"
    assert report[0]["status"] == "staged"
    assert "2.0.0 -> 2.1.0" in report[0]["detail"]


# --- _write_report rotation -----------------------------------------------------


def test_write_report_rotates_when_oversized(monkeypatch, tmp_path):
    """Past the cap, _write_report keeps the tail (last 200 lines) + the new
    entry, so update.log stays bounded over a workspace's lifetime."""
    monkeypatch.setattr(upd, "_REPORT_MAX_BYTES", 50)
    workspace = tmp_path / "ws"
    log = workspace / ".claude" / "agnes" / "update.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f'{{"old": {i}}}' for i in range(500)) + "\n", encoding="utf-8")

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


# --- OVERRIDE merge with NO stored baseline -------------------------------------


def test_step_workspace_override_merges_without_baseline(monkeypatch, tmp_path):
    """OVERRIDE mode with NO stored baseline (pre-baseline install, or a moved
    workspace whose baseline is filed under the old path) still merges on a SHA
    change — the 3-way engine backs up every changed file and apply_update()
    establishes the baseline. Previously this case was skipped."""
    from cli.lib.initial_workspace import StatusInfo

    workspace = tmp_path / "ws"
    workspace.mkdir()
    status = StatusInfo(
        configured=True, synced=True, template_source="repo", template_sha="newsha", synced_at="t", files=[]
    )
    applied = {}

    def fake_apply(*a, **k):
        applied["called"] = True
        return _FakeUpdateResult()

    monkeypatch.setattr("cli.lib.initial_workspace.probe_status", lambda *a, **k: status)
    monkeypatch.setattr("src.initial_workspace.is_override_workspace", lambda ws: True)
    monkeypatch.setattr("cli.lib.override.read_override_metadata", lambda ws: {"template_sha": "OLDsha"})
    monkeypatch.setattr("cli.lib.initial_workspace.load_template_baseline", lambda ws: None)  # no baseline
    monkeypatch.setattr("cli.lib.initial_workspace.download_zip", lambda *a, **k: b"new-zip")
    monkeypatch.setattr("cli.lib.initial_workspace.apply_update", fake_apply)
    monkeypatch.setattr("cli.lib.initial_workspace.write_agnes_env", lambda *a, **k: None)

    report: list[dict] = []
    upd._step_workspace(workspace, server_url="http://s", token="t", report=report)

    ws_step = next(s for s in report if s["stage"] == "workspace")
    assert ws_step["status"] == "merged", report
    assert applied.get("called") is True


# --- update() config-degradation guards -----------------------------------------


def test_update_skips_workspace_steps_when_token_missing(monkeypatch, tmp_path):
    """No saved token → the workspace-independent CLI step still runs, the
    workspace-relative steps are skipped with a note, and exit is clean."""
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setattr(upd, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://s")
    monkeypatch.setattr(upd, "get_token", lambda: None)

    cli_ran: list = []
    ws_ran: list = []
    monkeypatch.setattr(upd, "_step_cli", lambda **k: cli_ran.append(True))
    monkeypatch.setattr(upd, "_step_workspace", lambda *a, **k: ws_ran.append(True))

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output
    assert cli_ran == [True]
    assert ws_ran == []
    entry = json.loads(result.output)
    ws = next(s for s in entry["steps"] if s["stage"] == "workspace")
    assert ws["status"] == "skipped" and "no token" in ws["detail"]


def test_update_degrades_on_corrupt_config(monkeypatch, tmp_path):
    """A corrupt config (here: workspace-root read raises, like a malformed
    config.yaml) must not crash the repair command — the CLI step still runs,
    workspace steps are skipped, the failure is recorded, and exit is clean.
    Regression guard for the boundary that re-reads config in _resolve_workspace."""
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(upd, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(upd, "get_server_url", lambda: "http://s")
    monkeypatch.setattr(upd, "get_token", lambda: "tok")

    def boom():
        raise ValueError("mapping values are not allowed here")  # yaml-ish

    monkeypatch.setattr(upd, "get_workspace_root", boom)

    cli_ran: list = []
    ws_ran: list = []
    monkeypatch.setattr(upd, "_step_cli", lambda **k: cli_ran.append(True))
    monkeypatch.setattr(upd, "_step_workspace", lambda *a, **k: ws_ran.append(True))

    result = runner.invoke(update_app, ["--json"])
    assert result.exit_code == 0, result.output
    assert cli_ran == [True]
    assert ws_ran == []
    entry = json.loads(result.output)
    assert any(s["stage"] == "config" and s["status"] == "error" for s in entry["steps"]), entry
