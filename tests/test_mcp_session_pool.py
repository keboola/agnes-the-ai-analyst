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


class TestEventLoopBinding:
    """Everything an entry holds — the session, its keeper task, the event and
    the futures — belongs to the loop that created it. The sync wrappers
    (`list_tools`, `call_tool`, `_materialize_one_tool`) run one `asyncio.run`
    per call, so an entry cached by the previous call is a session whose loop
    is gone and whose subprocess was torn down with it."""

    def test_a_session_from_a_closed_loop_is_never_handed_out(self, spawns):
        pool = session_pool.get_pool()

        async def drive():
            async with pool.acquire(_params({"T": "x"})) as session:
                return session

        first = asyncio.run(drive())
        second = asyncio.run(drive())

        assert spawns["n"] == 2, "a session whose event loop is closed was reused"
        assert first is not second

    def test_a_closed_loop_leaves_no_bookkeeping_behind(self, spawns):
        """Per-loop bookkeeping must not accumulate one dead loop per call."""
        pool = session_pool.get_pool()

        async def drive():
            async with pool.acquire(_params({"T": "x"})):
                pass
            return len(pool._entries), len(pool._states)

        asyncio.run(drive())
        asyncio.run(drive())
        entries, states = asyncio.run(drive())
        assert (entries, states) == (1, 1), "bookkeeping from a closed loop is still held"


class TestReservation:
    def test_a_just_spawned_session_is_not_closed_to_satisfy_the_cap(self, spawns, monkeypatch):
        """The cap sweep runs before the caller can take `entry.lock`, so a
        brand-new entry looks idle. With every other entry busy it is the only
        eviction candidate — and the caller would be handed a dead process."""
        monkeypatch.setattr(session_pool, "MAX_SESSIONS", 1)

        async def drive():
            pool = session_pool.get_pool()
            busy = asyncio.Event()
            release = asyncio.Event()

            async def hold():
                async with pool.acquire(_params({"T": "busy"})):
                    busy.set()
                    await release.wait()

            holder = asyncio.create_task(hold())
            await busy.wait()
            closed_before = spawns["closed"]
            async with pool.acquire(_params({"T": "fresh"})):
                assert spawns["closed"] == closed_before, "the session handed to us was closed under us"
            release.set()
            await holder

        asyncio.run(drive())

    def test_an_idle_sweep_spares_a_session_a_caller_is_holding(self, spawns, monkeypatch):
        monkeypatch.setattr(session_pool, "IDLE_TIMEOUT_S", 0.0)

        async def drive():
            pool = session_pool.get_pool()
            busy = asyncio.Event()
            release = asyncio.Event()

            async def hold():
                async with pool.acquire(_params({"T": "busy"})):
                    busy.set()
                    await release.wait()

            holder = asyncio.create_task(hold())
            await busy.wait()
            await asyncio.sleep(0.01)
            closed_before = spawns["closed"]
            async with pool.acquire(_params({"T": "other"})):  # any acquire sweeps
                pass
            assert spawns["closed"] == closed_before, "a session in use was swept as idle"
            release.set()
            await holder

        asyncio.run(drive())

    def test_a_caller_queued_behind_a_failure_is_not_given_the_dead_session(self, spawns):
        """Serialization means the second caller reserves the entry, then waits
        on its lock. If the first call kills the session in the meantime, the
        waiter must not run on the process that just died."""

        async def drive():
            pool = session_pool.get_pool()
            first_inside = asyncio.Event()

            async def failing():
                with pytest.raises(RuntimeError):
                    async with pool.acquire(_params({"T": "x"})):
                        first_inside.set()
                        await asyncio.sleep(0.02)
                        raise RuntimeError("transport died")

            async def queued():
                await first_inside.wait()
                async with pool.acquire(_params({"T": "x"})) as session:
                    return session

            first = asyncio.create_task(failing())
            second = asyncio.create_task(queued())
            await first
            await second

        asyncio.run(drive())
        assert spawns["n"] == 2, "the queued caller inherited the session that had just died"


class TestSpawnConcurrency:
    def test_a_slow_start_does_not_block_an_unrelated_source(self, monkeypatch):
        """A ~6s startup for one source must not queue every other source's
        tool calls behind it — which is what a pool-wide lock held across the
        spawn does."""
        gate: dict = {}

        @asynccontextmanager
        async def _slow_stdio_client(params):
            if (params.env or {}).get("T") == "slow":
                await gate["release"].wait()
            yield (object(), object())

        class _FakeClientSession:
            def __init__(self, read, write):
                self._s = _FakeSession()

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(session_pool, "stdio_client", _slow_stdio_client)
        monkeypatch.setattr(session_pool, "ClientSession", _FakeClientSession)

        async def drive():
            pool = session_pool.get_pool()
            gate["release"] = asyncio.Event()

            async def slow():
                async with pool.acquire(_params({"T": "slow"})):
                    pass

            slow_task = asyncio.create_task(slow())
            await asyncio.sleep(0)  # let it reach the spawn
            async with pool.acquire(_params({"T": "fast"})):
                pass
            gate["release"].set()
            await slow_task

        asyncio.run(asyncio.wait_for(drive(), timeout=5))

    def test_two_callers_for_one_spec_start_one_process(self, spawns):
        async def drive():
            pool = session_pool.get_pool()

            async def call():
                async with pool.acquire(_params({"T": "x"})):
                    await asyncio.sleep(0)

            await asyncio.gather(call(), call())

        asyncio.run(drive())
        assert spawns["n"] == 1, "a concurrent second caller started a duplicate process"


class TestCancellation:
    def test_a_timed_out_call_evicts_the_session(self, spawns):
        """A caller that wraps the upstream call in `asyncio.wait_for` (the
        admin connect probe does) unwinds through the pool with
        `CancelledError`, which is not an `Exception`. The session is left with
        a request in flight upstream, so the next caller must not inherit it."""

        async def drive():
            pool = session_pool.get_pool()

            async def slow_call():
                async with pool.acquire(_params({"T": "x"})):
                    await asyncio.sleep(5)

            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(slow_call(), timeout=0.05)
            async with pool.acquire(_params({"T": "x"})):
                pass

        asyncio.run(drive())
        assert spawns["n"] == 2, "a session abandoned mid-call was handed to the next caller"


class TestShutdownWiring:
    def test_the_app_lifespan_closes_the_pool(self):
        """`close_all` is documented for process shutdown; nothing but tests
        called it, so a graceful stop left the subprocesses to process death."""
        import inspect

        import app.main as main

        src = inspect.getsource(main.lifespan)
        assert "session_pool" in src and "close_all" in src, (
            "the FastAPI lifespan teardown does not drain the MCP session pool"
        )


class TestSwitch:
    def test_the_pool_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("AGNES_MCP_SESSION_POOL", "0")
        assert session_pool.pool_enabled() is False

    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("AGNES_MCP_SESSION_POOL", raising=False)
        assert session_pool.pool_enabled() is True

    def test_the_toggle_is_registered_as_a_switch(self):
        """CONTRIBUTING.md's sync-map: a user-visible switch is a registry
        entry, never a hand-rolled `os.environ.get` with inline parsing."""
        from app.switches import get_switch

        switch = get_switch("mcp_session_pool")
        assert switch.env_var == "AGNES_MCP_SESSION_POOL"
        assert switch.kind == "bool"
        assert switch.default is True
        assert switch.config_keys == ("mcp", "session_pool")

    def test_it_is_documented_for_operators(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parent.parent / "docs" / "feature-flags.md").read_text(encoding="utf-8")
        assert "mcp_session_pool" in doc

    def test_the_config_key_is_honored_not_just_the_env_var(self, monkeypatch):
        """Proves the read goes through the registry: with no env var set, the
        `mcp.session_pool` config key alone turns it off."""
        import app.instance_config as instance_config

        monkeypatch.delenv("AGNES_MCP_SESSION_POOL", raising=False)
        real = instance_config.get_value

        def _fake(*keys, default=None):
            if keys == ("mcp", "session_pool"):
                return False
            return real(*keys, default=default)

        monkeypatch.setattr(instance_config, "get_value", _fake)
        assert session_pool.pool_enabled() is False

    def test_an_off_token_the_old_parser_missed_now_turns_it_off(self, monkeypatch):
        """The hand-rolled parser accepted `0`/`false`/`no` only, so `off`
        read as ON. The registry's shared rule covers it."""
        monkeypatch.setenv("AGNES_MCP_SESSION_POOL", "off")
        assert session_pool.pool_enabled() is False
