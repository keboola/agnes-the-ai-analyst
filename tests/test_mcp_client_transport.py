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


async def _close_pool():
    from connectors.mcp import session_pool

    await session_pool.close_all()


def _patch_stdio_everywhere(monkeypatch, params_factory, streams_cm, session_ctor):
    """Patch the stdio transport on BOTH paths that can launch it.

    Since the session pool landed, a stdio source is served from a warm
    process by default and the direct spawn is the fallback. The params are
    built before that fork, so these tests parametrize over both: what gets
    launched must not depend on which path launched it.
    """
    from connectors.mcp import session_pool

    monkeypatch.setattr(mcp_client, "StdioServerParameters", params_factory)
    monkeypatch.setattr(mcp_client, "stdio_client", streams_cm)
    monkeypatch.setattr(mcp_client, "ClientSession", session_ctor)
    monkeypatch.setattr(session_pool, "stdio_client", streams_cm)
    monkeypatch.setattr(session_pool, "ClientSession", session_ctor)
    monkeypatch.setattr(session_pool, "spec_key", lambda params, **kw: "test-key")


@pytest.mark.parametrize("pooled", (True, False))
def test_stdio_env_is_base_secret_overlays(monkeypatch, pooled):
    """Per-source ``env`` is the base; the auth_secret_env secret overlays it."""
    captured = {}

    def _fake_stdio_params(*, command, args, env):
        captured["command"] = command
        captured["args"] = args
        captured["env"] = env
        return MagicMock(name="StdioServerParameters")

    fake_stdio = _fake_streams_cm((MagicMock(name="read"), MagicMock(name="write")))
    ctor, _session = _fake_client_session()

    monkeypatch.setenv("AGNES_MCP_SESSION_POOL", "1" if pooled else "0")
    _patch_stdio_everywhere(monkeypatch, _fake_stdio_params, fake_stdio, ctor)
    monkeypatch.setattr(mcp_client, "_lookup_secret_for_source", lambda src, **kw: "tok-123")

    src = {
        "transport": "stdio",
        "command": "crm-mcp",
        "args": ["--flag"],
        "env": {"CRM_API_URL": "u"},
        "auth_secret_env": "CRM_TOKEN",
    }

    async def _drive():
        async with mcp_client._open_session(src):
            pass

    asyncio.run(_drive())
    asyncio.run(_close_pool())

    assert captured["command"] == "crm-mcp"
    assert captured["args"] == ["--flag"]
    # base env preserved, secret overlaid under its env-var name
    assert captured["env"] == {"CRM_API_URL": "u", "CRM_TOKEN": "tok-123"}


@pytest.mark.parametrize("pooled", (True, False))
def test_stdio_no_env_no_secret_passes_none(monkeypatch, pooled):
    """No env and no secret → env=None (prior behavior, unchanged)."""
    captured = {}

    def _fake_stdio_params(*, command, args, env):
        captured["env"] = env
        return MagicMock(name="StdioServerParameters")

    fake_stdio = _fake_streams_cm((MagicMock(name="read"), MagicMock(name="write")))
    ctor, _session = _fake_client_session()

    monkeypatch.setenv("AGNES_MCP_SESSION_POOL", "1" if pooled else "0")
    _patch_stdio_everywhere(monkeypatch, _fake_stdio_params, fake_stdio, ctor)

    src = {"transport": "stdio", "command": "plain-mcp"}

    async def _drive():
        async with mcp_client._open_session(src):
            pass

    asyncio.run(_drive())
    asyncio.run(_close_pool())

    assert captured["env"] is None


def test_unknown_transport_raises_notimplemented():
    src = {"transport": "websocket", "url": "wss://x"}

    async def _drive():
        async with mcp_client._open_session(src):
            pass  # pragma: no cover

    with pytest.raises(NotImplementedError, match="websocket"):
        asyncio.run(_drive())


# ── per-user pool-key salting ──────────────────────────────────────────────


class _RecordingPool:
    """Stands in for the session pool; records the key_salt of every acquire."""

    def __init__(self) -> None:
        self.salts: list = []

    @asynccontextmanager
    async def acquire(self, params, *, key_salt=""):
        self.salts.append(key_salt)
        yield MagicMock(name="pooled-session")


@pytest.mark.parametrize(
    "scope,caller,expected",
    [
        ("per_user", "alice", "user:alice"),
        ("per_user", "bob", "user:bob"),
        ("per_user", None, ""),  # caller-less materialize: one identity, unsalted
        ("shared", "alice", ""),
        (None, "alice", ""),  # scope defaults to shared
    ],
)
def test_stdio_pool_key_is_salted_with_the_user_for_per_user_sources(monkeypatch, scope, caller, expected):
    """A `scope='per_user'` source must never share a warm subprocess between
    two users, even when their resolved credentials coincide (no stored
    secret, or an identical value): the upstream process can retain
    per-session state. `_open_session` is the one seam that knows both the
    scope and the caller, so it salts the pool key there."""
    from connectors.mcp import session_pool

    pool = _RecordingPool()
    monkeypatch.setattr(session_pool, "pool_enabled", lambda: True)
    monkeypatch.setattr(session_pool, "get_pool", lambda: pool)

    src = {"transport": "stdio", "command": "crm-mcp"}
    if scope is not None:
        src["scope"] = scope

    async def _drive():
        async with mcp_client._open_session(src, caller_user_id=caller):
            pass

    asyncio.run(_drive())
    assert pool.salts == [expected]


def test_installed_mcp_sdk_still_speaks_the_api_this_repo_calls():
    """The `mcp` range in pyproject must resolve an SDK this code can call.

    Both halves below broke at once when an unbounded `mcp>=1.28.1` resolved
    the 2.x SDK in CI, and both are import-time or first-call failures rather
    than one wrong answer: `connectors/mcp/client.py` is reached from
    `app.main`, so a missing symbol errors out every test that builds the app,
    and a signature change surfaces only when a real MCP server is dialled --
    in production, past every mock in this file.
    """
    import importlib
    import inspect

    # Five modules import FastMCP from here (app/api/mcp_streamable.py,
    # app/api/mcp_http.py, app/api/mcp/foundation_tools.py,
    # app/api/mcp/tools_generator.py, cli/mcp/server.py). 2.x moved it.
    importlib.import_module("mcp.server.fastmcp")

    from connectors.mcp import client as mcp_client

    params = inspect.signature(mcp_client.streamablehttp_client).parameters
    assert "url" in params
    # `_open_session` calls this as `streamablehttp_client(url, headers=...)`.
    assert "headers" in params, (
        "the bound streamable-HTTP transport no longer accepts `headers=` -- "
        "the callsite in connectors/mcp/client.py would raise TypeError and "
        "send no auth header; migrate the callsite, do not re-alias the symbol"
    )


def test_the_renamed_transport_is_not_a_drop_in_for_the_old_one():
    """Guards against 'fixing' the rename by aliasing the new name to the old.

    That looks like a one-line import fix and passes every mocked test in this
    file, because they all monkeypatch the symbol away. It is not a fix: the
    new entry point takes an `http_client: httpx.AsyncClient` where the old one
    takes `headers`, so the alias turns every HTTP MCP connection into a
    `TypeError` and the resolved bearer/basic header is never sent.
    """
    import inspect

    sh = pytest.importorskip("mcp.client.streamable_http")
    new = getattr(sh, "streamable_http_client", None)
    if new is None:  # pragma: no cover - SDK predates the rename
        pytest.skip("installed SDK has only the old spelling")

    new_params = inspect.signature(new).parameters
    old_params = inspect.signature(sh.streamablehttp_client).parameters

    assert "headers" in old_params
    assert "headers" not in new_params, (
        "the two spellings now take the same arguments -- if that is real, "
        "this guard and the `mcp<2` cap can both be revisited together"
    )
