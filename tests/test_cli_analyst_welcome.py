"""Integration tests for da analyst setup → /api/welcome wiring."""

import json
from pathlib import Path

import httpx
import pytest

from cli.commands.analyst import _generate_claude_md


class _MockClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        body, status = self._responses.get(url, ({}, 404))
        return httpx.Response(status_code=status, json=body, request=httpx.Request("GET", url))


def _ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    return workspace


def test_generate_claude_md_uses_server_render(tmp_path, monkeypatch):
    workspace = _ws(tmp_path)
    rendered = "# CUSTOM\n\nFrom server.\n"
    mock = _MockClient({
        "https://example.com/api/welcome?server_url=https%3A%2F%2Fexample.com": (
            {"content": rendered}, 200
        ),
    })
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")

    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == rendered
    # Workspace side-effects are created on the success path too.
    assert (workspace / ".claude" / "CLAUDE.local.md").exists()
    settings = json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"


def test_generate_claude_md_falls_back_on_404(tmp_path, monkeypatch):
    workspace = _ws(tmp_path)
    mock = _MockClient({})  # everything 404s
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    body = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    assert "AI Data Analyst" in body
    assert "https://example.com" in body


def test_generate_claude_md_falls_back_on_null_content(tmp_path, monkeypatch):
    """Server returns 200 but malformed body (`content: null`). CLI must use fallback."""
    workspace = _ws(tmp_path)
    mock = _MockClient({
        "https://example.com/api/welcome?server_url=https%3A%2F%2Fexample.com": (
            {"content": None}, 200
        ),
    })
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    body = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    # Embedded fallback contains these literals
    assert "AI Data Analyst" in body
    assert "https://example.com" in body


def test_generate_claude_md_warns_on_5xx(tmp_path, monkeypatch, capsys):
    """500 from server → embedded fallback, with a stderr warning so operators can diagnose."""
    workspace = _ws(tmp_path)
    mock = _MockClient({
        "https://example.com/api/welcome?server_url=https%3A%2F%2Fexample.com": (
            {"detail": "boom"}, 500
        ),
    })
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")

    body = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    assert "AI Data Analyst" in body  # fallback used

    captured = capsys.readouterr()
    assert "500" in captured.err
    assert "fallback" in captured.err.lower()
