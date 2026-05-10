"""Tests for `agnes capture-session` — SessionStart hook helper."""

import json

from typer.testing import CliRunner

from cli.commands.capture_session import capture_session_app
from cli.lib.private_list import add_private
from cli.lib.session_queue import queue_path

runner = CliRunner()


def test_capture_appends_session_id_and_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({
        "session_id": "abc-123",
        "transcript_path": "/Users/foo/.claude/projects/.../abc.jsonl",
        "cwd": "/Users/foo/work",
        "hook_event_name": "SessionStart",
    })
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    assert queue_path(tmp_path).read_text(encoding="utf-8") == \
        "abc-123\t/Users/foo/.claude/projects/.../abc.jsonl\n"


def test_capture_writes_empty_session_id_when_field_missing(tmp_path, monkeypatch):
    """Payload missing session_id: still write to queue (legacy behavior).
    The empty session_id means the entry cannot be marked private later
    via /agnes-private (which keys on session_id), but it'll still upload."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"transcript_path": "/abc.jsonl"})
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "\t/abc.jsonl\n"


def test_capture_silently_skips_when_field_missing(tmp_path, monkeypatch):
    """transcript_path is required; absence is a no-op."""
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
    payload = json.dumps({"transcript_path": 123})
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
    p1 = json.dumps({"session_id": "abc", "transcript_path": "/abc.jsonl"})
    p2 = json.dumps({"session_id": "abc", "transcript_path": "/abc.jsonl"})
    runner.invoke(capture_session_app, [], input=p1)
    runner.invoke(capture_session_app, [], input=p2)
    assert queue_path(tmp_path).read_text(encoding="utf-8") == (
        "abc\t/abc.jsonl\n"
        "abc\t/abc.jsonl\n"
    )


def test_capture_verbose_emits_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"session_id": "abc", "transcript_path": "/x.jsonl"})
    result = runner.invoke(capture_session_app, ["--verbose"], input=payload)
    assert result.exit_code == 0
    assert "/x.jsonl" in result.output


def test_capture_skips_queue_when_session_is_already_private(tmp_path, monkeypatch):
    """Race scenario: user ran /agnes-private BEFORE the SessionStart hook
    chain reached capture-session. The private list write happened first;
    capture-session sees it and skips the queue append."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    add_private(tmp_path, "abc-123")
    payload = json.dumps({
        "session_id": "abc-123",
        "transcript_path": "/abc.jsonl",
    })
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    # Queue was NOT written.
    assert not queue_path(tmp_path).exists()


def test_capture_writes_when_unrelated_session_is_private(tmp_path, monkeypatch):
    """Marking session X private must not block capture of unrelated session Y."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    add_private(tmp_path, "other-session")
    payload = json.dumps({
        "session_id": "abc-123",
        "transcript_path": "/abc.jsonl",
    })
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "abc-123\t/abc.jsonl\n"
