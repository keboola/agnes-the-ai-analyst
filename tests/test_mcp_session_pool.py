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

    async def call_tool(self, name, arguments=None, **kwargs):
        if name == "stuck":
            await asyncio.Event().wait()  # never returns
        return {"name": name, "arguments": arguments}


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

    def test_the_per_user_salt_separates_identical_specs(self):
        """A `scope='per_user'` source must not share one warm process between
        users even when their resolved credentials coincide — no stored secret,
        or an identical shared value — because the upstream process can retain
        per-session state. The caller's id salts the key, so the isolation is
        structural, not an accident of differing tokens."""
        p = _params({"T": "same-for-everyone"})
        alice = session_pool.spec_key(p, salt="user:alice")
        bob = session_pool.spec_key(p, salt="user:bob")
        assert alice != bob
        assert session_pool.spec_key(p, salt="user:alice") == alice
        assert session_pool.spec_key(p) != alice, "an unsalted caller must not collide with a salted one"

    def test_the_salt_is_not_in_the_key_in_the_clear(self):
        key = session_pool.spec_key(_params(), salt="user:alice@example.com")
        assert "alice" not in key


class TestEnvNumber:
    """`_env_number` must be total — it runs at import, where an exception
    takes the whole connector down (the exact outcome its docstring promises
    to avoid)."""

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
    def test_a_non_finite_value_falls_back_for_a_float_tunable(self, monkeypatch, raw):
        """`float('nan')` parses fine, and then `IDLE_TIMEOUT_S = nan` makes
        every idle-expiry comparison false — sessions never age out."""
        monkeypatch.setenv("AGNES_MCP_TEST_KNOB", raw)
        assert session_pool._env_number("AGNES_MCP_TEST_KNOB", 180.0) == 180.0

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_a_non_finite_value_survives_the_int_conversion(self, monkeypatch, raw):
        """`MAX_SESSIONS` wraps `_env_number` in `int()`: `int(float('nan'))`
        raises ValueError and `int(float('inf'))` raises OverflowError."""
        monkeypatch.setenv("AGNES_MCP_TEST_KNOB", raw)
        assert int(session_pool._env_number("AGNES_MCP_TEST_KNOB", 8.0)) == 8

    def test_non_finite_tunables_do_not_crash_the_import(self, monkeypatch):
        """The real thing the guard exists for: re-executing the module with
        poisoned env must land every knob on its default, not raise."""
        import importlib

        poisoned = {
            "AGNES_MCP_SESSION_POOL_MAX": "nan",
            "AGNES_MCP_SESSION_IDLE_S": "inf",
            "AGNES_MCP_SESSION_SPAWN_TIMEOUT_S": "-inf",
            "AGNES_MCP_SESSION_CALL_TIMEOUT_S": "nan",
        }
        for var, raw in poisoned.items():
            monkeypatch.setenv(var, raw)
        try:
            mod = importlib.reload(session_pool)
            assert mod.MAX_SESSIONS == 8
            assert mod.IDLE_TIMEOUT_S == 180.0
            assert mod.SPAWN_TIMEOUT_S == 60.0
            assert mod.CALL_TIMEOUT_S == 300.0
        finally:
            for var in poisoned:
                monkeypatch.delenv(var)
            importlib.reload(session_pool)


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

    def test_two_salted_users_with_identical_specs_get_separate_processes(self, spawns):
        """The pool half of the per-user salt: same launch spec, different
        `key_salt` — two processes, and each user's process is reused only
        under their own salt."""

        async def drive():
            pool = session_pool.get_pool()
            for salt in ("user:alice", "user:bob", "user:alice"):
                async with pool.acquire(_params({"T": "x"}), key_salt=salt):
                    pass

        asyncio.run(drive())
        assert spawns["n"] == 2, "two users shared one warm process (or reuse per user broke)"


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


class TestHungStartup:
    """An upstream that starts but never answers `initialize` must not wedge
    every caller of its spec forever: the spawner's wait was unbounded, and
    concurrent callers park on the shared single-flight marker behind it."""

    def _hang_first_initialize(self, monkeypatch):
        counter = {"n": 0, "closed": 0}
        hang = {"remaining": 1}

        @asynccontextmanager
        async def _fake_stdio_client(params):
            counter["n"] += 1
            try:
                yield (object(), object())
            finally:
                counter["closed"] += 1

        class _NeverReady:
            async def initialize(self):
                await asyncio.Event().wait()  # never answers

        class _FakeClientSession:
            def __init__(self, read, write):
                if hang["remaining"] > 0:
                    hang["remaining"] -= 1
                    self._s = _NeverReady()
                else:
                    self._s = _FakeSession()

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(session_pool, "stdio_client", _fake_stdio_client)
        monkeypatch.setattr(session_pool, "ClientSession", _FakeClientSession)
        return counter

    def test_a_spawn_that_never_initializes_times_out_and_is_cleaned_up(self, monkeypatch):
        monkeypatch.setattr(session_pool, "SPAWN_TIMEOUT_S", 0.05)
        counter = self._hang_first_initialize(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()
            with pytest.raises(TimeoutError, match="initializ"):
                async with pool.acquire(_params({"T": "x"})):
                    pass  # pragma: no cover
            assert not pool._state().spawning, "the spawn marker was left behind"
            # Cancelling the keeper is what unwinds the transport and kills
            # the half-started subprocess; wait for the stand-in to close.
            await _wait_until(lambda: counter["closed"] >= 1)
            assert counter["closed"] == 1, "the half-spawned process was orphaned"
            # The next acquire must attempt (and here: complete) a fresh spawn.
            async with pool.acquire(_params({"T": "x"})):
                pass

        asyncio.run(asyncio.wait_for(drive(), timeout=5))
        assert counter["n"] == 2

    def test_a_caller_waiting_on_a_hung_spawn_recovers(self, monkeypatch):
        """The second caller parked on the single-flight marker must not hang
        with the spawner: when the spawn times out the marker is released, and
        the waiter retries with a fresh spawn of its own."""
        monkeypatch.setattr(session_pool, "SPAWN_TIMEOUT_S", 0.05)
        counter = self._hang_first_initialize(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()

            async def call():
                async with pool.acquire(_params({"T": "x"})):
                    pass

            results = await asyncio.gather(call(), call(), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            assert len(failures) == 1, f"expected exactly the spawner to fail, got {results!r}"
            assert isinstance(failures[0], TimeoutError)

        asyncio.run(asyncio.wait_for(drive(), timeout=5))
        assert counter["n"] == 2, "the waiter did not retry with a fresh spawn"


class TestStuckCall:
    def test_a_call_past_the_ceiling_errors_and_evicts(self, spawns, monkeypatch):
        """One upstream call that never returns must cost that one call, not
        the source: the pooled `call_tool` is bounded, and the timeout rides
        the existing eviction path so the process is closed."""
        monkeypatch.setattr(session_pool, "CALL_TIMEOUT_S", 0.05)

        async def drive():
            pool = session_pool.get_pool()
            with pytest.raises(TimeoutError, match="did not return"):
                async with pool.acquire(_params({"T": "x"})) as session:
                    await session.call_tool("stuck", {})
            # Evicted and closed: the next acquire starts a fresh process.
            async with pool.acquire(_params({"T": "x"})) as session:
                result = await session.call_tool("fine", {})
                assert result["name"] == "fine"

        asyncio.run(asyncio.wait_for(drive(), timeout=5))
        assert spawns["n"] == 2, "the session that swallowed a call was handed out again"
        assert spawns["closed"] >= 1

    def test_a_caller_supplied_read_timeout_is_not_overridden(self, monkeypatch):
        """A caller that chose its own `read_timeout_seconds` keeps it — the
        pool's default is a default, not a clamp."""
        from datetime import timedelta

        monkeypatch.setattr(session_pool, "CALL_TIMEOUT_S", 0.05)
        seen = {}

        class _Recording:
            async def initialize(self):
                pass

            async def call_tool(self, name, arguments=None, read_timeout_seconds=None, **kwargs):
                seen["read_timeout_seconds"] = read_timeout_seconds
                return {"name": name}

        @asynccontextmanager
        async def _fake_stdio_client(params):
            yield (object(), object())

        class _FakeClientSession:
            def __init__(self, read, write):
                self._s = _Recording()

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(session_pool, "stdio_client", _fake_stdio_client)
        monkeypatch.setattr(session_pool, "ClientSession", _FakeClientSession)

        async def drive():
            pool = session_pool.get_pool()
            async with pool.acquire(_params({"T": "x"})) as session:
                await session.call_tool("t", {}, read_timeout_seconds=timedelta(seconds=42))

        asyncio.run(drive())
        assert seen["read_timeout_seconds"] == timedelta(seconds=42)

    def test_a_waiter_behind_a_stuck_call_gets_a_clear_error_not_a_hang(self, spawns, monkeypatch):
        """Calls on one session are serialized; the wait for that lock was
        unbounded, so one wedged call queued every later caller of the source
        forever. The waiter must fail with a clear busy error instead."""
        monkeypatch.setattr(session_pool, "CALL_TIMEOUT_S", 0.05)
        monkeypatch.setattr(session_pool, "CLOSE_TIMEOUT_S", 0.0)  # lock budget = CALL + 2*CLOSE

        async def drive():
            pool = session_pool.get_pool()
            inside = asyncio.Event()
            release = asyncio.Event()

            async def holder():
                async with pool.acquire(_params({"T": "x"})):
                    inside.set()
                    await release.wait()  # holds the entry lock, no tool call

            async def waiter():
                await inside.wait()
                with pytest.raises(TimeoutError, match="busy"):
                    async with pool.acquire(_params({"T": "x"})):
                        pass  # pragma: no cover

            holder_task = asyncio.create_task(holder())
            await waiter()
            release.set()
            await holder_task

        asyncio.run(asyncio.wait_for(drive(), timeout=5))
        assert spawns["n"] == 1, "the busy error should not have evicted the healthy holder"


class _GatedTransport:
    """Test double whose startup and/or teardown block on test-owned gates.

    A spec whose env carries ``T='slow-start'`` blocks inside the transport's
    entry until ``gates['start']`` is set; ``T='slow-close'`` blocks the
    teardown on ``gates['close']``. Gates are created by the test inside the
    running loop.
    """

    def __init__(self, monkeypatch) -> None:
        self.counter = {"n": 0, "closed": 0}
        self.gates: dict = {}
        counter, gates = self.counter, self.gates

        @asynccontextmanager
        async def _fake_stdio_client(params):
            counter["n"] += 1
            tag = (params.env or {}).get("T", "")
            try:
                if tag == "slow-start":
                    await gates["start"].wait()
                yield (object(), object())
            finally:
                if tag == "slow-close":
                    await gates["close"].wait()
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


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.005)


class TestIdleReaper:
    def test_an_idle_session_is_closed_with_no_further_traffic(self, spawns, monkeypatch):
        """The idle timeout must fire on its own. Sweeping only from the next
        acquire means a quiet instance holds warm subprocesses (each with the
        upstream's whole import tree resident) until the process exits — the
        opposite of what the switch text and the operator docs promise."""
        monkeypatch.setattr(session_pool, "IDLE_TIMEOUT_S", 0.0)

        async def drive():
            pool = session_pool.get_pool()
            async with pool.acquire(_params({"T": "x"})):
                pass
            await _wait_until(lambda: spawns["closed"] >= 1)
            return len(pool._entries)

        assert asyncio.run(drive()) == 0, "an idle session outlived its timeout with no traffic"
        assert spawns["closed"] == 1


class TestSelfDeath:
    def test_a_session_whose_upstream_died_is_not_handed_out(self, spawns):
        """When the keeper exits on its own (upstream crashed or exited while
        idle), the entry must be detached rather than left registered —
        otherwise the next tool call runs on a dead transport and fails before
        a healthy replacement is started."""

        async def drive():
            pool = session_pool.get_pool()
            params = _params({"T": "x"})
            async with pool.acquire(params):
                pass
            entry = pool._entries[session_pool.spec_key(params)]
            # The upstream going away on its own: the keeper unwinds and
            # resolves `closed`, with the entry still registered.
            entry.close.set()
            await asyncio.wait_for(asyncio.shield(entry.closed), timeout=1)
            async with pool.acquire(params):
                pass

        asyncio.run(drive())
        assert spawns["n"] == 2, "the dead session was handed to the next caller"


class TestAbandonedCheckout:
    def test_a_cancelled_checkout_releases_its_reservation(self, monkeypatch):
        """`_checkout` reserves the entry, then pays for stale teardown before
        returning; only `acquire`'s finally releases the claim, and it is
        unreachable until `_checkout` returns. A caller cancelled inside that
        teardown wait (its `asyncio.wait_for` firing — the admin connect probe
        does exactly this) must not leave the reservation behind: a permanently
        reserved entry is permanently in-use, so no sweep can ever close it,
        and once enough pile up the cap stops bounding the pool."""
        transport = _GatedTransport(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()
            transport.gates["close"] = asyncio.Event()
            slow, fast = _params({"T": "slow-close"}), _params({"T": "fast"})
            async with pool.acquire(slow):
                pass
            async with pool.acquire(fast):
                pass
            # Backdate the slow-closing entry so the next checkout sweeps it.
            pool._entries[session_pool.spec_key(slow)].last_used -= 10_000.0

            async def caller():
                async with pool.acquire(fast):
                    pass

            task = asyncio.create_task(caller())
            fresh = pool._entries[session_pool.spec_key(fast)]
            await _wait_until(lambda: fresh.reserved == 1)
            assert fresh.reserved == 1, "test setup: the caller should be inside the teardown wait"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            reserved = fresh.reserved
            transport.gates["close"].set()
            await asyncio.sleep(0.01)
            return reserved

        assert asyncio.run(drive()) == 0, "an abandoned checkout left its reservation behind"


class TestAbandonedStartup:
    def test_a_call_abandoned_mid_spawn_kills_the_starting_process(self, monkeypatch):
        """`await ready` was guarded by `except Exception`; CancelledError is a
        BaseException, so a caller that gave up during the ~6 s startup (an
        `asyncio.wait_for`, an HTTP disconnect) left the keeper parked forever
        with a live subprocess nothing could ever reach — not the sweeps (the
        entry was never registered) and not `close_all`."""
        transport = _GatedTransport(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()
            transport.gates["start"] = asyncio.Event()

            async def caller():
                async with pool.acquire(_params({"T": "slow-start"})):
                    pass

            task = asyncio.create_task(caller())
            await _wait_until(lambda: transport.counter["n"] >= 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The start gate is never opened: only cancelling the keeper can
            # unwind the transport. Wait for the subprocess stand-in to close.
            await _wait_until(lambda: transport.counter["closed"] >= 1)
            assert transport.counter["closed"] == 1, "the starting subprocess was orphaned"
            assert not pool._state().spawning, "the spawn marker was left behind"

        asyncio.run(drive())

    def test_a_cancellation_between_spawn_and_registration_closes_the_entry(self, monkeypatch):
        """Between `_spawn` returning and the entry landing in the pool there
        is one more await (the pool guard). A cancellation delivered exactly
        there orphaned the fresh entry: registered nowhere, `close` never
        set, subprocess alive until process exit."""
        transport = _GatedTransport(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()
            state = pool._state()
            transport.gates["start"] = asyncio.Event()

            async def caller():
                async with pool.acquire(_params({"T": "slow-start"})):
                    pass

            task = asyncio.create_task(caller())
            await _wait_until(lambda: transport.counter["n"] >= 1)
            await state.guard.acquire()  # hold the pool guard...
            transport.gates["start"].set()  # ...and only then let the spawn finish
            await asyncio.sleep(0.05)  # caller is now blocked on the guard
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            state.guard.release()
            await _wait_until(lambda: transport.counter["closed"] >= 1)
            assert transport.counter["closed"] == 1, "the just-spawned entry was orphaned"
            assert not state.entries and not state.spawning

        asyncio.run(drive())


class TestCloseRace:
    def test_close_all_waits_out_an_in_flight_spawn(self, monkeypatch):
        """`aclose` snapshotted `entries` only: a spawn completing after the
        sweep re-inserted its entry, so a shutdown racing a starting session
        left that subprocess unclosed and a stale loop-state behind."""
        transport = _GatedTransport(monkeypatch)

        async def drive():
            pool = session_pool.get_pool()
            transport.gates["start"] = asyncio.Event()

            async def caller():
                async with pool.acquire(_params({"T": "slow-start"})):
                    pass

            task = asyncio.create_task(caller())
            await _wait_until(lambda: transport.counter["n"] >= 1)
            closer = asyncio.create_task(session_pool.close_all())
            await asyncio.sleep(0.01)  # closer is now waiting on the spawn
            transport.gates["start"].set()
            await task
            await closer
            return len(pool._entries)

        assert asyncio.run(drive()) == 0, "a spawn racing shutdown re-registered its entry"
        assert transport.counter["closed"] == transport.counter["n"] == 1


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
