"""Tests for `agnes capture-session` — SessionStart hook helper."""

import json

from typer.testing import CliRunner

from cli.commands.capture_session import capture_session_app
from cli.lib.session_queue import queue_path

runner = CliRunner()


def test_capture_appends_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({
        "session_id": "abc",
        "transcript_path": "/Users/foo/.claude/projects/.../abc.jsonl",
        "cwd": "/Users/foo/work",
        "hook_event_name": "SessionStart",
    })
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    assert queue_path(tmp_path).read_text(encoding="utf-8") == \
        "/Users/foo/.claude/projects/.../abc.jsonl\n"


def test_capture_silently_skips_when_field_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(capture_session_app, [], input='{"session_id": "abc"}')
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()


def test_capture_silently_skips_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(capture_session_app, [], input="not json {")
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()


def test_capture_silently_skips_empty_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(capture_session_app, [], input="")
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()


def test_capture_silently_skips_non_string_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"transcript_path": 123})  # number, not string
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()


def test_capture_silently_skips_when_payload_not_object(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(capture_session_app, [], input='["array", "not object"]')
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()


def test_capture_appends_multiple_calls(tmp_path, monkeypatch):
    """Resume scenario: SessionStart fires again — same path appended twice."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    p1 = json.dumps({"transcript_path": "/abc.jsonl"})
    p2 = json.dumps({"transcript_path": "/abc.jsonl"})  # resume — same path
    runner.invoke(capture_session_app, [], input=p1)
    runner.invoke(capture_session_app, [], input=p2)
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "/abc.jsonl\n/abc.jsonl\n"


def test_capture_verbose_emits_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"transcript_path": "/x.jsonl"})
    result = runner.invoke(capture_session_app, ["--verbose"], input=payload)
    assert result.exit_code == 0
    # CliRunner merges stderr into output by default; just ensure path is mentioned
    assert "/x.jsonl" in result.output
