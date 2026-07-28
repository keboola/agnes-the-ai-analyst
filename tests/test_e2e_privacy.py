"""End-to-end test pinning the user-visible privacy promise:
**a session marked /agnes-private never reaches the server.**

With the scan-based `agnes push`, the flow is: ``/agnes-private`` writes the
session_id to the workspace's private list (anchored on the ``workspace_root``
config key); the next push scans the workspace's Claude Code session folder
and skips any transcript whose stem is on that list, audit-logging the skip.
Both commands anchor on the SAME ``workspace_root``, so they always agree on
which workspace's list to use. This file wires `mark-private` + `push`
together against a recording fake `api_post` to verify the cross-component
contract.
"""

from __future__ import annotations

from typer.testing import CliRunner

from cli.commands.mark_private import mark_private_app
from cli.commands.push import push_app
from cli.lib.upload_log import (
    failed_log_path,
    private_skipped_log_path,
    uploaded_log_path,
)


runner = CliRunner()


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeHealthResp:
    """No-gzip-capability stand-in for the `/api/health` probe response."""

    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}


def _record_uploads(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def _fake(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return _FakeResp(200)

    monkeypatch.setattr("cli.commands.push.api_post", _fake)
    return calls


def _stub_push(monkeypatch, workspace, files) -> None:
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.get_token", lambda: "test-pat")
    monkeypatch.setattr("cli.commands.push.get_workspace_root", lambda: str(workspace))
    monkeypatch.setattr("cli.commands.push.list_session_files", lambda _ws: list(files))
    # `_server_accepts_gzip()`'s health-check probe calls `api_get` as a name
    # bound in push.py's own module scope — patching `get_server_url` above
    # does not sandbox it, so without this the probe makes a REAL network
    # call to whatever `get_server_url()` resolves to on the machine running
    # the suite (see `_stub_config` in test_cli_push.py for the full story).
    monkeypatch.setattr("cli.commands.push.api_get", lambda p, **kw: _FakeHealthResp())


def _mark_private(workspace, session_id, monkeypatch) -> None:
    """Simulate `/agnes-private` — `!`-prefix bash runs `agnes mark-private`
    with CLAUDE_CODE_SESSION_ID set and workspace_root anchored in config."""
    monkeypatch.setattr("cli.commands.mark_private.get_workspace_root", lambda: str(workspace))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    result = runner.invoke(mark_private_app, [])
    assert result.exit_code == 0, result.output


def _uploaded_session_names(calls) -> list[str]:
    out: list[str] = []
    for endpoint, kwargs in calls:
        if endpoint != "/api/upload/sessions":
            continue
        file_field = kwargs.get("files", {}).get("file")
        if isinstance(file_field, tuple):
            out.append(file_field[0])
    return out


def _make_jsonl(workspace, name):
    p = workspace / name
    p.write_text("{}\n", encoding="utf-8")
    return p


def test_e2e_marked_session_never_uploads(tmp_path, monkeypatch):
    """User runs /agnes-private; the next push skips that transcript and
    audit-logs it, while a sibling public session uploads normally."""
    private = _make_jsonl(tmp_path, "private-session.jsonl")
    public = _make_jsonl(tmp_path, "public-session.jsonl")
    _stub_push(monkeypatch, tmp_path, [private, public])
    calls = _record_uploads(monkeypatch)

    _mark_private(tmp_path, "private-session", monkeypatch)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output

    assert _uploaded_session_names(calls) == ["public-session.jsonl"]

    skipped_log = private_skipped_log_path(tmp_path).read_text(encoding="utf-8")
    assert "private-session" in skipped_log
    assert "private-session.jsonl" in skipped_log
    # A privacy skip is an intentional exclusion, never a failure.
    assert not failed_log_path(tmp_path).exists()
    # And the private session is never recorded in the upload ledger.
    led = uploaded_log_path(tmp_path)
    assert (not led.exists()) or "private-session" not in led.read_text(encoding="utf-8")


def test_e2e_unmarked_sessions_upload_normally(tmp_path, monkeypatch):
    """Control: with no /agnes-private call, both sessions upload — proves the
    test above exercises the privacy filter, not some unrelated reason."""
    a = _make_jsonl(tmp_path, "a.jsonl")
    b = _make_jsonl(tmp_path, "b.jsonl")
    _stub_push(monkeypatch, tmp_path, [a, b])
    calls = _record_uploads(monkeypatch)

    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0, result.output

    assert sorted(_uploaded_session_names(calls)) == ["a.jsonl", "b.jsonl"]
    assert not private_skipped_log_path(tmp_path).exists()
