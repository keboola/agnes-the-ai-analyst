"""Unit tests for connectors/mcp/client.py transport routing + auth headers.

Mocks the SDK's three transport constructors so the tests run without a
network connection or a subprocess. Covers:

* HTTP (Streamable) and SSE branches dispatch to the right SDK function.
* ``url`` is passed through verbatim.
* ``_build_http_headers`` builds the correct ``Authorization`` header for
  bearer / basic / none / missing-env-var.
* Unsupported transport raises ``NotImplementedError`` with a clear message.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from connectors.mcp import client as mcp_client


# ── _build_http_headers ────────────────────────────────────────────────────


def test_build_headers_none_method_returns_empty():
    assert mcp_client._build_http_headers({"auth_method": "none"}) == {}
    assert mcp_client._build_http_headers({"auth_method": ""}) == {}
    assert mcp_client._build_http_headers({}) == {}


def test_build_headers_bearer(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-abc")
    src = {"auth_method": "bearer", "auth_secret_env": "MY_TOKEN"}
    assert mcp_client._build_http_headers(src) == {"Authorization": "Bearer secret-abc"}


def test_build_headers_basic_encodes_userpass(monkeypatch):
    monkeypatch.setenv("MY_CRED", "alice:wonderland")
    src = {"auth_method": "basic", "auth_secret_env": "MY_CRED"}
    expected = "Basic " + base64.b64encode(b"alice:wonderland").decode()
    assert mcp_client._build_http_headers(src) == {"Authorization": expected}


def test_build_headers_missing_env_falls_through_to_anon(monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    src = {"auth_method": "bearer", "auth_secret_env": "MY_TOKEN"}
    # No exception, no Authorization header — caller will attempt anonymous.
    assert mcp_client._build_http_headers(src) == {}


# ── _open_session transport routing ────────────────────────────────────────


def _fake_streams_cm(streams):
    """Build an async context manager that yields a fixed tuple of streams."""

    @asynccontextmanager
    async def cm(*args, **kwargs):
        cm.last_call = (args, kwargs)
        yield streams

    cm.last_call = None
    return cm


def _fake_client_session():
    """ClientSession() replacement: async-context, .initialize() awaited."""
    session = MagicMock(name="ClientSession")
    session.initialize = AsyncMock()
    session_cm = MagicMock(name="ClientSession.cm")
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    ctor = MagicMock(return_value=session_cm)
    return ctor, session


def test_http_transport_dispatches_to_streamable_client(monkeypatch):
    streams = (MagicMock(name="read"), MagicMock(name="write"), MagicMock(name="get_id"))
    fake_http = _fake_streams_cm(streams)
    fake_sse = _fake_streams_cm(("r", "w"))
    ctor, _session = _fake_client_session()

    monkeypatch.setattr(mcp_client, "streamablehttp_client", fake_http)
    monkeypatch.setattr(mcp_client, "sse_client", fake_sse)
    monkeypatch.setattr(mcp_client, "ClientSession", ctor)

    src = {
        "transport": "http",
        "url": "https://upstream.example.com/mcp",
        "auth_method": "bearer",
        "auth_secret_env": "MY_TOKEN",
    }
    monkeypatch.setenv("MY_TOKEN", "x")

    async def _drive():
        async with mcp_client._open_session(src):
            pass

    asyncio.run(_drive())

    assert fake_http.last_call is not None, "streamable client should have been entered"
    assert fake_sse.last_call is None, "sse client should NOT have been entered for http transport"
    args, kwargs = fake_http.last_call
    assert args[0] == "https://upstream.example.com/mcp"
    assert kwargs["headers"] == {"Authorization": "Bearer x"}


def test_sse_transport_dispatches_to_sse_client(monkeypatch):
    streams = (MagicMock(name="read"), MagicMock(name="write"))
    fake_http = _fake_streams_cm((MagicMock(), MagicMock(), MagicMock()))
    fake_sse = _fake_streams_cm(streams)
    ctor, _session = _fake_client_session()

    monkeypatch.setattr(mcp_client, "streamablehttp_client", fake_http)
    monkeypatch.setattr(mcp_client, "sse_client", fake_sse)
    monkeypatch.setattr(mcp_client, "ClientSession", ctor)

    src = {"transport": "sse", "url": "https://legacy.example/mcp/sse"}

    async def _drive():
        async with mcp_client._open_session(src):
            pass

    asyncio.run(_drive())

    assert fake_sse.last_call is not None
    assert fake_http.last_call is None
    args, kwargs = fake_sse.last_call
    assert args[0] == "https://legacy.example/mcp/sse"
    # No auth → headers=None (keeps the SDK's default User-Agent path)
    assert kwargs["headers"] is None


def test_http_transport_without_url_raises_value_error():
    src = {"transport": "http"}

    async def _drive():
        async with mcp_client._open_session(src):
            pass  # pragma: no cover

    with pytest.raises(ValueError, match="'url'"):
        asyncio.run(_drive())


def test_unknown_transport_raises_notimplemented():
    src = {"transport": "websocket", "url": "wss://x"}

    async def _drive():
        async with mcp_client._open_session(src):
            pass  # pragma: no cover

    with pytest.raises(NotImplementedError, match="websocket"):
        asyncio.run(_drive())
