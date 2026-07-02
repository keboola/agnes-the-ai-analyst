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
