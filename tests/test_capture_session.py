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


# ---------- Breadcrumb tests (David #11 from PR review) ---------------------
#
# capture-session is a SessionStart hook that always exits 0 (it must NOT
# fail loudly inside Claude Code's startup chain). Without an external
# observability signal, an upstream contract change (Claude Code's stdin
# JSON shape shifts; the queue mysteriously stays empty) is invisible to
# operators. The breadcrumb log gives `agnes diagnose` something to
# inspect.

from cli.commands.capture_session import _BREADCRUMB_FILENAME


def _breadcrumb_lines(workspace) -> list[str]:
    """Read all breadcrumb lines, dropping trailing newline."""
    path = workspace / ".claude" / _BREADCRUMB_FILENAME
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_capture_writes_ok_breadcrumb_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    payload = json.dumps({"session_id": "abc-123", "transcript_path": "/abc.jsonl"})
    runner.invoke(capture_session_app, [], input=payload)
    lines = _breadcrumb_lines(tmp_path)
    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert parts[1] == "ok"
    assert parts[2] == "abc-123"


def test_capture_writes_bad_json_breadcrumb_on_invalid_input(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Pre-create .claude/ so breadcrumb has somewhere to land (the breadcrumb
    # is read-only re: dir creation — see _record_breadcrumb).
    (tmp_path / ".claude").mkdir()
    runner.invoke(capture_session_app, [], input="not json at all")
    lines = _breadcrumb_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0].split("\t")[1] == "bad_json"


def test_capture_writes_no_transcript_breadcrumb_when_field_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    runner.invoke(capture_session_app, [], input=json.dumps({"session_id": "x"}))
    lines = _breadcrumb_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0].split("\t")[1] == "no_transcript_path"


def test_capture_writes_private_skip_breadcrumb_on_marked_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    add_private(tmp_path, "private-sid")
    runner.invoke(
        capture_session_app, [],
        input=json.dumps({"session_id": "private-sid", "transcript_path": "/x"}),
    )
    lines = _breadcrumb_lines(tmp_path)
    assert lines[-1].split("\t")[1] == "private_skip"
    assert lines[-1].split("\t")[2] == "private-sid"


def test_breadcrumb_does_not_create_claude_dir_in_arbitrary_workspaces(tmp_path, monkeypatch):
    """If the workspace has no .claude/ directory, capture-session
    never materializes one — same rationale as the read-only path in
    private_list.py. Hooks fire in directories the user just opened
    Claude Code in; the breadcrumb log shouldn't pollute those."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    runner.invoke(capture_session_app, [], input="malformed")
    # No .claude/ created, no breadcrumb written.
    assert not (tmp_path / ".claude").exists()
