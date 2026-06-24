"""Tests for `agnes push` — the scan-based SessionEnd uploader.

push reads the workspace root from config (`workspace_root`), scans that
workspace's Claude Code session folder for ``*.jsonl`` transcripts, and
uploads new/grown ones (dedup by session_id + byte size against the upload
ledger). These tests patch `get_workspace_root` + `list_session_files` in the
push module so the scan is deterministic; the encoder itself is covered by
`test_session_paths.py`.
"""

import json
import re
from contextlib import contextmanager

from typer.testing import CliRunner

from cli.commands.push import push_app
from cli.lib.private_list import add_private
from cli.lib.upload_log import (
    failed_log_path,
    mark_uploaded,
    private_skipped_log_path,
    read_uploaded,
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


def _stub_config(monkeypatch, workspace) -> None:
    """Wire server/token + the workspace_root anchor to *workspace*."""
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: str(workspace))


def _stub_sessions(monkeypatch, files) -> None:
    """Make the folder scan return exactly *files* (list of Paths)."""
    monkeypatch.setattr("cli.commands.push.list_session_files", lambda _ws: list(files))


def _record_uploads(monkeypatch) -> list[tuple[str, dict]]:
    """Patch api_post to record calls and return success. Returns the recorder list."""
    calls: list[tuple[str, dict]] = []

    def _fake(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


def _make_jsonl(workspace, name: str, content: str = '{"event":"x"}\n'):
    p = workspace / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------- Smoke + dry-run --------------------------------------------------


def test_push_help():
    result = runner.invoke(push_app, ["--help"])
    assert result.exit_code == 0
    out = _clean(result.output)
    assert "--quiet" in out
    assert "--json" in out
    assert "--dry-run" in out
    # The legacy-scan flag is gone — scan IS the mechanism now.
    assert "--legacy-scan" not in out


def test_push_no_workspace_root_is_noop(tmp_path, monkeypatch):
    """No workspace_root in config → push uploads nothing and exits 0."""
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: None)

    def _raise(*a, **kw):
        raise AssertionError("api_post must not be called without workspace_root")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["workspace_root"] is None
    assert payload["sessions"] == 0


def test_push_dry_run_no_writes(tmp_path, monkeypatch):
    """--dry-run lists what would upload but sends nothing and writes no ledger."""
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "abc.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
    assert not uploaded_log_path(tmp_path).exists()


# ---------- Happy path + dedup by size ---------------------------------------


def test_push_uploads_new_session_and_writes_ledger(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "abc.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1
    # Ledger row is session_id + size.
    led = read_uploaded(tmp_path)
    assert led == {"abc": transcript.stat().st_size}


def test_push_skips_already_uploaded_same_size(tmp_path, monkeypatch):
    """Second push of an unchanged transcript uploads nothing (dedup by size)."""
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "abc.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    # Pre-seed the ledger as if already uploaded at the current size.
    mark_uploaded(tmp_path, "abc", transcript.stat().st_size)

    def _raise(*a, **kw):
        raise AssertionError("unchanged session must not re-upload")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["sessions"] == 0
    assert payload["skipped_unchanged"] == 1


def test_push_reuploads_grown_transcript(tmp_path, monkeypatch):
    """A transcript that grew since last upload (larger size) re-uploads."""
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "abc.jsonl", content="{}\n")
    _stub_sessions(monkeypatch, [transcript])

    # Ledger says we uploaded it at a SMALLER size.
    mark_uploaded(tmp_path, "abc", 1)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert len([c for c in calls if c[0] == "/api/upload/sessions"]) == 1
    # Ledger now records the new (larger) size.
    assert read_uploaded(tmp_path)["abc"] == transcript.stat().st_size


def test_push_end_to_end_dedup_across_two_runs(tmp_path, monkeypatch):
    """First push uploads; second push (file unchanged) uploads nothing."""
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "abc.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    runner.invoke(push_app, ["--quiet"])
    runner.invoke(push_app, ["--quiet"])
    assert len([c for c in calls if c[0] == "/api/upload/sessions"]) == 1


# ---------- Concurrency lock -------------------------------------------------


def test_push_silent_exit_when_lock_held(tmp_path, monkeypatch):
    """Concurrent SessionEnd hooks: only one push runs, others silent-exit."""
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "x.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    @contextmanager
    def _yield_none(workspace):
        yield None

    monkeypatch.setattr("cli.commands.push.acquire_or_skip", _yield_none)

    def _raise(*a, **kw):
        raise AssertionError("api_post called when lock unavailable")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert result.output == ""
    assert not uploaded_log_path(tmp_path).exists()


# ---------- CLAUDE.local.md --------------------------------------------------


def test_push_uploads_local_md_from_workspace_root(tmp_path, monkeypatch):
    """CLAUDE.local.md uploaded from <workspace_root>/.claude/, not cwd."""
    _stub_config(monkeypatch, tmp_path)
    _stub_sessions(monkeypatch, [])
    calls = _record_uploads(monkeypatch)

    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.local.md").write_text("notes")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    md_calls = [c for c in calls if c[0] == "/api/upload/local-md"]
    assert len(md_calls) == 1


def test_push_json_output(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "x.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["sessions"] == 1
    assert data["errors"] == []
    assert data["private_skipped"] == 0


# ---------- Private filter ---------------------------------------------------


def test_push_skips_private_session_and_audit_logs(tmp_path, monkeypatch):
    """A session whose id is on the private list is never uploaded; the skip
    is audit-logged and the file's content never reaches the server."""
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)

    transcript = _make_jsonl(tmp_path, "sid-private.jsonl")
    _stub_sessions(monkeypatch, [transcript])
    add_private(tmp_path, "sid-private")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    assert [c for c in calls if c[0] == "/api/upload/sessions"] == []
    audit = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "sid-private" in audit
    assert str(transcript) in audit
    # Private skip never records to the upload ledger.
    assert not uploaded_log_path(tmp_path).exists()


def test_push_mixes_private_and_public_correctly(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)

    secret = _make_jsonl(tmp_path, "sid-secret.jsonl")
    public = _make_jsonl(tmp_path, "sid-public.jsonl")
    _stub_sessions(monkeypatch, [secret, public])
    add_private(tmp_path, "sid-secret")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    uploaded_names = [
        c[1]["files"]["file"][0] for c in calls if c[0] == "/api/upload/sessions"
    ]
    assert uploaded_names == ["sid-public.jsonl"]
    audit = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "sid-secret" in audit
    assert "sid-public" not in audit


def test_push_dry_run_shows_private_skip(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "sid-priv.jsonl")
    _stub_sessions(monkeypatch, [transcript])
    add_private(tmp_path, "sid-priv")

    def _raise(*a, **kw):
        raise AssertionError("api_post was called during --dry-run")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--dry-run"])
    assert result.exit_code == 0
    assert "1 private session" in result.output
    assert "sid-priv" in result.output


# ---------- Failure handling -------------------------------------------------


def _stub_api_post_status(monkeypatch, status: int) -> None:
    def _fixed(*a, **kw):
        return _FakeResp(status)
    monkeypatch.setattr("cli.commands.push.api_post", _fixed)


def test_push_4xx_logged_to_failed_not_ledger(tmp_path, monkeypatch):
    """403 (and 400/413) → forensic failed-log, NOT the upload ledger."""
    for status in (400, 403, 413):
        ws = tmp_path / f"ws-{status}"
        ws.mkdir()
        _stub_config(monkeypatch, ws)
        _stub_api_post_status(monkeypatch, status)
        transcript = _make_jsonl(ws, f"sid-{status}.jsonl")
        _stub_sessions(monkeypatch, [transcript])

        result = runner.invoke(push_app, ["--json"])
        assert result.exit_code == 0, (status, result.output)
        payload = json.loads(result.output)
        assert payload["dropped_permanent"] == 1, (status, payload)
        assert payload["sessions"] == 0
        log = failed_log_path(ws).read_text(encoding="utf-8")
        assert f"\t{status}\t" in log
        assert f"sid-{status}" in log
        # Not recorded as uploaded → a later push (after the cause is fixed)
        # can still retry. (Permanent failures stay in the forensic log only.)
        assert read_uploaded(ws) == {}


def test_push_transient_failure_retries_next_run(tmp_path, monkeypatch):
    """401 / 408 / 429 / 5xx / network → left out of the ledger (no failed-log),
    so the next push retries the same file."""
    for status in (401, 408, 429, 500, 503):
        ws = tmp_path / f"ws-{status}"
        ws.mkdir()
        _stub_config(monkeypatch, ws)
        _stub_api_post_status(monkeypatch, status)
        transcript = _make_jsonl(ws, f"sid-{status}.jsonl")
        _stub_sessions(monkeypatch, [transcript])

        result = runner.invoke(push_app, ["--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dropped_permanent"] == 0, (status, payload)
        assert read_uploaded(ws) == {}
        assert not failed_log_path(ws).exists()


def test_push_transient_then_success(tmp_path, monkeypatch):
    """503 leaves nothing recorded; once the server accepts, the next push
    uploads it and writes the ledger."""
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "x.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    _stub_api_post_status(monkeypatch, 503)
    runner.invoke(push_app, ["--quiet"])
    assert read_uploaded(tmp_path) == {}

    calls = _record_uploads(monkeypatch)  # now 200
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert len([c for c in calls if c[0] == "/api/upload/sessions"]) == 1
    assert read_uploaded(tmp_path)["x"] == transcript.stat().st_size


def test_push_network_exception_retries(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "x.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    def _raise(*a, **kw):
        raise ConnectionError("server unreachable")

    monkeypatch.setattr("cli.commands.push.api_post", _raise)

    result = runner.invoke(push_app, ["--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dropped_permanent"] == 0
    assert read_uploaded(tmp_path) == {}
    assert not failed_log_path(tmp_path).exists()


def test_push_4xx_visible_in_stdout(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    _stub_api_post_status(monkeypatch, 413)
    transcript = _make_jsonl(tmp_path, "huge.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    assert "agnes-sessions-failed.txt" in result.output
    assert "permanent failure" in result.output
