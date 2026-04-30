"""Integration tests for da analyst setup → /api/welcome wiring."""

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


def test_generate_claude_md_uses_server_render(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    rendered = "# CUSTOM\n\nFrom server.\n"
    mock = _MockClient({
        "https://example.com/api/welcome?server_url=https%3A%2F%2Fexample.com": (
            {"content": rendered}, 200
        ),
    })
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == rendered


def test_generate_claude_md_falls_back_on_404(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    mock = _MockClient({})  # everything 404s
    monkeypatch.setattr("cli.commands.analyst.httpx", type("_M", (), {"get": mock.get}))
    _generate_claude_md(workspace, server_url="https://example.com", token="t")
    body = (workspace / "CLAUDE.md").read_text(encoding="utf-8")
    assert "AI Data Analyst" in body  # embedded fallback contains this string
    assert "https://example.com" in body
