"""Behavioural tests for `agnes mcp connect` / `agnes mcp disconnect`
(2026-07-30 outbound MCP OAuth sources spec §3, CLI device-style UX)."""

from __future__ import annotations

import typer

from cli.commands import mcp as mcpcmd


class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# agnes mcp connect
# ---------------------------------------------------------------------------


def test_connect_opens_browser_and_returns_once_connected(monkeypatch, capsys):
    opened_urls = []
    calls = {"n": 0}

    def _fake_api_get(path):
        # First call is the pre-browser baseline (not yet connected); the
        # flow completes on a later poll.
        calls["n"] += 1
        return _Resp(200, {"has_secret": calls["n"] >= 2, "updated_at": "2026-07-31T10:00:00Z"})

    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: opened_urls.append(url) or True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(mcpcmd, "api_get", _fake_api_get)

    mcpcmd.mcp_connect(source_id="src1", no_browser=False, timeout=10)

    out = capsys.readouterr().out
    assert opened_urls == ["https://agnes.example.com/api/mcp/sources/src1/oauth/authorize"]
    assert "Connected src1" in out


def test_connect_trailing_slash_server_url_yields_single_slash(monkeypatch, capsys):
    """A server URL configured with a trailing slash must not produce a
    doubled path separator (Devin Review on #1130)."""
    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com/")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    calls = {"n": 0}

    def _fake_api_get(path):
        calls["n"] += 1
        return _Resp(200, {"has_secret": calls["n"] >= 2})

    monkeypatch.setattr(mcpcmd, "api_get", _fake_api_get)
    mcpcmd.mcp_connect(source_id="src1", no_browser=True, timeout=10)
    out = capsys.readouterr().out
    assert "https://agnes.example.com/api/mcp/sources/src1/oauth/authorize" in out
    assert "com//api" not in out


def test_reconnect_waits_for_the_credential_to_change(monkeypatch, capsys):
    """Re-connecting an already-connected source must NOT declare success
    off the pre-existing credential — it waits for updated_at to move
    (Devin Review on #1130)."""
    calls = {"n": 0}

    def _fake_api_get(path):
        calls["n"] += 1
        ts = "2026-07-31T09:00:00Z" if calls["n"] < 4 else "2026-07-31T11:11:11Z"
        return _Resp(200, {"has_secret": True, "updated_at": ts})

    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(mcpcmd, "api_get", _fake_api_get)

    mcpcmd.mcp_connect(source_id="src1", no_browser=False, timeout=10)

    assert calls["n"] == 4  # baseline + 2 stale polls + the changed one
    assert "Connected src1" in capsys.readouterr().out


def test_connect_polls_until_connected(monkeypatch, capsys):
    calls = {"n": 0}

    def _fake_api_get(path):
        calls["n"] += 1
        return _Resp(200, {"has_secret": calls["n"] >= 3})

    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(mcpcmd, "api_get", _fake_api_get)

    mcpcmd.mcp_connect(source_id="src1", no_browser=False, timeout=10)

    assert calls["n"] == 3
    assert "Connected src1" in capsys.readouterr().out


def test_connect_no_browser_prints_url_instead_of_opening(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    calls = {"n": 0}

    def _fake_api_get(path):
        calls["n"] += 1
        return _Resp(200, {"has_secret": calls["n"] >= 2})

    monkeypatch.setattr(mcpcmd, "api_get", _fake_api_get)

    mcpcmd.mcp_connect(source_id="src1", no_browser=True, timeout=10)

    assert opened == []  # webbrowser.open never called
    out = capsys.readouterr().out
    assert "https://agnes.example.com/api/mcp/sources/src1/oauth/authorize" in out


def test_connect_times_out_and_exits_nonzero(monkeypatch):
    real_monotonic = mcpcmd.time.monotonic
    ticks = {"n": 0}

    def _fake_monotonic():
        # Advance the clock past the deadline on the second read so the
        # loop's first status check still runs once, then times out.
        ticks["n"] += 1
        return real_monotonic() + (ticks["n"] * 1000)

    monkeypatch.setattr(mcpcmd.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(mcpcmd, "get_server_url", lambda: "https://agnes.example.com")
    monkeypatch.setattr(mcpcmd.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(mcpcmd.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(mcpcmd, "api_get", lambda path: _Resp(200, {"has_secret": False}))

    try:
        mcpcmd.mcp_connect(source_id="src1", no_browser=False, timeout=1)
        raised = False
    except typer.Exit as exc:
        raised = True
        assert exc.exit_code == 1
    assert raised


# ---------------------------------------------------------------------------
# agnes mcp disconnect
# ---------------------------------------------------------------------------


def test_disconnect_calls_delete_and_reports_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(mcpcmd, "api_delete", lambda path: calls.append(path) or _Resp(204))
    mcpcmd.mcp_disconnect(source_id="src1", yes=True)
    assert calls == ["/api/mcp/sources/src1/oauth/connection"]
    assert "Disconnected src1" in capsys.readouterr().out


def test_disconnect_fails_on_error_status(monkeypatch):
    monkeypatch.setattr(mcpcmd, "api_delete", lambda path: _Resp(403, "not_granted"))
    try:
        mcpcmd.mcp_disconnect(source_id="src1", yes=True)
        raised = False
    except typer.Exit:
        raised = True
    assert raised


def test_disconnect_prompts_without_yes(monkeypatch):
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_kw: False)
    try:
        mcpcmd.mcp_disconnect(source_id="src1", yes=False)
        raised = False
    except typer.Abort:
        raised = True
    assert raised
