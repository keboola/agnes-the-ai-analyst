"""Worker runtime loop: claims jobs off the ``jobs`` queue and runs their
registered handlers (spec §3.3 / plan wave-2B Task 3).

Two independent lanes share one asyncio loop:

- **heavy** — concurrency 1 (one slot/task)
- **light** — concurrency 2 (two slots/tasks)

Each lane slot repeats: ``claim_next(kinds=<lane's registered kinds>)`` ->
if nothing eligible, sleep ``poll_interval_s`` and retry -> otherwise run
the kind's handler via ``asyncio.to_thread`` while a heartbeat task
extends the lease every ``lease_seconds/3`` -> ``complete()``/``fail()``.

A third, independent task sweeps ``reap_exhausted()`` once per
``poll_interval_s`` tick — this is the stuck-job reaper: a 'running' job
whose lease expired on its LAST attempt is not eligible for
``claim_next()``'s crash-recovery reclaim (which requires
``attempts < max_attempts``), so without an active sweep it would stay
'running' forever. Kept as one task independent of lane activity so it
converges every lane's stuck jobs from a single cadence rather than
racing N lane slots into duplicate sweeps.

Heartbeat-lost handling: if ``heartbeat()`` ever returns ``False`` (the
job's lease was reclaimed by another worker — see
``JobsRepository.heartbeat``'s docstring), the heartbeat task logs and
stops extending. The in-flight handler thread cannot be cancelled
cooperatively (it's a real OS thread, not a coroutine) and is left to run
to completion; its eventual ``complete()``/``fail()`` call is a
raise-free no-op against the now-reclaimed row (guarded by
``leased_by = <this worker> AND status = 'running'`` — see those methods'
docstrings), so no state gets clobbered.

Graceful shutdown: cancelling the task returned by ``worker_loop(...)``
(mirrors the ``canary_loop`` task-create/cancel pattern in
``app/main.py``) delivers ``CancelledError`` at the next `await` — either
the idle poll sleep (immediate exit) or, if a handler is mid-flight, at
the point ``asyncio.to_thread`` resolves the underlying OS thread's
result (a running executor future refuses ``.cancel()``, so the *thread*
itself always runs to completion regardless of the cancel request; only
delivery of the outcome to this coroutine is what gets pre-empted). A
hard kill (SIGKILL) mid-handler leaves the job 'running' with a lease
that later expires and is recovered via ``claim_next()``'s reclaim path,
or — if attempts are already exhausted by then — this module's own
``reap_exhausted()`` sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket

from app.worker.registry import HEAVY_LANE, JOB_KINDS, LIGHT_LANE, JobKind

logger = logging.getLogger(__name__)

_HEAVY_CONCURRENCY = 1
_LIGHT_CONCURRENCY = 2

#: Floor on the heartbeat cadence so a misconfigured/very short
#: ``lease_seconds`` (e.g. in a test) can't spin the heartbeat loop.
_MIN_HEARTBEAT_INTERVAL_S = 0.5


def default_worker_id() -> str:
    """``<hostname>:<pid>`` — stable per-process identity for ``leased_by``."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _jobs_repo():
    # Imported lazily (module-function, not module-level import) so tests
    # can monkeypatch ``src.repositories.jobs_repo`` freely and so this
    # module carries no import-time dependency on which backend is active.
    from src.repositories import jobs_repo

    return jobs_repo()


def _kinds_for_lane(lane: str) -> list[str]:
    return [name for name, kind in JOB_KINDS.items() if kind.lane == lane]


def _sweep_stale_scratch() -> None:
    """Best-effort orphaned-scratch sweep, run before each HEAVY job.

    Heavy jobs (``data-refresh``, ``jira-refresh`` — registered in a later
    task) are exactly the Keboola-export workload that leaves
    ``kbc-export-*`` / ``kbc-slice-*`` staging dirs behind when a process
    is hard-killed mid-export (SIGKILL/OOM/container recreate) — see
    ``connectors/keboola/storage_api.py:sweep_orphaned_scratch``'s
    docstring for the full failure mode this prevents (unswept scratch
    fills the data disk until every sync fails with ENOSPC). Reused as-is,
    not reimplemented: same age-gate (``AGNES_SCRATCH_MAX_AGE_SEC``,
    default 1h) and prefix set.
    """
    try:
        from connectors.keboola.storage_api import sweep_orphaned_scratch

        sweep_orphaned_scratch()
    except Exception:
        logger.exception("worker: stale-scratch sweep failed (non-fatal)")


async def _heartbeat_loop(job_id: str, worker_id: str, lease_seconds: int) -> None:
    """Extend the lease every ``lease_seconds/3`` while a handler runs.

    Stops silently (no exception) the first time ``heartbeat()`` returns
    ``False`` — see the module docstring for why the in-flight handler
    thread is left running regardless.
    """
    interval = max(lease_seconds / 3, _MIN_HEARTBEAT_INTERVAL_S)
    while True:
        await asyncio.sleep(interval)
        ok = await asyncio.to_thread(_jobs_repo().heartbeat, job_id, worker_id, lease_seconds)
        if not ok:
            logger.warning(
                "worker %s: heartbeat lost for job %s (lease reclaimed by another worker); abandoning heartbeat",
                worker_id,
                job_id,
            )
            return


async def _run_one(job: dict, kind: JobKind, worker_id: str) -> None:
    """Run one claimed job's handler with a concurrent heartbeat, then
    complete()/fail() it."""
    hb_task = asyncio.create_task(
        _heartbeat_loop(job["id"], worker_id, kind.lease_seconds),
        name=f"worker-heartbeat-{job['id']}",
    )
    try:
        await asyncio.to_thread(kind.handler, job["payload_json"])
    except Exception as exc:
        logger.exception("worker %s: job %s (kind=%s) failed", worker_id, job["id"], job["kind"])
        await asyncio.to_thread(
            _jobs_repo().fail,
            job["id"],
            worker_id,
            str(exc),
            retry_in_seconds=kind.retry_in_seconds,
        )
    else:
        await asyncio.to_thread(_jobs_repo().complete, job["id"], worker_id)
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task


async def _lane_slot(lane: str, worker_id: str, poll_interval_s: float) -> None:
    """One concurrency slot for ``lane``: claim -> run -> repeat, sleeping
    ``poll_interval_s`` whenever there's nothing to do (no registered
    kinds for the lane, or nothing eligible to claim).

    The whole iteration body runs under a broad ``except Exception`` (NOT
    ``except BaseException`` — ``asyncio.CancelledError`` must propagate
    for shutdown to work) so a transient failure anywhere in the claim/run
    path (e.g. a DB hiccup on ``claim_next()``) logs and retries after
    ``poll_interval_s`` instead of permanently killing this slot — and,
    via ``asyncio.gather``'s cancel-all-on-first-exception semantics in
    ``worker_loop``, every OTHER slot and the reaper too. Mirrors the
    hardening already used by ``canary_loop``/``_state_checkpoint_loop``
    in ``app/main.py``.
    """
    while True:
        try:
            kinds = _kinds_for_lane(lane)
            if not kinds:
                await asyncio.sleep(poll_interval_s)
                continue

            # claim_next() needs a lease duration before it knows which job
            # (and therefore which kind) it will return; using the longest
            # lease configured across the lane's kinds guarantees the initial
            # lease never expires before the first heartbeat tick corrects it
            # to the claimed job's actual kind.lease_seconds (heartbeat reads
            # the kind fresh after claiming, below).
            max_lease = max((JOB_KINDS[name].lease_seconds for name in kinds), default=120)
            job = await asyncio.to_thread(
                _jobs_repo().claim_next,
                kinds=kinds,
                worker_id=worker_id,
                lease_seconds=max_lease,
            )
            if job is None:
                await asyncio.sleep(poll_interval_s)
                continue

            kind = JOB_KINDS.get(job["kind"])
            if kind is None:
                # Registry drift: this job's kind isn't (or is no longer)
                # registered on this process. Fail it outright rather than
                # spin forever re-claiming a job nobody here can execute.
                logger.error(
                    "worker %s: no registered handler for job kind %r (job %s); failing",
                    worker_id,
                    job["kind"],
                    job["id"],
                )
                await asyncio.to_thread(
                    _jobs_repo().fail,
                    job["id"],
                    worker_id,
                    f"no registered handler for kind {job['kind']!r}",
                    retry_in_seconds=None,
                )
                continue

            if lane == HEAVY_LANE:
                await asyncio.to_thread(_sweep_stale_scratch)

            await _run_one(job, kind, worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker %s: lane %s poll iteration failed (non-fatal); retrying", worker_id, lane)
            await asyncio.sleep(poll_interval_s)


async def _reap_loop(poll_interval_s: float) -> None:
    """Sweep ``reap_exhausted()`` once per ``poll_interval_s`` tick,
    independent of lane activity (see module docstring)."""
    while True:
        try:
            reaped = await asyncio.to_thread(_jobs_repo().reap_exhausted)
            if reaped:
                logger.info("worker: reaped %d stuck job(s) (lease expired at max attempts)", reaped)
        except Exception:
            logger.exception("worker: reap_exhausted sweep failed (non-fatal)")
        await asyncio.sleep(poll_interval_s)


async def worker_loop(*, worker_id: str, poll_interval_s: float = 5.0) -> None:
    """Run the worker runtime until cancelled.

    Starts the reaper task, ``_HEAVY_CONCURRENCY`` heavy-lane slots and
    ``_LIGHT_CONCURRENCY`` light-lane slots, and waits on all of them.
    Cancelling the enclosing task (the ``canary_loop`` task-create/cancel
    pattern in ``app/main.py``'s lifespan) cancels every child task too —
    ``asyncio.gather`` propagates cancellation of its own awaiter to every
    task it's gathering.
    """
    tasks = [asyncio.create_task(_reap_loop(poll_interval_s), name="worker-reaper")]
    tasks += [
        asyncio.create_task(_lane_slot(HEAVY_LANE, worker_id, poll_interval_s), name=f"worker-heavy-{i}")
        for i in range(_HEAVY_CONCURRENCY)
    ]
    tasks += [
        asyncio.create_task(_lane_slot(LIGHT_LANE, worker_id, poll_interval_s), name=f"worker-light-{i}")
        for i in range(_LIGHT_CONCURRENCY)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Defensive: make sure every child is actually cancelled/awaited
        # even if gather() returned early for a reason other than our own
        # cancellation (e.g. one task raised and gather fails fast while
        # siblings are still running).
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
