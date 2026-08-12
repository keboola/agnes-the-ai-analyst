"""Reuse of warm stdio MCP sessions.

What is under test is the pool's bookkeeping — when it reuses, when it must
NOT, and what it does with a broken session. The SDK's transport is stood in
for, because the thing that can be wrong here is the keying and the
lifecycle, not whether anyio moves bytes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp import StdioServerParameters

from connectors.mcp import session_pool


def _params(env: dict | None = None, args: list | None = None) -> StdioServerParameters:
    return StdioServerParameters(command="true", args=args or [], env=env)


class _FakeSession:
    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True


@pytest.fixture
def spawns(monkeypatch):
    """Replace the SDK transport; count how many processes would have started."""
    counter = {"n": 0, "closed": 0}

    @asynccontextmanager
    async def _fake_stdio_client(params):
        counter["n"] += 1
        try:
            yield (object(), object())
        finally:
            counter["closed"] += 1

    class _FakeClientSession:
        def __init__(self, read, write):
            self._s = _FakeSession()

        async def __aenter__(self):
            return self._s

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(session_pool, "stdio_client", _fake_stdio_client)
    monkeypatch.setattr(session_pool, "ClientSession", _FakeClientSession)
    return counter


@pytest.fixture(autouse=True)
def _fresh_pool():
    yield
    asyncio.run(session_pool.close_all())


class TestKeying:
    def test_the_resolved_secret_is_part_of_the_key(self):
        """Otherwise a rotated token would keep serving from a process still
        holding the old one — and, worse, a per-user source could hand one
        analyst's warm process to another."""
        a = session_pool.spec_key(_params({"KBC_STORAGE_TOKEN": "user-a"}))
        b = session_pool.spec_key(_params({"KBC_STORAGE_TOKEN": "user-b"}))
        assert a != b

    def test_the_runner_version_is_part_of_the_key(self):
        a = session_pool.spec_key(_params(args=["--from", "pkg==1.0.0"]))
        b = session_pool.spec_key(_params(args=["--from", "pkg==2.0.0"]))
        assert a != b

    def test_the_key_does_not_contain_the_secret_in_the_clear(self):
        key = session_pool.spec_key(_params({"KBC_STORAGE_TOKEN": "super-secret-value"}))
        assert "super-secret-value" not in key


class TestReuse:
    def test_the_same_spec_starts_one_process(self, spawns):
        async def drive():
            pool = session_pool.get_pool()
            for _ in range(3):
                async with pool.acquire(_params({"T": "x"})):
                    pass

        asyncio.run(drive())
        assert spawns["n"] == 1, "a warm session was not reused"

    def test_a_rotated_secret_starts_a_new_one(self, spawns):
        async def drive():
            pool = session_pool.get_pool()
            async with pool.acquire(_params({"T": "old"})):
                pass
            async with pool.acquire(_params({"T": "new"})):
                pass

        asyncio.run(drive())
        assert spawns["n"] == 2

    def test_a_failed_call_evicts_rather_than_reusing_a_broken_process(self, spawns):
        async def drive():
            pool = session_pool.get_pool()
            with pytest.raises(RuntimeError):
                async with pool.acquire(_params({"T": "x"})):
                    raise RuntimeError("transport died")
            async with pool.acquire(_params({"T": "x"})):
                pass

        asyncio.run(drive())
        assert spawns["n"] == 2, "the broken session was handed to the next caller"
        assert spawns["closed"] >= 1

    def test_a_failure_is_not_retried_for_the_caller(self, spawns):
        """A retry inside the pool could run a mutating tool twice."""
        calls = {"n": 0}

        async def drive():
            pool = session_pool.get_pool()
            with pytest.raises(ValueError):
                async with pool.acquire(_params()):
                    calls["n"] += 1
                    raise ValueError("boom")

        asyncio.run(drive())
        assert calls["n"] == 1


class TestLifecycle:
    def test_an_idle_session_is_closed(self, spawns, monkeypatch):
        monkeypatch.setattr(session_pool, "IDLE_TIMEOUT_S", 0.0)

        async def drive():
            pool = session_pool.get_pool()
            async with pool.acquire(_params({"T": "x"})):
                pass
            await asyncio.sleep(0.01)
            async with pool.acquire(_params({"T": "y"})):  # any acquire sweeps
                pass

        asyncio.run(drive())
        assert spawns["closed"] >= 1

    def test_the_cap_closes_the_least_recently_used(self, spawns, monkeypatch):
        monkeypatch.setattr(session_pool, "MAX_SESSIONS", 2)

        async def drive():
            pool = session_pool.get_pool()
            for tag in ("a", "b", "c"):
                async with pool.acquire(_params({"T": tag})):
                    pass
            return len(pool._entries)

        live = asyncio.run(drive())
        assert live <= 2
        assert spawns["closed"] >= 1

    def test_close_all_leaves_nothing_running(self, spawns):
        async def drive():
            pool = session_pool.get_pool()
            async with pool.acquire(_params({"T": "x"})):
                pass
            await session_pool.close_all()
            return len(pool._entries)

        assert asyncio.run(drive()) == 0
        assert spawns["closed"] == spawns["n"]


class TestSwitch:
    def test_the_pool_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("AGNES_MCP_SESSION_POOL", "0")
        assert session_pool.pool_enabled() is False

    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("AGNES_MCP_SESSION_POOL", raising=False)
        assert session_pool.pool_enabled() is True
