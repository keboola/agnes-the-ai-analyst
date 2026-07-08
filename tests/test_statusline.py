"""Tests for `agnes statusline` — Claude Code statusLine helper."""

import json

from typer.testing import CliRunner

from cli.commands.statusline import statusline_app
from cli.lib.private_list import add_private

runner = CliRunner()


def test_statusline_emits_private_indicator(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    add_private(tmp_path, "abc-123")
    payload = json.dumps({"session_id": "abc-123"})
    result = runner.invoke(statusline_app, [], input=payload)
    assert result.exit_code == 0
    assert "agnes-private" in result.output


def test_statusline_silent_for_non_private(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"session_id": "abc-123"})
    result = runner.invoke(statusline_app, [], input=payload)
    assert result.exit_code == 0
    assert "agnes-private" not in result.output


def test_statusline_silent_on_malformed_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(statusline_app, [], input="not json {")
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_silent_on_missing_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(statusline_app, [], input='{"foo": "bar"}')
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_silent_on_empty_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(statusline_app, [], input="")
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_silent_when_payload_not_object(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(statusline_app, [], input='["array"]')
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_steps_aside_during_windows_deferred_update(tmp_path, monkeypatch):
    # A fresh deferred-update sentinel makes the statusline yield (empty output)
    # EVEN for a private session — so the render isn't relaunching the tool venv
    # the Windows swap must replace. (`_IS_WINDOWS` forced so it runs on any CI.)
    import cli.commands.statusline as sl

    monkeypatch.setattr(sl, "_IS_WINDOWS", True)
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "deferred-update.active").write_text("2026-07-01T22:00:00")
    add_private(tmp_path, "abc-123")

    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_ignores_stale_deferred_update_sentinel(tmp_path, monkeypatch):
    # A stale sentinel (crashed helper that never cleaned up) must NOT wedge the
    # status bar — past the TTL the private indicator is emitted normally.
    import cli.commands.statusline as sl

    monkeypatch.setattr(sl, "_IS_WINDOWS", True)
    monkeypatch.setattr(sl, "_DEFERRED_UPDATE_TTL_S", 0.0)
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "deferred-update.active").write_text("stale")
    add_private(tmp_path, "abc-123")

    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert "agnes-private" in result.output


def test_statusline_sentinel_ignored_off_windows(tmp_path, monkeypatch):
    # Off-Windows the sentinel is irrelevant (POSIX upgrades in place) — private
    # indicator still shows even if a sentinel file happens to be present.
    import cli.commands.statusline as sl

    monkeypatch.setattr(sl, "_IS_WINDOWS", False)
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "deferred-update.active").write_text("2026-07-01T22:00:00")
    add_private(tmp_path, "abc-123")

    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert "agnes-private" in result.output


# --------------------------------------------------------------------------- #
# #744 — one-line "what changed" summary after an `agnes update` convergence
# --------------------------------------------------------------------------- #


def _write_update_log(workspace, entry):
    log = workspace / ".claude" / "agnes" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _entry(ts="20260707T120000Z", steps=None, agnes_version="0.73.0"):
    return {
        "ts": ts,
        "agnes_version": agnes_version,
        "workspace": None,
        "steps": steps if steps is not None else [],
    }


def test_statusline_shows_changed_summary_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _write_update_log(
        tmp_path,
        _entry(
            steps=[
                {"stage": "cli", "status": "ok", "detail": "already current / offline"},
                {"stage": "workspace", "status": "refreshed", "detail": "CLAUDE.md updated"},
                {"stage": "agnes-owned", "status": "ok", "detail": "hooks / statusline / commands reasserted"},
                {"stage": "pull", "status": "ok", "detail": "3 tables, 10 parquets"},
            ]
        ),
    )
    payload = json.dumps({"session_id": "abc-123"})

    first = runner.invoke(statusline_app, [], input=payload)
    assert first.exit_code == 0
    assert "workspace refreshed" in first.output
    assert first.output.strip().startswith("Agnes:")

    second = runner.invoke(statusline_app, [], input=payload)
    assert second.exit_code == 0
    assert second.output.strip() == ""


def test_statusline_all_ok_summary_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _write_update_log(
        tmp_path,
        _entry(
            steps=[
                {"stage": "cli", "status": "ok", "detail": "already current / offline"},
                {"stage": "workspace", "status": "ok", "detail": "CLAUDE.md already current"},
                {"stage": "workspace", "status": "skipped", "detail": "no token configured"},
                {"stage": "agnes-owned", "status": "ok", "detail": "hooks / statusline / commands reasserted"},
                {"stage": "marketplace", "status": "ok", "detail": "plugins already current"},
                {"stage": "pull", "status": "ok", "detail": "3 tables, 10 parquets"},
            ]
        ),
    )
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_missing_update_log_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_malformed_update_log_renders_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    log = tmp_path / ".claude" / "agnes" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("not { valid json at all\n", encoding="utf-8")
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_statusline_cli_phrase_active_next_session(tmp_path, monkeypatch):
    import cli.commands.statusline as sl

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(sl, "_running_version", lambda: "0.72.9")
    _write_update_log(
        tmp_path,
        _entry(
            steps=[
                {"stage": "cli", "status": "updated", "detail": "0.72.9 -> 0.73.0 (active next run)"},
            ]
        ),
    )
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert "CLI 0.72.9 -> 0.73.0 (active next session)" in result.output


def test_statusline_cli_phrase_windows_staged_failure(tmp_path, monkeypatch):
    import cli.commands.statusline as sl

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(sl, "_running_version", lambda: "0.72.9")
    (tmp_path / "upgrade_status.json").write_text(
        json.dumps({"last_attempt_ts": 9999999999.0, "last_outcome": "failure", "consecutive_failures": 1}),
        encoding="utf-8",
    )
    _write_update_log(
        tmp_path,
        _entry(
            steps=[
                {
                    "stage": "cli",
                    "status": "staged",
                    "detail": "0.72.9 -> 0.73.0 (windows deferred install; completes after this process exits)",
                },
            ]
        ),
    )
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert "CLI update failed" in result.output
    assert "active next session" not in result.output


def test_statusline_truncates_long_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    steps = [
        {
            "stage": "marketplace",
            "status": "enabled",
            "detail": f"re-enabled 1 stack plugin(s) in settings.json: some-very-long-plugin-name-{i}",
        }
        for i in range(10)
    ]
    _write_update_log(tmp_path, _entry(steps=steps))
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    line = result.output.strip()
    assert line
    assert "\n" not in line
    assert len(line) <= 80
    assert line.endswith("…")


def test_statusline_private_marker_takes_precedence_over_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    add_private(tmp_path, "abc-123")
    _write_update_log(
        tmp_path,
        _entry(steps=[{"stage": "workspace", "status": "refreshed", "detail": "CLAUDE.md updated"}]),
    )
    result = runner.invoke(statusline_app, [], input=json.dumps({"session_id": "abc-123"}))
    assert result.exit_code == 0
    assert "agnes-private" in result.output
    assert "Agnes:" not in result.output
