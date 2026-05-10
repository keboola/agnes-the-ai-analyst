"""Tests for agnes push command (SessionEnd uploader)."""

import json
import re
from contextlib import contextmanager
from pathlib import Path

from typer.testing import CliRunner

from cli.commands.push import push_app
from cli.lib.session_queue import (
    append_to_queue,
    queue_path,
    uploaded_log_path,
)

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def _stub_config(monkeypatch) -> None:
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")


def _record_uploads(monkeypatch) -> list[tuple[str, dict]]:
    """Patch api_post to record calls and return success. Returns the recorder list."""
    calls: list[tuple[str, dict]] = []

    def _fake(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


# ---------- Existing tests (preserved) ---------------------------------------


def test_push_help():
    result = runner.invoke(push_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in _clean(result.output)
    assert "--json" in _clean(result.output)
    assert "--dry-run" in _clean(result.output)
    assert "--legacy-scan" in _clean(result.output)


def test_push_no_sessions_no_mkdir(tmp_path, monkeypatch):
    """Empty workspace -> push exits 0, doesn't create user/sessions/."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert not (tmp_path / "user" / "sessions").exists(), \
        "lazy mkdir: nothing to upload must not create user/sessions/"


def test_push_dry_run_no_writes(tmp_path, monkeypatch):
    """--dry-run lists what would upload but sends nothing."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    # Create a queued session
    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, str(transcript))

    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
    # Queue not consumed by dry-run
    assert queue_path(tmp_path).exists()


# ---------- New tests for queue + lock + recovery ---------------------------


def test_push_uploads_queued_session_and_clears_queue(tmp_path, monkeypatch):
    """Happy path: queue has one session, push uploads it, queue cleared, log written."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    # Exactly one upload to /api/upload/sessions
    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1

    # Queue is gone (snapshot consumed and discarded)
    assert not queue_path(tmp_path).exists()

    # Uploaded log has one entry mentioning the path
    log = uploaded_log_path(tmp_path).read_text(encoding="utf-8")
    assert str(transcript) in log
    assert "\t" in log  # TSV separator


def test_push_dedups_duplicate_paths_in_queue(tmp_path, monkeypatch):
    """Resume scenario: same path queued twice — push uploads once."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, str(transcript))
    append_to_queue(tmp_path, str(transcript))  # resume duplicate

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1, "duplicates within one push should collapse"


def test_push_silent_exit_when_lock_held(tmp_path, monkeypatch):
    """Concurrent SessionEnd hooks: only one push runs, others silent-exit."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    # Simulate "another push holds the lock" by patching acquire_or_skip to
    # yield None (the contract for "couldn't acquire").
    @contextmanager
    def _yield_none(workspace):
        yield None

    monkeypatch.setattr("cli.commands.push.acquire_or_skip", _yield_none)

    # api_post must NOT be called when lock unavailable.
    def _raise(*a, **kw):
        raise AssertionError("api_post called when lock unavailable")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert result.output == ""  # silent

    # Queue is preserved — the other push will consume it.
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"{transcript}\n"


def test_push_processes_recovery_snapshot_first(tmp_path, monkeypatch):
    """Pre-existing snapshot from a crashed push gets picked up."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    # Pre-existing snapshot (simulating prior crash)
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    recovery = claude / "agnes-sessions.snapshot.99999.txt"
    crashed_jsonl = tmp_path / "crashed.jsonl"
    crashed_jsonl.write_text("{}\n")
    recovery.write_text(f"{crashed_jsonl}\n", encoding="utf-8")

    # And a fresh queue entry
    fresh_jsonl = tmp_path / "fresh.jsonl"
    fresh_jsonl.write_text("{}\n")
    append_to_queue(tmp_path, str(fresh_jsonl))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 2
    assert not recovery.exists(), "recovery snapshot should be discarded after processing"
    assert not queue_path(tmp_path).exists(), "fresh queue should be consumed"


def test_push_skips_stale_queue_entry(tmp_path, monkeypatch):
    """Queue entry pointing to a deleted file: skipped, not retried forever."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    def _raise(*a, **kw):
        raise AssertionError("api_post should not be called for missing file")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    append_to_queue(tmp_path, str(tmp_path / "ghost.jsonl"))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    # Queue consumed; stale entry NOT requeued (would loop)
    assert not queue_path(tmp_path).exists()
    # Uploaded log not touched (nothing actually uploaded)
    assert not uploaded_log_path(tmp_path).exists()


def test_push_requeues_failed_uploads(tmp_path, monkeypatch):
    """Server returns 500 → path stays in queue for next push."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    def _fail(*a, **kw):
        return _FakeResp(500)

    monkeypatch.setattr("cli.commands.push.api_post", _fail)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, str(transcript))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    # Failed path requeued for retry
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"{transcript}\n"
    # Not in uploaded log
    assert not uploaded_log_path(tmp_path).exists()


def test_push_uploads_local_md(tmp_path, monkeypatch):
    """CLAUDE.local.md uploaded when present, regardless of session queue state."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.local.md").write_text("notes")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    md_calls = [c for c in calls if c[0] == "/api/upload/local-md"]
    assert len(md_calls) == 1


def test_push_json_output(tmp_path, monkeypatch):
    """--json emits a single JSON object with results."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    _record_uploads(monkeypatch)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, str(transcript))

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["sessions"] == 1
    assert data["errors"] == []
