"""Behavioural tests for `agnes mcp my-secret test` CLI command."""

from __future__ import annotations

import typer

from cli.commands import mcp as mcpcmd


class _Resp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_my_secret_test_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        mcpcmd,
        "api_post",
        lambda path: _Resp(200, {"ok": True, "tool_count": 3, "message": "ok"}),
    )
    mcpcmd.my_secret_test(source_id="src1", json_out=False)
    out = capsys.readouterr().out
    assert "3 tools reachable" in out


def test_my_secret_test_json(monkeypatch, capsys):
    monkeypatch.setattr(
        mcpcmd,
        "api_post",
        lambda path: _Resp(200, {"ok": False, "tool_count": None, "message": "bad token"}),
    )
    mcpcmd.my_secret_test(source_id="src1", json_out=True)
    out = capsys.readouterr().out
    assert '"ok": false' in out


def test_my_secret_test_403_fails(monkeypatch):
    monkeypatch.setattr(
        mcpcmd,
        "api_post",
        lambda path: _Resp(403, "You are not connected to 'src1'. Run `agnes mcp my-secret set src1`..."),
    )
    try:
        mcpcmd.my_secret_test(source_id="src1", json_out=False)
        raised = False
    except typer.Exit:
        raised = True
    assert raised  # _fail exits non-zero, surfacing the remedy on stderr
