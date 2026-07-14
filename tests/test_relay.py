"""Sandbox-local loopback relay (`app/chat/relay.py`).

The relay is the only thing inside the E2B sandbox that ever holds a broker
ticket, and it must hold it in memory only — never in `os.environ` (which
subprocesses inherit and which can be dumped via `env`/`/proc/*/environ`)
and never on disk. These tests pin that guarantee plus the fail-closed
behavior before any ticket has been pushed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.chat.relay import Relay


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
