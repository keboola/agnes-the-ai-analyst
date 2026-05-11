"""Tests for agnes push command (SessionEnd uploader)."""

import json
import re
from contextlib import contextmanager

from typer.testing import CliRunner

from cli.commands.push import push_app
from cli.lib.private_list import add_private
from cli.lib.session_queue import (
    append_to_queue,
    failed_log_path,
    private_skipped_log_path,
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


# ---------- Smoke + dry-run --------------------------------------------------


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
    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, "sid-1", str(transcript))

    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
    assert queue_path(tmp_path).exists()  # not consumed


# ---------- Queue happy path + dedup + lock + recovery ----------------------


def test_push_uploads_queued_session_and_clears_queue(tmp_path, monkeypatch):
    """Happy path: queue has one session, push uploads it, queue cleared, log written."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1
    assert not queue_path(tmp_path).exists()
    log = uploaded_log_path(tmp_path).read_text(encoding="utf-8")
    assert str(transcript) in log
    assert "\t" in log


def test_push_dedups_duplicate_paths_in_queue(tmp_path, monkeypatch):
    """Resume scenario: same (session_id, path) queued twice — push uploads once."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    transcript = tmp_path / "abc.jsonl"
    transcript.write_text('{"event":"test"}\n')
    append_to_queue(tmp_path, "sid-1", str(transcript))
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1


def test_push_silent_exit_when_lock_held(tmp_path, monkeypatch):
    """Concurrent SessionEnd hooks: only one push runs, others silent-exit."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    @contextmanager
    def _yield_none(workspace):
        yield None

    monkeypatch.setattr("cli.commands.push.acquire_or_skip", _yield_none)

    def _raise(*a, **kw):
        raise AssertionError("api_post called when lock unavailable")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert result.output == ""
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"sid-1\t{transcript}\n"


def test_push_silent_exit_when_filelock_raises_oserror(tmp_path, monkeypatch):
    """OSError from filelock (read-only FS, permission denied, disk full)
    must not crash push with an unhandled traceback. Exercises the real
    acquire_or_skip by replacing it with a context manager that raises
    OSError on entry — simulates what filelock.FileLock.acquire raises
    on a read-only mount."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    # Queue setup must happen BEFORE we install the failing lock — the
    # `append_to_queue` path holds its own `agnes-queue.lock` and a
    # blanket `FileLock.acquire` patch would break that one too.
    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-1", str(transcript))

    # Wrap the real acquire_or_skip with one that raises OSError before
    # yielding. We can't just patch `cli.commands.push.acquire_or_skip`
    # because the new behaviour lives inside `acquire_or_skip` itself —
    # we have to exercise its except handler. So we patch `FileLock`
    # used inside push_lock: subclass with overridden `acquire` that
    # raises OSError.
    from cli.lib import push_lock as pl

    class _BrokenLock:
        def __init__(self, path: str) -> None:
            self._path = path

        def acquire(self, timeout: float = -1):
            raise OSError("read-only filesystem")

    monkeypatch.setattr(pl, "FileLock", _BrokenLock)

    def _api_should_not_be_called(*a, **kw):
        raise AssertionError("api_post called when lock acquisition raised OSError")

    monkeypatch.setattr("cli.commands.push.api_post", _api_should_not_be_called)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, f"push must exit 0 on OSError, got: {result.output}"
    # No traceback in output
    assert "Traceback" not in result.output
    # Queue preserved for next push attempt
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"sid-1\t{transcript}\n"


def test_push_processes_recovery_snapshot_first(tmp_path, monkeypatch):
    """Pre-existing snapshot from a crashed push gets picked up."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    recovery = claude / "agnes-sessions.snapshot.99999.txt"
    crashed_jsonl = tmp_path / "crashed.jsonl"
    crashed_jsonl.write_text("{}\n")
    recovery.write_text(f"sid-old\t{crashed_jsonl}\n", encoding="utf-8")

    fresh_jsonl = tmp_path / "fresh.jsonl"
    fresh_jsonl.write_text("{}\n")
    append_to_queue(tmp_path, "sid-new", str(fresh_jsonl))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 2
    assert not recovery.exists()
    assert not queue_path(tmp_path).exists()


def test_push_skips_stale_queue_entry(tmp_path, monkeypatch):
    """Queue entry pointing to a deleted file: skipped, not retried forever."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    def _raise(*a, **kw):
        raise AssertionError("api_post should not be called for missing file")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    append_to_queue(tmp_path, "sid-1", str(tmp_path / "ghost.jsonl"))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    assert not queue_path(tmp_path).exists()
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
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"sid-1\t{transcript}\n"
    assert not uploaded_log_path(tmp_path).exists()


def test_push_uploads_local_md(tmp_path, monkeypatch):
    """CLAUDE.local.md uploaded when present."""
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
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["sessions"] == 1
    assert data["errors"] == []
    assert data["private_skipped"] == 0


# ---------- Private filter tests --------------------------------------------


def test_push_skips_private_session_and_audit_logs(tmp_path, monkeypatch):
    """Queue contains a private session_id → no upload, audit log appended."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    transcript = tmp_path / "secret.jsonl"
    transcript.write_text("{}\n")
    add_private(tmp_path, "sid-private")
    append_to_queue(tmp_path, "sid-private", str(transcript))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert sessions_calls == [], "private session must NOT be uploaded"

    # Audit log entry written
    audit = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "sid-private" in audit
    assert str(transcript) in audit

    # Queue consumed (snapshot processed and discarded — private entry not requeued)
    assert not queue_path(tmp_path).exists()


def test_push_mixes_private_and_public_correctly(tmp_path, monkeypatch):
    """A push run with one private + one public session uploads only the public one."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    secret = tmp_path / "secret.jsonl"
    secret.write_text("{}\n")
    public = tmp_path / "public.jsonl"
    public.write_text("{}\n")

    add_private(tmp_path, "sid-secret")
    append_to_queue(tmp_path, "sid-secret", str(secret))
    append_to_queue(tmp_path, "sid-public", str(public))

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1

    audit = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "sid-secret" in audit
    assert "sid-public" not in audit


def test_push_dry_run_shows_private_skip(tmp_path, monkeypatch):
    """--dry-run preview reports private-skipped count separately."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    transcript = tmp_path / "secret.jsonl"
    transcript.write_text("{}\n")
    add_private(tmp_path, "sid-priv")
    append_to_queue(tmp_path, "sid-priv", str(transcript))

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
    assert "1 private session" in result.output
    assert "sid-priv" in result.output


# ---------- 4xx permanent-failure handling -----------------------------------


def _stub_api_post_status(monkeypatch, status: int) -> None:
    """Patch api_post to always return the given status code."""
    def _fixed(*a, **kw):
        return _FakeResp(status)
    monkeypatch.setattr("cli.commands.push.api_post", _fixed)


def test_push_drops_4xx_to_audit_log_not_requeue(tmp_path, monkeypatch):
    """4xx (here: 401 token expired) → drop + audit, no requeue.
    Closes the prior infinite-loop bug where every non-200 except
    `file not found on disk` was requeued forever."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    _stub_api_post_status(monkeypatch, 401)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    # Entry must NOT be in the live queue any more.
    assert not queue_path(tmp_path).exists() or \
        queue_path(tmp_path).read_text(encoding="utf-8") == ""
    # Audit log must record the drop with status + session_id + path.
    log = failed_log_path(tmp_path).read_text(encoding="utf-8")
    assert "\t401\t" in log
    assert "sid-1" in log
    assert str(transcript) in log


def test_push_drops_each_4xx_status(tmp_path, monkeypatch):
    """403, 413, 400 → all drop (not just 401)."""
    for status in (400, 403, 413):
        ws = tmp_path / f"ws-{status}"
        ws.mkdir()
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))
        _stub_config(monkeypatch)
        _stub_api_post_status(monkeypatch, status)
        transcript = ws / "x.jsonl"
        transcript.write_text("{}\n")
        append_to_queue(ws, f"sid-{status}", str(transcript))

        result = runner.invoke(push_app, [])
        assert result.exit_code == 0, (status, result.output)
        log = failed_log_path(ws).read_text(encoding="utf-8")
        assert f"\t{status}\t" in log, (status, log)


def test_push_requeues_408_and_429(tmp_path, monkeypatch):
    """408 Request Timeout + 429 Too Many Requests are transient per
    HTTP spec — server is asking us to retry, not telling us the
    request is invalid. Must requeue, not drop."""
    for status in (408, 429):
        ws = tmp_path / f"ws-{status}"
        ws.mkdir()
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))
        _stub_config(monkeypatch)
        _stub_api_post_status(monkeypatch, status)
        transcript = ws / "x.jsonl"
        transcript.write_text("{}\n")
        append_to_queue(ws, f"sid-{status}", str(transcript))

        result = runner.invoke(push_app, [])
        assert result.exit_code == 0
        # Requeued → entry back in live queue.
        live = queue_path(ws).read_text(encoding="utf-8")
        assert f"sid-{status}\t{transcript}\n" == live
        # NOT in the failed audit log.
        assert not failed_log_path(ws).exists()


def test_push_requeues_5xx(tmp_path, monkeypatch):
    """5xx is genuine server-side failure: request was valid but server
    couldn't honor it right now. Requeue for the next push."""
    for status in (500, 502, 503):
        ws = tmp_path / f"ws-{status}"
        ws.mkdir()
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))
        _stub_config(monkeypatch)
        _stub_api_post_status(monkeypatch, status)
        transcript = ws / "x.jsonl"
        transcript.write_text("{}\n")
        append_to_queue(ws, f"sid-{status}", str(transcript))

        result = runner.invoke(push_app, [])
        assert result.exit_code == 0
        live = queue_path(ws).read_text(encoding="utf-8")
        assert f"sid-{status}\t{transcript}\n" == live
        assert not failed_log_path(ws).exists()


def test_push_requeues_network_exception(tmp_path, monkeypatch):
    """Connection error / DNS / timeout — no status code from server.
    Treat as transient: requeue rather than drop."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    def _raise(*a, **kw):
        raise ConnectionError("server unreachable")
    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-net", str(transcript))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    live = queue_path(tmp_path).read_text(encoding="utf-8")
    assert f"sid-net\t{transcript}\n" == live
    assert not failed_log_path(tmp_path).exists()


def test_push_4xx_drop_count_in_json_output(tmp_path, monkeypatch):
    """--json surfaces the new `dropped_permanent` counter so operators
    can pipe it into monitoring / scripts."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    _stub_api_post_status(monkeypatch, 401)

    transcript = tmp_path / "x.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-1", str(transcript))

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dropped_permanent"] == 1
    assert payload["sessions"] == 0


def test_push_4xx_drop_visible_in_quiet_stdout(tmp_path, monkeypatch):
    """Non-quiet stdout mentions the audit-log path so operators tailing
    `agnes push` output get a pointer to the forensic trail."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    _stub_api_post_status(monkeypatch, 413)

    transcript = tmp_path / "huge.jsonl"
    transcript.write_text("{}\n")
    append_to_queue(tmp_path, "sid-big", str(transcript))

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    assert "agnes-sessions-failed.txt" in result.output
    assert "permanent failure" in result.output


# ---------- David #8: legacy-scan honors the private list -------------------
#
# `--legacy-scan` walks ~/.claude/projects/<encoded-cwd>/*.jsonl. Claude Code
# names jsonls `<session-id>.jsonl`, so the file stem IS the session id —
# the same private filter that protects queue uploads must apply. Without
# this, an operator running `agnes push --legacy-scan` to backfill old
# sessions would silently upload everything on disk.


def test_push_legacy_scan_skips_private_session(tmp_path, monkeypatch):
    """Legacy-scan picks up `<sid>.jsonl` from the projects dir; if the
    sid is on the private list, it must be skipped + audit-logged."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)
    calls = _record_uploads(monkeypatch)

    projects_dir = tmp_path / "projects-fake"
    projects_dir.mkdir()
    pub = projects_dir / "sid-public.jsonl"
    pub.write_text("{}\n")
    priv = projects_dir / "sid-private.jsonl"
    priv.write_text("{}\n")

    monkeypatch.setattr(
        "cli.lib.claude_sessions.list_session_files",
        lambda _w: [pub, priv],
    )
    add_private(tmp_path, "sid-private")

    result = runner.invoke(push_app, ["--legacy-scan", "--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1
    uploaded_path = sessions_calls[0][1]["files"]["file"][0]
    assert uploaded_path == "sid-public.jsonl"

    audit = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "sid-private" in audit
    assert str(priv) in audit


def test_push_legacy_scan_dry_run_segregates_private(tmp_path, monkeypatch):
    """Dry-run JSON shape: legacy-scan candidates surface in
    would_upload OR would_skip_private depending on private membership."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    _stub_config(monkeypatch)

    projects_dir = tmp_path / "projects-fake"
    projects_dir.mkdir()
    public_jsonl = projects_dir / "sid-pub.jsonl"
    public_jsonl.write_text("{}\n")
    private_jsonl = projects_dir / "sid-priv.jsonl"
    private_jsonl.write_text("{}\n")

    monkeypatch.setattr(
        "cli.lib.claude_sessions.list_session_files",
        lambda _w: [public_jsonl, private_jsonl],
    )
    add_private(tmp_path, "sid-priv")

    result = runner.invoke(push_app, ["--legacy-scan", "--dry-run", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert str(public_jsonl) in payload["would_upload"]["sessions"]
    assert str(private_jsonl) not in payload["would_upload"]["sessions"]
    skipped_paths = [e["path"] for e in payload["would_skip_private"]]
    assert str(private_jsonl) in skipped_paths
