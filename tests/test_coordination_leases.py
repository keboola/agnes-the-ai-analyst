"""Tests for the shared leader-lease helper (app/coordination/leases.py).

Covers the generic acquire-or-wait loop in isolation from any of its three
real consumers (Slack socket mode, Telegram poll, paused-sandbox sweep) —
those get their own wiring-level tests (test_slack_transport.py,
test_telegram_bot.py, test_chat_manager*.py). Uses plain ``asyncio.run``
per test (this repo has no pytest-asyncio plugin installed), matching
tests/test_worker_runtime.py's convention.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import reset_coordination_for_tests
from app.coordination.leases import run_with_lease


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


async def _cancel_and_await(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> None:
    """Poll `predicate()` until true or `timeout` elapses.

    Every lease backend call (`lease_acquire`/`lease_renew`/`lease_release`)
    now hops off the event loop via `asyncio.to_thread` (#Important-2), so a
    single `await asyncio.sleep(0)` scheduler tick no longer guarantees a
    lease operation — and anything gated behind it, like `start()` — has
    completed by the time control returns to the test. Poll instead of
    assuming synchronous completion.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    assert predicate(), f"condition not met within {timeout}s"


class _FakeConsumer:
    """Records start()/stop() calls for a single run_with_lease caller."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[str] = []
        self.active = False

    async def start(self) -> None:
        self.active = True
        self.events.append("start")

    async def stop(self) -> None:
        self.active = False
        self.events.append("stop")


class _FailingStartConsumer:
    """start() raises for its first ``fail_times`` calls, then succeeds —
    lets tests assert the release-lease-on-start-failure fix deterministically
    without racing a real upstream connect failure."""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.events: list[str] = []
        self.active = False

    async def start(self) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            self.events.append("start-fail")
            raise RuntimeError("boom: start failed")
        self.active = True
        self.events.append("start-ok")

    async def stop(self) -> None:
        self.active = False
        self.events.append("stop")


class _FlakyBackend:
    """A tiny CoordinationBackend test double with a controllable failure
    switch — lets tests force CoordinationUnavailable deterministically
    without racing real TTL expiry."""

    def __init__(self) -> None:
        self._leases: dict[str, tuple[str, float]] = {}
        self.fail_renew = False
        self.fail_acquire = False
        self.renew_calls = 0
        self.release_calls = 0

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def lease_acquire(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        if self.fail_acquire:
            raise CoordinationUnavailable("forced acquire failure")
        current = self._leases.get(name)
        if current is not None and current[1] > self._now():
            return False
        self._leases[name] = (holder_id, self._now() + ttl_s)
        return True

    def lease_renew(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        self.renew_calls += 1
        if self.fail_renew:
            raise CoordinationUnavailable("forced renew failure")
        current = self._leases.get(name)
        if current is None or current[0] != holder_id or current[1] <= self._now():
            return False
        self._leases[name] = (holder_id, self._now() + ttl_s)
        return True

    def lease_release(self, name: str, holder_id: str) -> None:
        self.release_calls += 1
        current = self._leases.get(name)
        if current is not None and current[0] == holder_id:
            del self._leases[name]


def test_memory_mode_acquires_immediately_and_starts():
    """Default (memory) backend: the first acquire attempt always succeeds
    (nothing else in-process contends), so start() fires practically
    instantly — preserving today's pre-lease behavior for the all-in-one
    default."""

    async def _run() -> None:
        consumer = _FakeConsumer("solo")
        task = asyncio.create_task(
            run_with_lease("solo-lease", "holder-a", ttl_s=1, start=consumer.start, stop=consumer.stop)
        )
        try:
            # lease_acquire hops off-loop via asyncio.to_thread, so this
            # polls rather than assuming a single scheduler tick suffices.
            await _wait_until(lambda: consumer.events == ["start"])
            assert consumer.active is True
        finally:
            await _cancel_and_await(task)

    asyncio.run(_run())


def test_two_consumers_one_lease_exactly_one_active():
    """Two fake consumers racing for the same lease name: at every observed
    instant at most one is active, and the loser never calls start()."""

    async def _run() -> None:
        a = _FakeConsumer("a")
        b = _FakeConsumer("b")
        ttl_s = 1
        task_a = asyncio.create_task(
            run_with_lease("shared-lease", "holder-a", ttl_s=ttl_s, start=a.start, stop=a.stop)
        )
        task_b = asyncio.create_task(
            run_with_lease("shared-lease", "holder-b", ttl_s=ttl_s, start=b.start, stop=b.stop)
        )
        try:
            # Let both loops run a few polling cycles.
            await _wait_until(lambda: a.active or b.active)
            await asyncio.sleep(0.3)
            assert not (a.active and b.active), "both consumers active at once"
            assert a.active or b.active, "neither consumer ever acquired the lease"
            # Exactly one of the two ever started. Which one wins is no
            # longer deterministic by task-creation order now that
            # lease_acquire hops off-loop via asyncio.to_thread (both
            # consumers' acquire attempts race across real threads) — assert
            # mutual exclusion of the outcome, not a specific winner.
            assert (a.events == ["start"] and b.events == []) or (b.events == ["start"] and a.events == []), (
                f"expected exactly one winner, got a={a.events!r} b={b.events!r}"
            )
        finally:
            await _cancel_and_await(task_a)
            await _cancel_and_await(task_b)

    asyncio.run(_run())


def test_holder_death_takeover_within_ttl():
    """A holder that stops renewing (simulated: acquire once, then never
    call run_with_lease again for it) loses the lease on expiry; a second
    consumer polling for the same name acquires and starts within ~ttl_s."""

    async def _run() -> None:
        from app.coordination.factory import coordination

        ttl_s = 1  # tiny — real ~0.33s renew/poll cadence (ttl_s / 3).
        dead_holder_ttl = 1
        assert coordination().lease_acquire("mortal-lease", "dead-holder", ttl_s=dead_holder_ttl)

        survivor = _FakeConsumer("survivor")
        task = asyncio.create_task(
            run_with_lease("mortal-lease", "holder-b", ttl_s=ttl_s, start=survivor.start, stop=survivor.stop)
        )
        try:
            deadline = time.monotonic() + dead_holder_ttl + ttl_s  # generous margin
            while time.monotonic() < deadline and not survivor.active:
                await asyncio.sleep(0.02)
            assert survivor.active is True, "takeover did not happen within TTL + margin"
            assert survivor.events == ["start"]
        finally:
            await _cancel_and_await(task)

    asyncio.run(_run())


def test_cancellation_stops_consumer_and_releases_lease():
    """Cancelling the run_with_lease task, while it holds the lease, calls
    stop() and releases the lease so a fresh acquirer isn't blocked."""

    async def _run() -> None:
        from app.coordination.factory import coordination

        consumer = _FakeConsumer("c")
        task = asyncio.create_task(
            run_with_lease("cancel-lease", "holder-c", ttl_s=5, start=consumer.start, stop=consumer.stop)
        )
        await _wait_until(lambda: consumer.events == ["start"])

        await _cancel_and_await(task)

        assert consumer.events == ["start", "stop"]
        assert consumer.active is False
        # Released — a brand new holder can acquire immediately, no TTL wait.
        assert coordination().lease_acquire("cancel-lease", "someone-else", ttl_s=5) is True

    asyncio.run(_run())


def test_renew_lost_stops_consumer_and_reacquires():
    """A lease_renew() -> False (lost to another holder, or expired and
    reclaimed) makes run_with_lease call stop() and go back to polling —
    it must not keep believing it holds the lease."""

    async def _run() -> None:
        import app.coordination.leases as leases_mod

        backend = _FlakyBackend()
        consumer = _FakeConsumer("d")

        orig_coordination = leases_mod.coordination
        leases_mod.coordination = lambda: backend  # type: ignore[assignment]
        try:
            task = asyncio.create_task(
                run_with_lease("flaky-lease", "holder-d", ttl_s=1, start=consumer.start, stop=consumer.stop)
            )
            await _wait_until(lambda: consumer.events == ["start"])

            # Simulate another holder stealing the lease out from under holder-d.
            backend._leases["flaky-lease"] = ("someone-else", time.monotonic() + 10)

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and "stop" not in consumer.events:
                await asyncio.sleep(0.05)
            assert consumer.events[:2] == ["start", "stop"]
            await _cancel_and_await(task)
        finally:
            leases_mod.coordination = orig_coordination

    asyncio.run(_run())


def test_transient_unavailability_within_ttl_is_tolerated():
    """A single CoordinationUnavailable blip on renew, shorter than ttl_s,
    must NOT stop the consumer — only sustained unavailability should."""

    async def _run() -> None:
        import app.coordination.leases as leases_mod

        backend = _FlakyBackend()
        consumer = _FakeConsumer("e")

        orig_coordination = leases_mod.coordination
        leases_mod.coordination = lambda: backend  # type: ignore[assignment]
        try:
            task = asyncio.create_task(
                run_with_lease("blip-lease", "holder-e", ttl_s=3, start=consumer.start, stop=consumer.stop)
            )
            await _wait_until(lambda: consumer.events == ["start"])

            backend.fail_renew = True
            await asyncio.sleep(0.3)  # well under ttl_s=3 -> one blip only
            backend.fail_renew = False
            await asyncio.sleep(0.3)

            assert consumer.events == ["start"], "consumer stopped on a transient blip"
            assert consumer.active is True
            await _cancel_and_await(task)
        finally:
            leases_mod.coordination = orig_coordination

    asyncio.run(_run())


def test_unavailability_persisting_beyond_ttl_stops_consumer():
    """CoordinationUnavailable on every renew for longer than ttl_s must
    stop the consumer and resume polling to re-acquire."""

    async def _run() -> None:
        import app.coordination.leases as leases_mod

        backend = _FlakyBackend()
        consumer = _FakeConsumer("f")

        orig_coordination = leases_mod.coordination
        leases_mod.coordination = lambda: backend  # type: ignore[assignment]
        try:
            ttl_s = 1
            task = asyncio.create_task(
                run_with_lease("outage-lease", "holder-f", ttl_s=ttl_s, start=consumer.start, stop=consumer.stop)
            )
            await _wait_until(lambda: consumer.events == ["start"])

            backend.fail_renew = True
            deadline = time.monotonic() + ttl_s + 2
            while time.monotonic() < deadline and "stop" not in consumer.events:
                await asyncio.sleep(0.05)
            assert "stop" in consumer.events, "consumer never stopped despite sustained outage"
            await _cancel_and_await(task)
        finally:
            leases_mod.coordination = orig_coordination

    asyncio.run(_run())


def test_start_raises_releases_lease_and_reacquires_after_backoff():
    """A start() failure must not strand the lease with a dead holder: it
    should be released promptly (well before the ttl_s backoff elapses —
    a fresh holder can grab it right away) and the original holder retries
    after backing off for ttl_s, succeeding on the next attempt."""

    async def _run() -> None:
        from app.coordination.factory import coordination

        consumer = _FailingStartConsumer(fail_times=1)
        ttl_s = 1
        task = asyncio.create_task(
            run_with_lease("start-fail-lease", "holder-g", ttl_s=ttl_s, start=consumer.start, stop=consumer.stop)
        )
        try:
            # First let holder-g actually acquire and hit the induced
            # start() failure (its own lease_acquire hops off-loop via
            # asyncio.to_thread, so this must be awaited, not assumed).
            await _wait_until(lambda: "start-fail" in consumer.events)

            # Released promptly after the failure — well under the ttl_s
            # backoff — so a fresh holder can acquire almost immediately.
            deadline = time.monotonic() + 0.5
            acquired_elsewhere = False
            while time.monotonic() < deadline:
                if coordination().lease_acquire("start-fail-lease", "someone-else", ttl_s=ttl_s):
                    acquired_elsewhere = True
                    break
                await asyncio.sleep(0.01)
            assert acquired_elsewhere, "lease was not released promptly after start() failure"
            coordination().lease_release("start-fail-lease", "someone-else")

            # After backing off for ttl_s, the original holder retries
            # acquisition and start() succeeds this time.
            deadline = time.monotonic() + ttl_s + 2
            while time.monotonic() < deadline and "start-ok" not in consumer.events:
                await asyncio.sleep(0.02)
            assert consumer.events == ["start-fail", "start-ok"]
            assert consumer.active is True
        finally:
            await _cancel_and_await(task)

    asyncio.run(_run())


def test_second_consumer_acquires_in_the_gap_after_start_failure():
    """While the original holder backs off after a start() failure, a
    second replica polling the same lease name must be able to acquire it
    and start successfully — a failing start() on one replica must not
    starve a healthier replica out of the lease."""

    async def _run() -> None:
        failing = _FailingStartConsumer(fail_times=99)  # keeps failing
        healthy = _FakeConsumer("healthy")
        ttl_s = 1
        task_a = asyncio.create_task(
            run_with_lease("gap-lease", "holder-a", ttl_s=ttl_s, start=failing.start, stop=failing.stop)
        )
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and "start-fail" not in failing.events:
                await asyncio.sleep(0.01)
            assert "start-fail" in failing.events

            task_b = asyncio.create_task(
                run_with_lease("gap-lease", "holder-b", ttl_s=ttl_s, start=healthy.start, stop=healthy.stop)
            )
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not healthy.active:
                    await asyncio.sleep(0.01)
                assert healthy.active is True, "second consumer never acquired the lease in the gap"
            finally:
                await _cancel_and_await(task_b)
        finally:
            await _cancel_and_await(task_a)

    asyncio.run(_run())
