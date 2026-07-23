"""Tests for `agnes push` — the scan-based SessionEnd uploader.

push reads the workspace root from config (`workspace_root`), scans that
workspace's Claude Code session folder for ``*.jsonl`` transcripts, and
uploads new/grown ones (dedup by session_id + byte size against the upload
ledger). These tests patch `get_workspace_root` + `list_session_files` in the
push module so the scan is deterministic; the encoder itself is covered by
`test_session_paths.py`.
"""

import gzip
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
    """Wire server/token + the workspace_root anchor to *workspace*.

    Also stubs the gzip-capability health probe (`api_get`, used by
    `_server_accepts_gzip()`) to a canned "no gzip" response. `push.py`
    imports `api_get` into its own module scope (`from cli.client import
    api_get, ...`), so patching `get_server_url` here does NOT sandbox
    that probe — without this, `_server_accepts_gzip()` makes a REAL
    network call to whatever `get_server_url()` resolves to on the
    machine running the suite. Tests exercising the gzip codepath itself
    override this via a later `monkeypatch.setattr("cli.commands.push.api_get", ...)`.
    """
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: str(workspace))
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp(None))


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


# ---------- Token redaction (#753) -------------------------------------------


_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


def test_push_redacts_jwt_in_session_transcript(tmp_path, monkeypatch):
    """A JWT-shaped token embedded in a transcript must not reach the server,
    but the on-disk ledger size stays the RAW size (not the redacted size)."""
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)
    content = json.dumps({"type": "tool_use", "input": {"command": f"echo {_JWT}"}}) + "\n"
    transcript = _make_jsonl(tmp_path, "abc.jsonl", content=content)
    raw_size = transcript.stat().st_size
    _stub_sessions(monkeypatch, [transcript])

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    sessions_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(sessions_calls) == 1
    uploaded_fh = sessions_calls[0][1]["files"]["file"][1]
    uploaded_bytes = uploaded_fh.read()
    assert _JWT.encode() not in uploaded_bytes
    assert b"[REDACTED-JWT]" in uploaded_bytes

    # Ledger still records the ON-DISK size, unaffected by redaction.
    assert read_uploaded(tmp_path) == {"abc": raw_size}


def test_push_redacts_jwt_in_local_md(tmp_path, monkeypatch):
    _stub_config(monkeypatch, tmp_path)
    _stub_sessions(monkeypatch, [])
    calls = _record_uploads(monkeypatch)

    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.local.md").write_text(f"my token is {_JWT}", encoding="utf-8")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0

    md_calls = [c for c in calls if c[0] == "/api/upload/local-md"]
    assert len(md_calls) == 1
    uploaded_content = md_calls[0][1]["json"]["content"]
    assert _JWT not in uploaded_content
    assert "[REDACTED-JWT]" in uploaded_content


def test_push_still_skips_private_session_when_it_contains_a_jwt(tmp_path, monkeypatch):
    """Private-listed sessions still never reach the upload call at all —
    redaction and the private filter are independent layers."""
    _stub_config(monkeypatch, tmp_path)
    calls = _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "sid-private.jsonl", content=f"token: {_JWT}\n")
    _stub_sessions(monkeypatch, [transcript])
    add_private(tmp_path, "sid-private")

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert [c for c in calls if c[0] == "/api/upload/sessions"] == []


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

    uploaded_names = [c[1]["files"]["file"][0] for c in calls if c[0] == "/api/upload/sessions"]
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


def test_push_permanent_failure_not_retried_next_run(tmp_path, monkeypatch):
    """A 4xx-rejected transcript must NOT be re-uploaded on the next scan —
    once logged to the failed-log, push skips it (the scan-based equivalent of
    the old queue dropping a permanently-failed entry). Two-run coverage."""
    _stub_config(monkeypatch, tmp_path)
    transcript = _make_jsonl(tmp_path, "sid-413.jsonl")
    _stub_sessions(monkeypatch, [transcript])

    # Run 1: server permanently rejects (413) → logged to the failed-log.
    _stub_api_post_status(monkeypatch, 413)
    r1 = runner.invoke(push_app, ["--json"])
    assert r1.exit_code == 0
    assert json.loads(r1.output)["dropped_permanent"] == 1
    assert "sid-413" in failed_log_path(tmp_path).read_text(encoding="utf-8")

    # Run 2: same unchanged transcript must not be uploaded again.
    def _boom(*a, **kw):
        raise AssertionError("permanently-failed session must not be re-uploaded")

    monkeypatch.setattr("cli.commands.push.api_post", _boom)
    r2 = runner.invoke(push_app, ["--json"])
    assert r2.exit_code == 0
    payload = json.loads(r2.output)
    assert payload["sessions"] == 0
    assert payload["skipped_failed"] == 1


def test_push_private_skip_logged_once_across_runs(tmp_path, monkeypatch):
    """A private session is audit-logged once, not on every push run (the
    transcript stays on disk and the private list is persistent)."""
    _stub_config(monkeypatch, tmp_path)
    _record_uploads(monkeypatch)
    transcript = _make_jsonl(tmp_path, "sid-priv.jsonl")
    _stub_sessions(monkeypatch, [transcript])
    add_private(tmp_path, "sid-priv")

    runner.invoke(push_app, ["--quiet"])
    runner.invoke(push_app, ["--quiet"])

    log = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    lines = [ln for ln in log.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert "\tsid-priv\t" in log


def test_push_real_scan_uses_workspace_root_folder(tmp_path, monkeypatch):
    """Integration: leave `list_session_files` REAL and exercise the central
    glue — workspace_root → encoded Claude Code folder → scan — even when push
    runs from a different cwd. Pins the contract the hook actually relies on."""
    from cli.lib.session_paths import session_dir

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    folder = session_dir(workspace)
    folder.mkdir(parents=True)
    (folder / "sid-real.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")

    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: str(workspace))
    # Sandbox the gzip-capability probe (see `_stub_config`'s docstring) —
    # this test hand-rolls its config stubs instead of calling `_stub_config`
    # so it can leave `list_session_files` real; still needs this patch.
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp(None))
    # NOTE: list_session_files is intentionally NOT patched here.
    calls = _record_uploads(monkeypatch)

    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output
    names = [c[1]["files"]["file"][0] for c in calls if c[0] == "/api/upload/sessions"]
    assert names == ["sid-real.jsonl"]


def test_push_json_shape_consistent_across_paths(tmp_path, monkeypatch):
    """Both non-dry-run --json paths (no workspace_root vs real run) emit the
    SAME key set, so a consumer reading e.g. result['dropped_permanent'] never
    KeyErrors depending on whether the workspace is anchored."""
    # No workspace_root.
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: None)
    r_none = runner.invoke(push_app, ["--json"])
    assert r_none.exit_code == 0
    keys_none = set(json.loads(r_none.output).keys())

    # Real run.
    ws = tmp_path / "ws"
    ws.mkdir()
    _stub_config(monkeypatch, ws)
    _record_uploads(monkeypatch)
    _stub_sessions(monkeypatch, [_make_jsonl(ws, "x.jsonl")])
    r_real = runner.invoke(push_app, ["--json"])
    assert r_real.exit_code == 0
    keys_real = set(json.loads(r_real.output).keys())

    assert keys_none == keys_real, (keys_none, keys_real)
    for k in ("sessions", "dropped_permanent", "skipped_failed", "workspace_root"):
        assert k in keys_none


class _FakeProbeResp:
    def __init__(self, caps: str | None) -> None:
        self.status_code = 200
        self.headers = {} if caps is None else {"X-Agnes-Accepts": caps}


def _record_upload_bodies(monkeypatch) -> list[tuple[str, str, bytes]]:
    """Patch api_post to record (path, part_filename, part_bytes) and succeed."""
    calls: list[tuple[str, str, bytes]] = []

    def _fake(path, **kwargs):
        files = kwargs.get("files")
        if files:
            name, buf = files["file"]
            calls.append((path, name, buf.getvalue()))
        else:
            calls.append((path, "", b""))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


def _one_transcript(tmp_path, monkeypatch, content: bytes):
    t = tmp_path / "sess-gz-test.jsonl"
    t.write_bytes(content)
    _stub_config(monkeypatch, tmp_path)
    _stub_sessions(monkeypatch, [t])
    return t


def test_push_gzips_when_server_advertises(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n' * 20
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp("session-gzip"))
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    session_calls = [c for c in calls if c[0] == "/api/upload/sessions"]
    assert len(session_calls) == 1
    _path, name, body = session_calls[0]
    assert name == "sess-gz-test.jsonl.gz"
    assert gzip.decompress(body) == content  # redaction is a no-op for this content


def test_push_plain_when_capability_absent(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeProbeResp(None))
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content


def test_push_plain_when_probe_fails(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)

    def _boom(p, **kw):
        raise RuntimeError("server unreachable")

    monkeypatch.setattr("cli.commands.push.api_get", _boom)
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content


def test_push_env_killswitch_skips_probe(tmp_path, monkeypatch):
    content = b'{"type": "message"}\n'
    _one_transcript(tmp_path, monkeypatch, content)
    monkeypatch.setenv("AGNES_PUSH_NO_GZIP", "1")

    probe_calls: list[str] = []

    def _record_probe(p, **kw):
        probe_calls.append(p)
        return _FakeProbeResp("session-gzip")

    monkeypatch.setattr("cli.commands.push.api_get", _record_probe)
    calls = _record_upload_bodies(monkeypatch)
    result = runner.invoke(push_app, [])
    assert result.exit_code == 0
    # With the kill-switch set, the capability probe must never fire...
    assert probe_calls == []
    # ...and the upload must fall back to the plain (non-gzip) filename.
    _path, name, body = [c for c in calls if c[0] == "/api/upload/sessions"][0]
    assert name == "sess-gz-test.jsonl"
    assert body == content


def test_gzip_probe_never_builds_a_real_client(tmp_path, monkeypatch):
    """Regression guard: `_stub_config` must fully sandbox `_server_accepts_gzip()`'s
    health-check probe. `push.py` calls `api_get` as a bare name resolved from its
    own module globals (`from cli.client import api_get`), a separate binding from
    `cli.client.get_client` — patching only `cli.commands.push.get_server_url`
    does not reach it, so an unpatched probe falls through to a real
    `cli.client.get_client()` call. Tracks (rather than raising inside)
    `get_client` so the assertion fires even though `_server_accepts_gzip()`
    swallows every exception and fails open to False."""
    import cli.client as client_module

    real_client_calls = {"count": 0}

    def _tracking_get_client(*a, **kw):
        real_client_calls["count"] += 1
        raise RuntimeError("blocked: probe must not construct a real httpx client")

    monkeypatch.setattr(client_module, "get_client", _tracking_get_client)
    _stub_config(monkeypatch, tmp_path)

    from cli.commands.push import _server_accepts_gzip

    _server_accepts_gzip()

    assert real_client_calls["count"] == 0, (
        "the gzip capability probe bypassed the api_get test stub and tried "
        "to build a real httpx client — _stub_config no longer sandboxes "
        "_server_accepts_gzip()"
    )
