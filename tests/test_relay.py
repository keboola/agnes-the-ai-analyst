"""Sandbox-local loopback relay (`app/chat/relay.py`).

The relay is the only thing inside the E2B sandbox that ever holds a broker
ticket, and it must hold it in memory only — never in `os.environ` (which
subprocesses inherit and which can be dumped via `env`/`/proc/*/environ`)
and never on disk. These tests pin that guarantee plus the fail-closed
behavior before any ticket has been pushed.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.chat.relay import Relay


class _CapturingClient:
    """Records the last post(url, json=..., content=..., headers=...) call."""

    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, content=None, headers=None):
        self.calls.append({"url": url, "json": json, "content": content, "headers": headers})

        class _Resp:
            status_code = 200
            content = b"{}"
            reason_phrase = "OK"
            headers: dict = {}

        return _Resp()


def test_relay_wraps_agnes_api_native_call_into_envelope():
    """The broker's /agnes-api route replays a {method, path, body} envelope.
    The in-sandbox CLI makes NATIVE REST calls (e.g. GET /api/v2/catalog) and
    the relay always POSTs to the broker, so the native method + target path
    (with query string) MUST be carried in the envelope — otherwise the call
    arrives as POST /api/broker/agnes-api/<subpath> and 405s."""

    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward("/agnes-api/api/v2/catalog?refresh=0", b"", {"x": "y"}, method="GET")
        return fake.calls[-1]

    call = asyncio.run(_run())
    # Envelope POSTed to the EXACT broker route, not the native subpath.
    assert call["url"] == "http://agnes:8000/api/broker/agnes-api"
    assert call["json"] == {"method": "GET", "path": "/api/v2/catalog?refresh=0", "body": None}
    assert call["headers"]["Authorization"] == "Bearer TOKMAIN"


def test_relay_wraps_agnes_mcp_post_body_into_envelope():
    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward("/agnes-mcp/api/query", json.dumps({"sql": "SELECT 1"}).encode(), {}, method="POST")
        return fake.calls[-1]

    call = asyncio.run(_run())
    assert call["url"] == "http://agnes:8000/api/broker/agnes-mcp"
    assert call["json"] == {"method": "POST", "path": "/api/query", "body": {"sql": "SELECT 1"}}
    assert call["headers"]["Authorization"] == "Bearer TOKMCP"  # mcp scope ticket


def test_relay_anthropic_stays_transparent_not_enveloped():
    """The /anthropic leg is a transparent external proxy — it must NOT be
    wrapped in an envelope; the raw body + SDK headers pass through to the
    broker's Anthropic proxy at the native subpath."""

    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward(
            "/anthropic/v1/messages", b'{"model":"x"}', {"content-type": "application/json"}, method="POST"
        )
        return fake.calls[-1]

    call = asyncio.run(_run())
    assert call["url"] == "http://agnes:8000/api/broker/anthropic/v1/messages"
    assert call["json"] is None  # raw content, not an envelope
    assert call["content"] == b'{"model":"x"}'


def test_relay_holds_tickets_in_memory_only():
    r = Relay(server_url="http://agnes:8000")
    r.set_tickets(main="TOKMAIN", mcp="TOKMCP")

    # the relay must not export tickets to the process environment
    assert "TOKMAIN" not in os.environ.values()
    assert "TOKMCP" not in os.environ.values()
    # ...but they must be held in-memory on the instance
    assert r._main_ticket == "TOKMAIN"
    assert r._mcp_ticket == "TOKMCP"


def test_relay_refuses_before_tickets():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_refuses_after_disarm():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        r.disarm()
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_refuses_wrong_scope_ticket():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        # only an mcp ticket is set; /agnes-api requires the main scope
        r.set_tickets(main="", mcp="TOKMCP")
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_forwards_sdk_headers_and_swaps_credential():
    """The relay must forward the caller's headers (Content-Type,
    anthropic-version, …) so the broker's Anthropic proxy can pass them to
    api.anthropic.com, while dropping hop-by-hop framing and REPLACING the
    dummy credential with the real ticket (Devin review on #849)."""
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        content = b"{}"
        reason_phrase = "OK"
        headers: dict = {}

    class _FakeClient:
        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResp()

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        r._client = _FakeClient()
        inbound = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer dummy-sandbox-key",
            "x-api-key": "dummy-sandbox-key",
            "host": "127.0.0.1",
            "content-length": "2",
        }
        await r._forward("/anthropic/v1/messages", b"{}", inbound)

    asyncio.run(_run())
    h = captured["headers"]
    lower = {k.lower() for k in h}
    # SDK headers survive
    assert h["content-type"] == "application/json"
    assert h["anthropic-version"] == "2023-06-01"
    # ticket replaces the dummy credential; the dummy never leaks upward
    assert h["Authorization"] == "Bearer TOKMAIN"
    assert "dummy-sandbox-key" not in " ".join(h.values())
    # hop-by-hop framing + the caller's dummy x-api-key are dropped
    assert "host" not in lower
    assert "content-length" not in lower
    assert "x-api-key" not in lower


def test_relay_outbound_client_has_generous_read_timeout():
    """Regression: the relay's outbound httpx client proxies LLM completions
    on the `/anthropic` leg. httpx's 5s default read timeout would abort every
    real completion (the sandbox-side twin of the broker's timeout bug), so
    ``start()`` must build the client with a generous read timeout."""
    import httpx

    captured: dict = {}

    orig = httpx.AsyncClient

    def _spy(*a, **k):
        captured["timeout"] = k.get("timeout")
        return orig(*a, **k)

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        import app.chat.relay as relay_mod

        real = relay_mod.httpx.AsyncClient
        relay_mod.httpx.AsyncClient = _spy  # type: ignore[assignment]
        try:
            port = await r.start(port_hint=0)
            assert port > 0
        finally:
            relay_mod.httpx.AsyncClient = real  # type: ignore[assignment]
            await r.stop()

    asyncio.run(_run())
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.read is not None and t.read >= 60.0, t
