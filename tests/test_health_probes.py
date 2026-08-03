import asyncio
import contextlib
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health_probes import ReadinessState, readiness, register_readiness_check, router


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
