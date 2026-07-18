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


def test_unregistered_kind_is_terminally_failed_without_retry(worker_db, monkeypatch):
    """Registry-drift guard (carry-over finding): a claimed job whose kind
    isn't (or is no longer) registered on this process must be failed
    terminally with no retry, not re-claimed forever.

    Under the current registry design a job can only be claimed for a
    kind that's registered (kinds passed to ``claim_next`` come straight
    from ``JOB_KINDS``), so the natural trigger is a race: the kind is
    deregistered on this process *between* the claim and the
    ``JOB_KINDS.get(job["kind"])`` lookup (e.g. a concurrent registry
    update). Simulated here via a repo proxy that pops the kind from
    ``JOB_KINDS`` right after ``claim_next`` returns it.
    """
    import app.worker.runtime as runtime_mod
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def handler(payload: dict) -> None:
        raise AssertionError("handler must never run — the guard fires before dispatch")

    register_kind(JobKind(name="ephemeral_kind", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    real_repo = jobs_repo()
    job = real_repo.enqueue("ephemeral_kind", {}, max_attempts=5)

    class DriftingRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            claimed = real_repo.claim_next(**kwargs)
            if claimed is not None:
                # Simulate registry drift between claim and dispatch.
                JOB_KINDS.pop("ephemeral_kind", None)
            return claimed

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: DriftingRepo())

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    row = real_repo.get(job["id"])
    assert row["status"] == "failed"
    assert "no registered handler" in row["error"]
    assert row["run_after"] is None, "must be finalized, not requeued, despite attempts remaining"


def test_shutdown_drain_waits_for_in_flight_handler_and_finalizes(worker_db, monkeypatch):
    """Critical: cancelling ``worker_loop`` must not orphan an in-flight
    handler's OS thread against a DB the caller is about to close.
    ``asyncio.to_thread``'s cancellation delivers ``CancelledError``
    immediately while the underlying thread keeps running — the loop must
    perform a bounded drain (wait for the handler, then finalize normally)
    instead of abandoning it the instant cancellation is requested."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "5")

    started = threading.Event()

    def slow_handler(payload: dict) -> None:
        started.set()
        time.sleep(0.4)

    register_kind(JobKind(name="slow_test", handler=slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("slow_test", {})

    async def _drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(_drive())

    row = repo.get(job["id"])
    assert row["status"] == "done", "in-flight handler must be finalized (not abandoned) by the shutdown drain"
    assert row["finished_at"] is not None


def test_shutdown_drain_times_out_and_abandons_slow_handler(worker_db, monkeypatch):
    """If the in-flight handler doesn't finish within
    ``AGNES_WORKER_DRAIN_TIMEOUT_S``, the drain gives up (logs, doesn't
    wait forever) and leaves the job 'running' for lease-expiry recovery
    (reclaim or ``reap_exhausted``), rather than blocking shutdown
    indefinitely."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "0.2")

    started = threading.Event()
    release = threading.Event()

    def very_slow_handler(payload: dict) -> None:
        started.set()
        # Held open past the drain timeout; released once the test has
        # observed the timeout behavior so the (non-daemon) thread doesn't
        # block interpreter shutdown.
        release.wait(timeout=5)

    register_kind(JobKind(name="very_slow_test", handler=very_slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("very_slow_test", {})

    async def _drive() -> float:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        start = time.monotonic()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        elapsed = time.monotonic() - start
        # Release the abandoned handler thread while the loop is still
        # running so its (harmless) completion callback has somewhere to
        # land, then give the loop one more tick to process it.
        release.set()
        await asyncio.sleep(0.05)
        return elapsed

    elapsed = asyncio.run(_drive())

    assert elapsed < 1.5, f"drain should give up around the 0.2s configured timeout, took {elapsed:.2f}s"
    row = repo.get(job["id"])
    assert row["status"] == "running", "abandoned in-flight job must NOT be finalized by a timed-out drain"


def test_shutdown_drain_timeout_cancels_abandoned_heartbeat(worker_db, monkeypatch):
    """An abandoned (drain-timed-out) job's heartbeat task must be
    cancelled (not left running) before worker_loop returns — otherwise
    it keeps extending the lease of a job the loop no longer owns, after
    the worker process has already told the caller shutdown is done."""
    from app.worker import runtime
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from src.repositories import jobs_repo

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "0.2")

    started = threading.Event()
    release = threading.Event()

    def very_slow_handler(payload: dict) -> None:
        started.set()
        # Held open past the drain timeout; released once the test has
        # observed the timeout behavior so the (non-daemon) thread doesn't
        # block interpreter shutdown.
        release.wait(timeout=5)

    register_kind(JobKind(name="very_slow_hb_test", handler=very_slow_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("very_slow_hb_test", {})

    captured: dict[str, object] = {}
    real_drain = runtime._drain_in_flight

    async def _capturing_drain(in_flight, worker_id):
        # Snapshot the in-flight entries (there's exactly one) before the
        # real drain runs, then let it do its normal timeout/cancel work.
        captured["entries"] = dict(in_flight)
        await real_drain(in_flight, worker_id)

    monkeypatch.setattr(runtime, "_drain_in_flight", _capturing_drain)

    async def _drive() -> None:
        task = asyncio.create_task(runtime.worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(_drive())

    entries = captured.get("entries")
    assert entries, "drain should have seen the in-flight job"
    entry = entries[job["id"]]
    assert entry.hb_task.cancelled() or entry.hb_task.done(), (
        "abandoned entry's heartbeat task must be cancelled/done once worker_loop returns, "
        "or it keeps extending a lease the loop no longer owns"
    )

    row = repo.get(job["id"])
    assert row["status"] == "running", "abandoned in-flight job must NOT be finalized by a timed-out drain"


def test_shutdown_drain_finalization_error_does_not_escape_worker_loop(worker_db, monkeypatch):
    """A DB exception raised by complete()/fail() while finalizing an
    in-flight job during the shutdown drain must be logged and swallowed,
    not propagated — otherwise it would abort the rest of the caller's
    lifespan shutdown (including closing the system DB) with the drain
    only partway through finalizing the other in-flight jobs."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo
    from src.repositories.jobs import JobsRepository

    monkeypatch.setenv("AGNES_WORKER_DRAIN_TIMEOUT_S", "5")

    started = threading.Event()
    finish = threading.Event()

    def quick_handler(payload: dict) -> None:
        started.set()
        finish.wait(timeout=5)

    register_kind(JobKind(name="finalize_boom_test", handler=quick_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("finalize_boom_test", {})

    def boom_complete(self, job_id, worker_id, lease_token) -> None:
        raise RuntimeError("simulated DB failure during drain finalization")

    monkeypatch.setattr(JobsRepository, "complete", boom_complete)

    async def _drive() -> None:
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"
        task.cancel()
        # Let the handler finish while the drain is waiting on it, so it
        # lands in `_done` and the drain takes the complete() path.
        finish.set()
        # If the fix regresses, the complete() RuntimeError raised inside
        # the drain's `finally` block replaces the CancelledError as what
        # propagates out of worker_loop, so this suppress would NOT catch
        # it and asyncio.run(_drive()) below would fail with the
        # RuntimeError instead of completing cleanly.
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()
        assert task.cancelled(), "worker_loop must finish via its own cancellation, not an escaped finalization error"

    asyncio.run(_drive())

    row = repo.get(job["id"])
    assert row["status"] == "running", "job whose complete() raised must not end up falsely marked done"


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


# ---------------------------------------------------------------------------
# observability (three-plane wave 2D, task 2): job-queue + worker metrics
# ---------------------------------------------------------------------------


def _metric_value(name, **labels):
    from app.observability.metrics import REGISTRY

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


def test_worker_loop_records_claims_total(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    register_kind(JobKind(name="metrics_claim_e2e", handler=lambda payload: None, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    repo.enqueue("metrics_claim_e2e", {})

    before = _metric_value("agnes_job_claims_total", kind="metrics_claim_e2e") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    after = _metric_value("agnes_job_claims_total", kind="metrics_claim_e2e")
    assert after == before + 1.0


def test_worker_loop_records_duration_and_done_outcome(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    register_kind(JobKind(name="metrics_duration_ok", handler=lambda payload: None, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("metrics_duration_ok", {})

    before_count = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_ok", outcome="done") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    assert repo.get(job["id"])["status"] == "done"
    after_count = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_ok", outcome="done")
    assert after_count == before_count + 1.0


def test_worker_loop_records_duration_and_failed_outcome(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def boom_handler(payload: dict) -> None:
        raise RuntimeError("metrics boom")

    register_kind(JobKind(name="metrics_duration_fail", handler=boom_handler, lane=LIGHT_LANE, lease_seconds=30))
    repo = jobs_repo()
    repo.enqueue("metrics_duration_fail", {}, max_attempts=1)

    before_duration = (
        _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_fail", outcome="failed") or 0.0
    )
    before_failures = (
        _metric_value("agnes_job_failures_total", kind="metrics_duration_fail", reason="RuntimeError") or 0.0
    )

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.4))

    after_duration = _metric_value("agnes_job_duration_seconds_count", kind="metrics_duration_fail", outcome="failed")
    after_failures = _metric_value("agnes_job_failures_total", kind="metrics_duration_fail", reason="RuntimeError")
    assert after_duration == before_duration + 1.0
    assert after_failures == before_failures + 1.0


def test_worker_loop_lane_active_reflects_busy_heavy_lane(worker_db):
    """agnes_worker_lane_active{lane="heavy"} must go up while a heavy job's
    handler is actually running, and come back down once it finishes."""
    from app.worker.registry import HEAVY_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    started = threading.Event()
    release = threading.Event()

    def slow_heavy_handler(payload: dict) -> None:
        started.set()
        release.wait(timeout=5)

    register_kind(JobKind(name="metrics_lane_active", handler=slow_heavy_handler, lane=HEAVY_LANE, lease_seconds=30))
    repo = jobs_repo()
    job = repo.enqueue("metrics_lane_active", {})

    baseline = _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) or 0.0
    baseline_running = _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) or 0.0

    async def _drive():
        task = asyncio.create_task(worker_loop(worker_id="test-worker", poll_interval_s=0.05))
        deadline = time.monotonic() + 2.0
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "handler never started"

        # While the handler is blocked mid-flight, the lane must show busy.
        assert _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) == baseline + 1.0
        assert (
            _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) == baseline_running + 1.0
        )

        release.set()
        # Give the handler + finalization a moment to complete, then cancel.
        deadline = time.monotonic() + 2.0
        while repo.get(job["id"])["status"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert repo.get(job["id"])["status"] == "done"
    assert _metric_value("agnes_worker_lane_active", lane=HEAVY_LANE) == baseline
    assert _metric_value("agnes_jobs_running", kind="metrics_lane_active", lane=HEAVY_LANE) == baseline_running


def test_worker_loop_no_handler_records_failure_metric(worker_db, monkeypatch):
    """The registry-drift ("no registered handler") branch finalizes the
    job without ever running a handler — it must still bump
    agnes_job_failures_total instead of being invisible to observability.

    Mirrors ``test_unregistered_kind_is_terminally_failed_without_retry``'s
    registry-drift simulation: a job can only be claimed for a kind
    currently in ``JOB_KINDS`` (``_kinds_for_lane`` filters on it), so an
    entirely-unregistered kind is never claimed at all. The drift has to
    happen *between* claim and dispatch, via a repo proxy that pops the
    kind right after ``claim_next`` returns it.
    """
    import app.worker.runtime as runtime_mod
    from app.worker.registry import JOB_KINDS, LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def handler(payload: dict) -> None:
        raise AssertionError("handler must never run — the guard fires before dispatch")

    register_kind(JobKind(name="metrics_drift_kind", handler=handler, lane=LIGHT_LANE, lease_seconds=30))
    real_repo = jobs_repo()
    real_repo.enqueue("metrics_drift_kind", {}, max_attempts=5)

    class DriftingRepo:
        def __getattr__(self, name):
            return getattr(real_repo, name)

        def claim_next(self, **kwargs):
            claimed = real_repo.claim_next(**kwargs)
            if claimed is not None:
                JOB_KINDS.pop("metrics_drift_kind", None)
            return claimed

    monkeypatch.setattr(runtime_mod, "_jobs_repo", lambda: DriftingRepo())

    before = _metric_value("agnes_job_failures_total", kind="other", reason="no-registered-handler") or 0.0

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.3))

    after = _metric_value("agnes_job_failures_total", kind="other", reason="no-registered-handler")
    assert after == before + 1.0
