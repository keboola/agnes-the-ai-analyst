"""Caller-identity resolution for the Streamable-HTTP MCP transport.

The passthrough closures registered on the Streamable-HTTP server forward the
caller's own identity into ``call_tool_async`` via ``_current_caller_id``, so a
``scope='per_user'`` source resolves the caller's own credential instead of
falling back to the shared one. ``_current_caller_id`` decodes the already-
verified session JWT (``sub`` = user id) with no DB access; it must fail closed
(return None) on any missing/invalid token.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

import app.api.mcp_streamable as ms
from app.auth.jwt import create_access_token


class _Access:
    def __init__(self, token: str) -> None:
        self.token = token


def test_current_caller_id_decodes_sub_from_session_jwt(monkeypatch):
    token = create_access_token("analyst1", "analyst@test.com")
    monkeypatch.setattr(ms, "get_access_token", lambda: _Access(token))
    assert ms._current_caller_id() == "analyst1"


def test_current_caller_id_none_when_no_access_token(monkeypatch):
    monkeypatch.setattr(ms, "get_access_token", lambda: None)
    assert ms._current_caller_id() is None


def test_current_caller_id_none_on_invalid_token(monkeypatch):
    monkeypatch.setattr(ms, "get_access_token", lambda: _Access("not-a-jwt"))
    assert ms._current_caller_id() is None
