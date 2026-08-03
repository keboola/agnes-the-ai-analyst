import asyncio
import contextlib
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health_probes
from app.api.health_probes import ReadinessState, readiness, register_readiness_check, router


@pytest.fixture(autouse=True)
def _reset_drain_deadline():
    """The shared shutdown-drain budget is process-global by design.

    It is set once per process (shutdown happens once) and never reset, so
    without this a test that exhausts the budget would leave every later
    drain test with 0s of it.
    """
    health_probes._drain_deadline = None
    yield
    health_probes._drain_deadline = None


def make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_healthz_always_alive():
    assert make_client().get("/healthz").json() == {"status": "alive"}


def test_hysteresis_three_fails_two_recoveries():
    st = ReadinessState()
    assert st.is_ready()
    st.record_canary(False)
    st.record_canary(False)
    assert st.is_ready(), "two failures must not flip (hysteresis)"
    st.record_canary(False)
    assert not st.is_ready(), "third consecutive failure flips to not-ready"
    st.record_canary(True)
    assert not st.is_ready(), "one success must not recover"
    st.record_canary(True)
    assert st.is_ready(), "two consecutive successes recover"


def test_readyz_reflects_singleton(monkeypatch):
    client = make_client()
    for _ in range(3):
        readiness.record_canary(False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    for _ in range(2):
        readiness.record_canary(True)
    assert client.get("/readyz").status_code == 200


def test_extra_check_gates_readyz():
    client = make_client()
    flag = {"ok": True}
    register_readiness_check("t_extra", lambda: flag["ok"])
    try:
        assert client.get("/readyz").status_code == 200
        flag["ok"] = False
        r = client.get("/readyz")
        assert r.status_code == 503
        assert "t_extra" in str(r.json()["failed_checks"])
    finally:
        from app.api import health_probes

        health_probes._extra_checks.pop("t_extra", None)


# ---------------------------------------------------------------------------
# canary_loop cancellation drain
# ---------------------------------------------------------------------------


def test_cancelled_canary_waits_for_inflight_write(monkeypatch):
    """Cancelling canary_loop must drain an in-flight _write_canary thread.

    app/main.py's lifespan runs ``_canary_task.cancel(); await _canary_task``
    and then immediately closes the DuckDB singletons. ``asyncio.to_thread``
    cancellation abandons the running OS thread, so returning while the write
    is still executing lets ``close_system_db()`` race the write — DuckDB
    wedges and event-loop teardown joins the executor thread forever
    (observed as a 60s pytest-timeout inside ``TestClient.__exit__``).
    """
    from app.api import health_probes

    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()

    def slow_write() -> bool:
        write_started.set()
        release_write.wait(timeout=10)
        write_finished.set()
        return True

    monkeypatch.setattr(health_probes, "_write_canary", slow_write)

    async def drive() -> None:
        task = asyncio.create_task(health_probes.canary_loop())
        assert await asyncio.to_thread(write_started.wait, 5), "canary write never started"
        task.cancel()
        asyncio.get_running_loop().call_later(0.2, release_write.set)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        finished_before_return = write_finished.is_set()
        release_write.set()  # never leave the thread blocked, whatever the outcome
        assert finished_before_return, (
            "cancelled canary_loop returned while its write thread was still running "
            "(lifespan would proceed to close the DB under the in-flight write)"
        )

    asyncio.run(drive())


def test_cancelled_canary_exits_promptly_while_idle(monkeypatch):
    """Cancellation during the interval sleep must exit immediately — the
    drain only engages when a write is actually in flight."""
    from app.api import health_probes

    wrote = threading.Event()

    def quick_write() -> bool:
        wrote.set()
        return True

    monkeypatch.setattr(health_probes, "_write_canary", quick_write)

    async def drive() -> None:
        task = asyncio.create_task(health_probes.canary_loop(interval_s=60.0))
        assert await asyncio.to_thread(wrote.wait, 5), "canary write never ran"
        await asyncio.sleep(0.05)  # let the loop re-enter the interval sleep
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert task.done()

    asyncio.run(drive())


def test_cancelled_canary_drain_is_bounded(monkeypatch):
    """A write that never returns must not hang shutdown forever.

    The drain trades the abandoned-thread bug for a bounded wait, not for an
    unbounded one: "single statement" is an assumption, and lock contention
    or a partitioned Postgres can violate it. Past AGNES_DRAIN_TIMEOUT_S we
    log and abandon — i.e. fall back to the pre-drain behavior.
    """
    from app.api import health_probes

    monkeypatch.setenv("AGNES_DRAIN_TIMEOUT_S", "0.2")

    write_started = threading.Event()
    release_write = threading.Event()

    def wedged_write() -> bool:
        write_started.set()
        release_write.wait(timeout=30)
        return True

    monkeypatch.setattr(health_probes, "_write_canary", wedged_write)

    async def drive() -> None:
        task = asyncio.create_task(health_probes.canary_loop())
        assert await asyncio.to_thread(write_started.wait, 5), "canary write never started"
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.ensure_future(_await_cancelled(task))),
                timeout=10,
            )
        finally:
            release_write.set()  # never leave the thread blocked

    async def _await_cancelled(task) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())


def test_drain_budget_is_shared_across_the_whole_shutdown(monkeypatch):
    """Two drains must share one budget, not get a full timeout each.

    app/main.py's lifespan cancels the checkpoint loop, then the canary
    loop, then the worker loop sequentially, and the worker drains a
    heartbeat per in-flight entry. Per-call budgets would stack and overrun
    the container's stop_grace_period — reintroducing the SIGKILL the bound
    exists to avoid.
    """
    monkeypatch.setenv("AGNES_DRAIN_TIMEOUT_S", "10")

    async def drive() -> None:
        health_probes.begin_shutdown()
        first = health_probes._drain_budget_s()
        assert first == pytest.approx(10.0, abs=0.5)
        await asyncio.sleep(0.3)
        second = health_probes._drain_budget_s()
        assert second < first, "second drain must inherit the remaining budget, not a fresh one"
        assert second == pytest.approx(9.7, abs=0.5)

    asyncio.run(drive())


def test_drain_budget_floors_at_zero_once_spent(monkeypatch):
    monkeypatch.setenv("AGNES_DRAIN_TIMEOUT_S", "0")
    health_probes.begin_shutdown()
    assert health_probes._drain_budget_s() == 0.0
    assert health_probes._drain_budget_s() == 0.0


def test_routine_cancellation_does_not_arm_the_shutdown_budget(monkeypatch):
    """A non-shutdown drain must not start the shared clock.

    The worker cancels a job's heartbeat task on every completed job — a
    routine, non-shutdown cancellation that can land mid-DB-call. If that
    armed the process-global deadline, one budget later every real shutdown
    drain would get zero time and the protection would be silently gone
    while the process ran fine.
    """
    monkeypatch.setenv("AGNES_DRAIN_TIMEOUT_S", "10")
    assert health_probes._drain_deadline is None

    write_started = threading.Event()
    release_write = threading.Event()

    def slow_write() -> bool:
        write_started.set()
        release_write.wait(timeout=10)
        return True

    async def drive() -> None:
        task = asyncio.create_task(health_probes.to_thread_drain_on_cancel(slow_write))
        assert await asyncio.to_thread(write_started.wait, 5), "write never started"
        task.cancel()
        release_write.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert health_probes._drain_deadline is None, "a routine cancellation must not arm the shutdown budget"
    assert health_probes._drain_budget_s() == pytest.approx(10.0, abs=0.5), "full timeout outside shutdown"


def test_each_shutdown_gets_its_own_budget(monkeypatch):
    """Arming only once would starve every app after the first in a process.

    A test run starts and stops the app many times; if the deadline were
    armed once and never re-armed, the second shutdown onward would get zero
    drain and the original hang would come straight back.
    """
    monkeypatch.setenv("AGNES_DRAIN_TIMEOUT_S", "10")
    health_probes.begin_shutdown()
    first = health_probes._drain_deadline
    assert first is not None

    health_probes.end_shutdown()
    assert health_probes._drain_deadline is None, "budget must not leak into normal operation"
    assert health_probes._drain_budget_s() == pytest.approx(10.0, abs=0.5)

    health_probes.begin_shutdown()
    assert health_probes._drain_deadline is not None
    assert health_probes._drain_deadline >= first, "a later shutdown gets a fresh budget"
    assert health_probes._drain_budget_s() == pytest.approx(10.0, abs=0.5)
