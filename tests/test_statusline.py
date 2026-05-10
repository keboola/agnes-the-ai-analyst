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
