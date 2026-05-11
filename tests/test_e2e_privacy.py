"""End-to-end test pinning the user-visible privacy promise:
**a session marked /agnes-private never reaches the server.**

Each unit test (capture_session, mark_private, push, private_list) covers
its own component edge cases. This file wires them together against a
recording fake `api_post` to verify the cross-component contract — closes
ZdenekSrotyr S2.9 in PR #242.

The test exercises both race orderings:
- mark BEFORE capture (capture-session sees the private flag and skips
  the queue write — entry never enters the upload pipeline)
- capture BEFORE mark (entry sits in the queue; push's per-entry
  re-check against the private list filters it out and audit-logs)
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from cli.commands.capture_session import capture_session_app
from cli.commands.mark_private import mark_private_app
from cli.commands.push import push_app
from cli.lib.session_queue import (
    failed_log_path,
    private_skipped_log_path,
    queue_path,
    uploaded_log_path,
)


runner = CliRunner()


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def _record_uploads(monkeypatch) -> list[tuple[str, dict]]:
    """Patch api_post to record every call. Sessions endpoint succeeds with
    200; local-md endpoint also succeeds. Returns the recorder list."""
    calls: list[tuple[str, dict]] = []

    def _fake(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


def _stub_push_config(monkeypatch) -> None:
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")


def _capture(workspace, session_id, transcript_path, monkeypatch) -> None:
    """Simulate a SessionStart hook firing `agnes capture-session` with the
    real CC stdin payload shape."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "cwd": str(workspace),
        "hook_event_name": "SessionStart",
    })
    result = runner.invoke(capture_session_app, [], input=payload)
    assert result.exit_code == 0, result.output


def _mark_private(workspace, session_id, monkeypatch) -> None:
    """Simulate the user typing `/agnes-private` inside Claude Code —
    `!`-prefix bash spawns `agnes mark-private` with CLAUDE_CODE_SESSION_ID
    set to the active session."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0, result.output


def _uploaded_session_names(calls: list[tuple[str, dict]]) -> list[str]:
    """Extract the `name` field from every recorded /api/upload/sessions
    multipart upload. Returns the list of jsonl filenames that reached
    the (fake) server."""
    out: list[str] = []
    for endpoint, kwargs in calls:
        if endpoint != "/api/upload/sessions":
            continue
        files = kwargs.get("files", {})
        file_field = files.get("file")
        if file_field is None:
            continue
        # `files={"file": (name, fh)}` — `_upload_one` in push.py
        name = file_field[0] if isinstance(file_field, tuple) else None
        if name is not None:
            out.append(name)
    return out


def test_e2e_mark_before_capture_session_never_uploads(tmp_path, monkeypatch):
    """User marks /agnes-private *before* the SessionStart hook runs
    capture-session. The capture-session call sees the session_id is on
    the private list and skips the queue write entirely — the upload
    pipeline never even sees it."""
    _stub_push_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    private_id = "private-session"
    private_transcript = tmp_path / "private.jsonl"
    private_transcript.write_text("{}\n")

    # Step 1: user runs /agnes-private (e.g. typed it before any
    # SessionStart hook reached capture-session).
    _mark_private(tmp_path, private_id, monkeypatch)

    # Step 2: SessionStart hook fires capture-session. It must see the
    # private flag and skip the queue write.
    _capture(tmp_path, private_id, private_transcript, monkeypatch)

    # Queue must be empty / absent — capture-session bailed before write.
    assert not queue_path(tmp_path).exists() or \
        queue_path(tmp_path).read_text(encoding="utf-8") == ""

    # Step 3: push runs (e.g. SessionEnd fires it).
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output

    # Assert: nothing reached the (fake) server.
    assert _uploaded_session_names(calls) == [], (
        f"Private session must not upload; got: {_uploaded_session_names(calls)}"
    )
    # And no uploaded-log entry was written.
    assert not uploaded_log_path(tmp_path).exists()


def test_e2e_capture_before_mark_filters_at_push(tmp_path, monkeypatch):
    """User marks /agnes-private *after* capture-session has already
    queued the transcript (typical scenario — user types the slash
    command mid-session). The entry sits in the queue; push's per-entry
    re-check against the private list filters it out at the last moment
    and audit-logs the skip."""
    _stub_push_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    private_id = "private-session"
    private_transcript = tmp_path / "private.jsonl"
    private_transcript.write_text("{}\n")
    public_id = "public-session"
    public_transcript = tmp_path / "public.jsonl"
    public_transcript.write_text("{}\n")

    # Step 1: SessionStart hooks fire capture-session for BOTH sessions.
    # Neither is private yet, so both land in the queue.
    _capture(tmp_path, private_id, private_transcript, monkeypatch)
    _capture(tmp_path, public_id, public_transcript, monkeypatch)
    # Sanity: queue has both entries.
    queue_lines = queue_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(queue_lines) == 2

    # Step 2: user marks one of them /agnes-private mid-session.
    _mark_private(tmp_path, private_id, monkeypatch)

    # Step 3: push runs.
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output

    # Assert: ONLY the public session reached the (fake) server.
    uploaded = _uploaded_session_names(calls)
    assert uploaded == ["public.jsonl"], (
        f"Only the non-private session should upload; got: {uploaded}"
    )

    # Audit log records the skipped private entry — the forensic trail.
    skipped_log = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert private_id in skipped_log
    assert "private.jsonl" in skipped_log

    # Failed log MUST be empty — private skip is not a failure, just
    # an intentional exclusion.
    assert not failed_log_path(tmp_path).exists()


def test_e2e_unmarked_sessions_upload_normally(tmp_path, monkeypatch):
    """Control case: with no /agnes-private call, both sessions upload.
    Proves the e2e tests above are exercising the privacy filter, not
    some unrelated reason the upload didn't fire."""
    _stub_push_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    t1 = tmp_path / "a.jsonl"
    t1.write_text("{}\n")
    t2 = tmp_path / "b.jsonl"
    t2.write_text("{}\n")

    _capture(tmp_path, "sid-a", t1, monkeypatch)
    _capture(tmp_path, "sid-b", t2, monkeypatch)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output

    uploaded = sorted(_uploaded_session_names(calls))
    assert uploaded == ["a.jsonl", "b.jsonl"], (
        f"Both unmarked sessions should upload; got: {uploaded}"
    )
    # No private-skipped audit entries either — nothing was marked private.
    assert not private_skipped_log_path(tmp_path).exists()
