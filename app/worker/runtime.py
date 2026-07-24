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

Same-worker double-execution guard (lease_token): all lane slots inside
one worker *process* share the same ``worker_id`` (hostname:pid — see
``default_worker_id``). After a stale slot's lease expires, ANOTHER slot
of the SAME process can reclaim the job under the *identical*
``worker_id``. ``JobsRepository``/``JobsPgRepository`` therefore guard
``heartbeat()``/``complete()``/``fail()`` on a fresh-per-claim
``lease_token`` (uuid4, minted by ``claim_next()``) rather than
``worker_id`` — a ``worker_id``-only guard cannot tell the two slots
apart, so the stale slot's late ``heartbeat()``/``complete()``/``fail()``
call would flip (or requeue) the live claim out from under the new slot.
This module threads the claimed row's ``lease_token`` through every
subsequent call for that job (see ``_run_one`` / ``_heartbeat_loop``).

Heartbeat-lost handling: if ``heartbeat()`` ever returns ``False`` (the
job's lease was reclaimed — by another worker, or by another slot of
this SAME worker — see ``JobsRepository.heartbeat``'s docstring), the
heartbeat task logs and stops extending. The in-flight handler thread
cannot be cancelled cooperatively (it's a real OS thread, not a
coroutine) and is left to run to completion; its eventual
``complete()``/``fail()`` call is a raise-free no-op against the
now-reclaimed row (guarded by ``lease_token = <this claim's token> AND
status = 'running'`` — see those methods' docstrings), so no state gets
clobbered.

Graceful shutdown (bounded drain): cancelling the task returned by
``worker_loop(...)`` (mirrors the ``canary_loop`` task-create/cancel
pattern in ``app/main.py``) delivers ``CancelledError`` at the next
`await`. For an idle lane slot that's the poll sleep — immediate exit.
For a slot with a handler mid-flight, ``asyncio.to_thread``'s
cancellation semantics are NOT "wait for the thread, then raise" —
cancelling the awaiting coroutine delivers ``CancelledError``
IMMEDIATELY, while the underlying OS thread keeps running in the
background regardless (a plain ``asyncio.Future`` — which is what
``run_in_executor``/``to_thread`` hands back — always honors ``.cancel()``
on the *awaiter* side even though the wrapped ``concurrent.futures``
future refuses cancellation once its thread has started). Left
unhandled, that orphans the handler thread exactly at the moment
``app/main.py``'s lifespan proceeds to close the DuckDB singletons the
thread may still be reading/writing — a WAL-corruption-class race.

To avoid that, ``_run_one`` runs the handler as a separate
``asyncio.shield``-ed future: our own await gets cancelled promptly (so
shutdown isn't blocked indefinitely), but the shielded future itself
keeps running untouched, and is registered into ``worker_loop``'s
``in_flight`` registry instead of being abandoned. ``worker_loop``, once
every lane/reaper task has been cancelled, performs ONE bounded drain
pass: wait on all registered in-flight futures together for up to
``AGNES_WORKER_DRAIN_TIMEOUT_S`` seconds (default 45s — comfortably under
the 60s ``stop_grace_period`` a compose/Kubernetes SIGTERM-then-SIGKILL
shutdown typically allows). Every future that finishes within the
window is finalized normally (``complete()``/``fail()``, using that
job's own ``lease_token``) before ``worker_loop`` returns — so a
handler that finishes during the drain window still gets its outcome
recorded instead of relying on lease-expiry recovery. Anything still
running when the timeout elapses is logged (job id + kind) and left
running; a hard kill at that point leaves the job 'running' with a
lease that later expires and is recovered via ``claim_next()``'s reclaim
path, or — if attempts are already exhausted by then — this module's own
``reap_exhausted()`` sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import socket
import time

from app.job_correlation import bind_request_id, unbind_request_id
from app.observability import metrics as obs_metrics
from app.worker import wakeup
from app.worker.registry import HEAVY_LANE, JOB_KINDS, LIGHT_LANE, JobKind

logger = logging.getLogger(__name__)

_HEAVY_CONCURRENCY = 1
_LIGHT_CONCURRENCY = 2

#: Floor on the heartbeat cadence so a misconfigured/very short
#: ``lease_seconds`` (e.g. in a test) can't spin the heartbeat loop.
_MIN_HEARTBEAT_INTERVAL_S = 0.5

#: Default bound for the shutdown drain (see module docstring). Kept
#: comfortably under the 60s stop_grace_period compose/k8s typically give
#: a container between SIGTERM and SIGKILL.
_DEFAULT_DRAIN_TIMEOUT_S = 45.0


def default_worker_id() -> str:
    """``<hostname>:<pid>`` — stable per-process identity for ``leased_by``.

    NOTE: this is shared by every lane slot in this process — it is NOT
    a unique per-claim identity. See the module docstring's same-worker
    double-execution note for why the atomicity guard uses ``lease_token``
    (minted fresh per claim) instead.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def _drain_timeout_s() -> float:
    raw = os.environ.get("AGNES_WORKER_DRAIN_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_DRAIN_TIMEOUT_S
    try:
        return max(float(raw), 0.0)
    except ValueError:
        logger.warning("worker: invalid AGNES_WORKER_DRAIN_TIMEOUT_S=%r, using default", raw)
        return _DEFAULT_DRAIN_TIMEOUT_S


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


@dataclasses.dataclass
class _InFlightJob:
    """One handler still running when shutdown drain begins — see the
    module docstring's "Graceful shutdown (bounded drain)" section."""

    job_id: str
    kind_name: str
    worker_id: str
    lease_token: str
    retry_in_seconds: int | None
    handler_future: asyncio.Future[None]
    hb_task: asyncio.Task[None]
    #: Lane the job was running in — carried through to `_drain_in_flight`
    #: so it can decrement `agnes_jobs_running`/`agnes_worker_lane_active`
    #: (incremented in `_run_one`, before the handoff) once this entry is
    #: finally resolved (finished or abandoned).
    lane: str
    #: `time.monotonic()` when the handler started — lets `_drain_in_flight`
    #: observe `agnes_job_duration_seconds` for a job that finishes during
    #: the drain window instead of losing its timing entirely.
    started_at: float


async def _heartbeat_loop(job_id: str, worker_id: str, lease_token: str, lease_seconds: int) -> None:
    """Extend the lease every ``lease_seconds/3`` while a handler runs.

    Stops silently (no exception) the first time ``heartbeat()`` returns
    ``False`` — see the module docstring for why the in-flight handler
    thread is left running regardless. A transient failure calling
    ``heartbeat()`` itself (e.g. a DB hiccup) is logged and retried at the
    next tick rather than killing this task outright — same hardening
    convention as ``_lane_slot``.
    """
    interval = max(lease_seconds / 3, _MIN_HEARTBEAT_INTERVAL_S)
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await asyncio.to_thread(_jobs_repo().heartbeat, job_id, worker_id, lease_token, lease_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "worker %s: heartbeat for job %s failed transiently (non-fatal); retrying next tick",
                worker_id,
                job_id,
            )
            continue
        if not ok:
            logger.warning(
                "worker %s: heartbeat lost for job %s (lease reclaimed — possibly by another slot of "
                "this same worker); abandoning heartbeat",
                worker_id,
                job_id,
            )
            return


async def _run_one(job: dict, kind: JobKind, worker_id: str, in_flight: dict[str, _InFlightJob]) -> None:
    """Run one claimed job's handler with a concurrent heartbeat, then
    complete()/fail() it.

    The handler runs as a separate, ``asyncio.shield``-ed future so that
    cancelling THIS coroutine (shutdown) cannot cancel the handler's
    underlying OS thread — see the module docstring. If our own await is
    cancelled before the handler finishes, the future is handed off to
    ``in_flight`` for ``worker_loop``'s bounded shutdown drain to finish
    waiting on (and finalize) instead of being abandoned here.

    Wraps ``agnes_jobs_running``/``agnes_worker_lane_active`` (lane-slot
    occupancy) and ``agnes_job_duration_seconds``/``agnes_job_failures_total``
    (outcome) around the handler invocation. The observability helpers
    (``app.observability.metrics``) never raise, so a metrics bug can't fail
    a job — but the occupancy gauges are only decremented here on the
    normal (success/exception) exit paths; a handed-off (cancelled) job
    stays "running" until ``_drain_in_flight`` resolves it, since the
    handler keeps executing in the background regardless of this
    coroutine's cancellation (see module docstring).
    """
    lease_token = job["lease_token"]
    # Bind the originating request-id (`_enqueued_by_request`, stamped at
    # enqueue time by `app.job_correlation.stamp_request_id`) into the same
    # `request_id_var` contextvar the request middleware uses, for the
    # duration of running this job. `asyncio.create_task`/`asyncio.to_thread`
    # both copy the *current* context at creation time, so this must happen
    # before `hb_task`/`handler_future` are created below in order for the
    # heartbeat task and the handler thread to see it. No-op (returns
    # `None`) when the payload has no (or a malformed) `_enqueued_by_request`
    # — never raises, so a missing/malformed key can't break job execution.
    rid_token = bind_request_id(job.get("payload_json"))
    try:
        hb_task = asyncio.create_task(
            _heartbeat_loop(job["id"], worker_id, lease_token, kind.lease_seconds),
            name=f"worker-heartbeat-{job['id']}",
        )
        handler_future = asyncio.ensure_future(asyncio.to_thread(kind.handler, job["payload_json"]))
        handed_off = False
        obs_metrics.begin_job_running(job["kind"], kind.lane)
        started_at = time.monotonic()
        try:
            await asyncio.shield(handler_future)
        except asyncio.CancelledError:
            handed_off = True
            in_flight[job["id"]] = _InFlightJob(
                job_id=job["id"],
                kind_name=job["kind"],
                worker_id=worker_id,
                lease_token=lease_token,
                retry_in_seconds=kind.retry_in_seconds,
                handler_future=handler_future,
                hb_task=hb_task,
                lane=kind.lane,
                started_at=started_at,
            )
            raise
        except Exception as exc:
            logger.exception("worker %s: job %s (kind=%s) failed", worker_id, job["id"], job["kind"])
            # Persist the outcome before recording it in metrics — if `.fail()`
            # itself raises, this propagates without ever having reported an
            # outcome that was never actually persisted.
            await asyncio.to_thread(
                _jobs_repo().fail,
                job["id"],
                worker_id,
                lease_token,
                str(exc),
                retry_in_seconds=kind.retry_in_seconds,
            )
            obs_metrics.record_job_duration(job["kind"], "failed", time.monotonic() - started_at)
            obs_metrics.record_job_failure(job["kind"], type(exc).__name__)
        else:
            # Same ordering rationale as the failure branch above.
            await asyncio.to_thread(_jobs_repo().complete, job["id"], worker_id, lease_token)
            obs_metrics.record_job_duration(job["kind"], "done", time.monotonic() - started_at)
        finally:
            if not handed_off:
                obs_metrics.end_job_running(job["kind"], kind.lane)
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
    finally:
        # Safe to reset unconditionally, including when handed off: the
        # handler future already copied its own context at creation time
        # (above), so resetting here doesn't affect a still-running
        # handed-off handler — it only prevents this contextvar from
        # leaking into whatever `_lane_slot` claims next in this same task.
        unbind_request_id(rid_token)


async def _lane_slot(
    lane: str,
    worker_id: str,
    poll_interval_s: float,
    in_flight: dict[str, _InFlightJob],
) -> None:
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
                # Idle: sleep up to poll_interval, but wake early on a
                # NOTIFY-driven signal (app.worker.wakeup). Degrades to a
                # plain poll_interval sleep when nothing signals (DuckDB
                # backend / listener down), so never worse than poll-only.
                await wakeup.idle_wait(poll_interval_s)
                continue

            # Counted here, not inside _run_one — a claim_next() success is
            # a claim regardless of what happens next (including the
            # no-registered-handler branch immediately below), so this is
            # the one place that sees every claim exactly once.
            obs_metrics.record_job_claim(job["kind"])

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
                obs_metrics.record_job_failure(job["kind"], "no-registered-handler")
                await asyncio.to_thread(
                    _jobs_repo().fail,
                    job["id"],
                    worker_id,
                    job["lease_token"],
                    f"no registered handler for kind {job['kind']!r}",
                    retry_in_seconds=None,
                )
                continue

            if lane == HEAVY_LANE:
                await asyncio.to_thread(_sweep_stale_scratch)

            await _run_one(job, kind, worker_id, in_flight)
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


async def _drain_in_flight(in_flight: dict[str, _InFlightJob], worker_id: str) -> None:
    """Bounded shutdown drain: wait on every handler future handed off by
    ``_run_one`` (see module docstring) for up to
    ``AGNES_WORKER_DRAIN_TIMEOUT_S`` seconds, finalizing whichever finish
    in time and logging (without finalizing) whichever don't.
    """
    if not in_flight:
        return
    timeout = _drain_timeout_s()
    logger.info(
        "worker %s: shutdown draining %d in-flight job(s) (timeout=%.0fs): %s",
        worker_id,
        len(in_flight),
        timeout,
        sorted(in_flight),
    )
    futures = [entry.handler_future for entry in in_flight.values()]
    _done, pending = await asyncio.wait(futures, timeout=timeout)
    for job_id, entry in in_flight.items():
        # `agnes_jobs_running`/`agnes_worker_lane_active` were incremented
        # in `_run_one` before the handoff and never decremented there
        # (the handler keeps running regardless of that coroutine's own
        # cancellation) — this drain loop is the only place every in-flight
        # entry is eventually resolved (finished in time, cancelled, or
        # abandoned on timeout), so it's the one place that must undo it,
        # exactly once per entry, regardless of which branch below fires.
        try:
            fut = entry.handler_future
            if fut in pending:
                logger.warning(
                    "worker %s: shutdown drain timed out after %.0fs — job %s (kind=%s) abandoned mid-flight; "
                    "will recover via lease expiry (reclaim or reap_exhausted)",
                    worker_id,
                    timeout,
                    job_id,
                    entry.kind_name,
                )
                entry.hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.hb_task
                continue
            if fut.cancelled():
                logger.warning(
                    "worker %s: in-flight job %s (kind=%s) handler future was cancelled during drain",
                    worker_id,
                    job_id,
                    entry.kind_name,
                )
                continue
            exc = fut.exception()
            duration = time.monotonic() - entry.started_at
            # Persist first, record metrics only after the persist call
            # actually succeeds — same ordering rationale as `_run_one`
            # (a failed `.fail()`/`.complete()` write must not still report
            # an outcome that never made it to the DB).
            try:
                if exc is not None:
                    logger.exception(
                        "worker %s: job %s (kind=%s) failed (finished during shutdown drain)",
                        worker_id,
                        job_id,
                        entry.kind_name,
                        exc_info=exc,
                    )
                    await asyncio.to_thread(
                        _jobs_repo().fail,
                        job_id,
                        entry.worker_id,
                        entry.lease_token,
                        str(exc),
                        retry_in_seconds=entry.retry_in_seconds,
                    )
                    obs_metrics.record_job_duration(entry.kind_name, "failed", duration)
                    obs_metrics.record_job_failure(entry.kind_name, type(exc).__name__)
                else:
                    await asyncio.to_thread(_jobs_repo().complete, job_id, entry.worker_id, entry.lease_token)
                    obs_metrics.record_job_duration(entry.kind_name, "done", duration)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "worker %s: job %s (kind=%s) finalization (complete/fail) failed during shutdown drain "
                    "(non-fatal); job will recover via lease expiry",
                    worker_id,
                    job_id,
                    entry.kind_name,
                )
            entry.hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry.hb_task
        finally:
            obs_metrics.end_job_running(entry.kind_name, entry.lane)


async def worker_loop(*, worker_id: str, poll_interval_s: float = 5.0) -> None:
    """Run the worker runtime until cancelled.

    Starts the reaper task, ``_HEAVY_CONCURRENCY`` heavy-lane slots and
    ``_LIGHT_CONCURRENCY`` light-lane slots, and waits on all of them.
    Cancelling the enclosing task (the ``canary_loop`` task-create/cancel
    pattern in ``app/main.py``'s lifespan) cancels every child task too —
    ``asyncio.gather`` propagates cancellation of its own awaiter to every
    task it's gathering. Before returning, performs one bounded drain of
    any handler still mid-flight (see module docstring).
    """
    in_flight: dict[str, _InFlightJob] = {}
    tasks = [asyncio.create_task(_reap_loop(poll_interval_s), name="worker-reaper")]
    # Best-effort PG LISTEN loop that wakes idle lane slots on a fresh
    # enqueue (see app.worker.wakeup). A clean no-op on DuckDB / if it can't
    # connect — the lane slots keep polling regardless.
    tasks.append(asyncio.create_task(wakeup.notify_listener(), name="worker-notify-listener"))
    tasks += [
        asyncio.create_task(_lane_slot(HEAVY_LANE, worker_id, poll_interval_s, in_flight), name=f"worker-heavy-{i}")
        for i in range(_HEAVY_CONCURRENCY)
    ]
    tasks += [
        asyncio.create_task(_lane_slot(LIGHT_LANE, worker_id, poll_interval_s, in_flight), name=f"worker-light-{i}")
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
        # Every lane slot has now stopped claiming new work. Any handler
        # that was mid-flight when its slot got cancelled was handed off
        # into `in_flight` (see _run_one) instead of being abandoned —
        # drain it here, bounded, before this function returns and
        # app/main.py proceeds to close the DB singletons.
        await _drain_in_flight(in_flight, worker_id)
