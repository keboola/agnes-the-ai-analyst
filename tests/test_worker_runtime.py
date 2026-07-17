"""Tests for the wave-2B worker runtime (``app/worker/registry.py`` +
``app/worker/runtime.py``).

Uses fake registered kinds against a fresh temp-DATA_DIR DuckDB backend —
no real job kinds are registered yet (that's a later task in the same
wave). Runs the real asyncio ``worker_loop`` for a short, bounded window
via ``asyncio.create_task`` + ``cancel()``, mirroring how ``app/main.py``
starts/stops the ``canary_loop`` background task.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time

import pytest


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, closed after the test.

    ``jobs_repo()`` (the repo factory the runtime module calls) resolves
    to the DuckDB backend here since neither DATABASE_URL nor
    AGNES_DB_URL is set — mirrors the ``system_db`` fixture pattern in
    ``tests/test_state_checkpoint.py``.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. the jobs table)
    yield
    close_system_db()


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


async def _run_and_cancel(coro, duration_s: float) -> None:
    """Start ``coro`` as a task, let it run for ``duration_s``, then
    cancel it and wait for a clean shutdown (mirrors the
    ``_canary_task.cancel()`` / ``await _canary_task`` pattern in
    ``app/main.py``'s lifespan)."""
    task = asyncio.create_task(coro)
    await asyncio.sleep(duration_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_register_kind_stores_by_name():
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind

    register_kind(JobKind(name="sanity", handler=lambda payload: None, lane=LIGHT_LANE))
    assert "sanity" in JOB_KINDS
    assert JOB_KINDS["sanity"].lane == LIGHT_LANE
    assert JOB_KINDS["sanity"].lease_seconds == 120  # dataclass default
    assert JOB_KINDS["sanity"].retry_in_seconds == 300  # dataclass default


def test_register_kind_rejects_unknown_lane():
    from app.worker.registry import JobKind, register_kind

    with pytest.raises(ValueError):
        register_kind(JobKind(name="bad", handler=lambda payload: None, lane="medium"))


# ---------------------------------------------------------------------------
# worker_loop
# ---------------------------------------------------------------------------


def test_default_worker_id_is_hostname_colon_pid():
    from app.worker.runtime import default_worker_id

    assert default_worker_id() == f"{socket.gethostname()}:{os.getpid()}"


def test_heavy_lane_serializes_while_light_lane_proceeds_concurrently(worker_db):
    """Two queued heavy jobs must never run at the same time (concurrency
    1), but a queued light job must be able to run WHILE a heavy job is
    still in flight (separate lane, separate slot) — not merely after
    both heavy jobs finish."""
    from app.worker.registry import HEAVY_LANE, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    lock = threading.Lock()
    heavy_state = {"n": 0, "peak": 0}
    heavy_intervals: list[tuple[float, float]] = []
    light_started_at: list[float] = []

    def heavy_handler(payload: dict) -> None:
        start = time.monotonic()
        with lock:
            heavy_state["n"] += 1
            heavy_state["peak"] = max(heavy_state["peak"], heavy_state["n"])
        time.sleep(0.4)
        with lock:
            heavy_state["n"] -= 1
        heavy_intervals.append((start, time.monotonic()))

    def light_handler(payload: dict) -> None:
        light_started_at.append(time.monotonic())

    register_kind(JobKind(name="heavy_test", handler=heavy_handler, lane=HEAVY_LANE, lease_seconds=30))
    register_kind(JobKind(name="light_test", handler=light_handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    repo.enqueue("heavy_test", {})
    repo.enqueue("heavy_test", {})
    repo.enqueue("light_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 1.3))

    assert heavy_state["peak"] == 1, "heavy lane ran more than one job concurrently"
    assert len(heavy_intervals) == 2, f"expected both heavy jobs to complete, got {heavy_intervals}"
    assert light_started_at, "light lane never ran"
    light_t = light_started_at[0]
    heavy_all_done_at = max(end for _, end in heavy_intervals)
    assert light_t < heavy_all_done_at, (
        f"light job (t={light_t}) ran only after ALL heavy work finished (t={heavy_all_done_at}) "
        f"— light lane appears to be serialized behind the heavy lane instead of running concurrently "
        f"(heavy intervals: {heavy_intervals})"
    )

    done_heavy = repo.list(kind="heavy_test", status="done")
    assert len(done_heavy) == 2


def test_handler_exception_fails_job_with_retry(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise RuntimeError("boom")

    register_kind(
        JobKind(name="boom_test", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30, retry_in_seconds=60)
    )

    repo = jobs_repo()
    job = repo.enqueue("boom_test", {}, max_attempts=5)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    row = repo.get(job["id"])
    assert row["status"] == "queued", "a failed job with attempts remaining and retry_in_seconds must be requeued"
    assert row["error"] == "boom"
    assert row["run_after"] is not None
    assert row["attempts"] == 1
    assert row["leased_by"] is None


def test_handler_exception_at_max_attempts_finalizes_failed(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise ValueError("nope")

    register_kind(JobKind(name="boom_once", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    job = repo.enqueue("boom_once", {}, max_attempts=1)

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert row["error"] == "nope"
    assert row["finished_at"] is not None


def test_graceful_cancel_mid_poll_returns_promptly(worker_db):
    """No kinds registered => every lane sits idle in its poll sleep.
    Cancelling must not wait out the (long) poll interval."""
    from app.worker.runtime import worker_loop

    async def _timed_drive() -> float:
        start = time.monotonic()
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=5.0))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        return time.monotonic() - start

    elapsed = asyncio.run(_timed_drive())
    assert elapsed < 1.0, f"cancel took {elapsed:.2f}s to take effect against a 5s poll interval"


def test_lane_slot_survives_transient_claim_next_failure(worker_db, monkeypatch):
    """A single blip in claim_next() (e.g. a DB hiccup) must not
    permanently kill the lane slot — it should log and retry after the
    next poll tick, same hardening convention as canary_loop /
    _state_checkpoint_loop in app/main.py."""
    import app.worker.runtime as runtime_mod
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    real_repo = jobs_repo()
    calls = {"n": 0}

    class FlakyRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient db hiccup")
            return real_repo.claim_next(**kwargs)

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: FlakyRepo())

    ran = {"done": False}

    def handler(payload: dict) -> None:
        ran["done"] = True

    register_kind(JobKind(name="flaky_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    job = real_repo.enqueue("flaky_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.5))

    assert calls["n"] >= 2, "expected at least one failed claim_next() attempt followed by a retry"
    assert ran["done"] is True, "lane slot never recovered from the transient failure"
    assert real_repo.get(job["id"])["status"] == "done"


def test_worker_loop_reaps_stuck_jobs(worker_db):
    """The reap sweep is wired into worker_loop itself, independent of
    whether any handler is registered for the stuck job's kind."""
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    repo = jobs_repo()
    job = repo.enqueue("nobody_handles_this", {}, max_attempts=1)
    claimed = repo.claim_next(kinds=["nobody_handles_this"], worker_id="dead-worker", lease_seconds=-5)
    assert claimed is not None
    assert repo.get(job["id"])["status"] == "running"

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert row["error"] == "lease expired after max attempts"
