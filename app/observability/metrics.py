"""Prometheus `/metrics` endpoint + core HTTP request metrics.

Three-plane wave 2D (observability), tasks 1 (HTTP metrics) and 2 (job-queue
+ worker runtime metrics). Spec:
docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md §3.7,
plan: docs/superpowers/plans/2026-07-17-three-plane-wave2d-observability.md.

Design notes:

- `REGISTRY` is a *dedicated* `CollectorRegistry`, never the
  `prometheus_client` process-global default. Two reasons: (1) tests can spin
  up multiple `create_app()` instances / import this module repeatedly across
  a session without a third-party library's default-registry collectors
  (or a previous test's) leaking into our `/metrics` output, and (2)
  `generate_latest(REGISTRY)` only ever serializes series we defined.
- Every replica (api/gateway/worker process, per `app.roles`) exposes its own
  `/metrics` — there is no multiprocess/pushgateway aggregation. The
  `replica` label (`hostname:pid`) lets a scraper attribute a series to the
  process that emitted it; the `role` label carries the active
  `AGNES_ROLE` set for that process.
- `/metrics` itself is intentionally NOT under `/api/*` (like `/healthz` /
  `/readyz`): no auth, unauthenticated internal-scrape-only endpoint. See
  `app/api/health_probes.py` for the sibling pattern.

Job-queue + worker metrics (task 2):

- `agnes_jobs_queued` is NOT a plain `Gauge` — it's populated at SCRAPE time
  by `_QueuedJobsCollector`, a custom `Collector` registered on `REGISTRY`
  below. A background-updated gauge would either poll the DB on its own
  schedule (extra load, staleness) or need `worker_loop` to push a value
  nobody but the scraper reads; sampling `jobs_repo().list(status="queued")`
  directly inside `collect()` keeps it always-current with zero idle cost.
  The query is capped (`_QUEUED_SCAN_LIMIT`) so a huge queue can't turn every
  scrape into an unbounded table scan; `agnes_jobs_queued_capped` flips to 1
  when the cap was hit so the undercount is visible instead of silently
  wrong. `collect()` must never raise — `REGISTRY.collect()` (driven by
  `generate_latest()`) calls every registered collector's `collect()` in one
  pass, so one collector's exception would 500 the *entire* `/metrics`
  response, taking every other metric down with it. A failure here is
  caught, counted via `agnes_metrics_collector_errors_total`, and this
  collector simply yields nothing for that scrape.
- `REGISTRY` is built with `auto_describe=False` (the `CollectorRegistry()`
  default), so `REGISTRY.register(...)` never calls `collect()` — the custom
  collector's DB query only ever runs from an actual `/metrics` scrape, never
  at import/registration time.
- `agnes_jobs_running` / `agnes_worker_lane_active` are ordinary `Gauge`s
  mutated by `app/worker/runtime.py` around each handler invocation
  (`begin_job_running`/`end_job_running`) — these track live in-process
  state (a slot currently executing a handler), not something a scrape-time
  DB query could observe. Because each replica only increments/decrements
  its OWN slots, these two gauges are genuinely additive across replicas —
  `sum() by (kind)` gives the correct fleet-wide total.
- `agnes_jobs_queued` / `agnes_jobs_queued_capped` are the opposite: every
  replica's `_QueuedJobsCollector` queries the SAME shared job queue, so
  every replica reports an IDENTICAL value each scrape (a global queue
  depth, not a per-replica one) even though the series still carries a
  `replica` label (kept for consistency with every other series in this
  module, and so a scraper can attribute a given sample to the process that
  emitted it — see above). Summing these across `replica` (or `role`)
  N-counts the true depth; use `max() by (kind)` instead. See
  `docs/observability.md` for the operator-facing version of this note.
- Every `record_*`/`begin_job_running`/`end_job_running` helper below
  swallows its own exceptions (log + return) — the worker loop calls these
  as pure side effects around job execution, and a metrics bug must never
  fail a job or break the claim/complete/fail lifecycle.
- Cardinality guards: `kind` is bounded to `app.worker.registry.JOB_KINDS`
  (anything else collapses to `"other"` — e.g. a job row inserted with a
  since-deregistered kind, or between-process registry drift); `lane` is
  bounded to `heavy`/`light` (the caller's own value, already constrained by
  `JobKind.lane` validation in `app/worker/registry.py`); `outcome` is
  `done`/`failed`; the failure `reason` label is capped defensively
  (`_MAX_REASON_LEN`) and falls back to `"other"` for anything empty or
  implausibly long, even though in practice it's always a short exception
  class name.
"""

from __future__ import annotations

import logging
import os
import socket

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger(__name__)

# Dedicated registry — see module docstring. Every metric below is
# registered on THIS registry, never prometheus_client's global default.
# `auto_describe` defaults to False — see the module docstring's note on why
# that matters for the custom `_QueuedJobsCollector` registered below.
REGISTRY = CollectorRegistry()

# Label value used for the route-path-template when a request never matched
# a FastAPI route (e.g. a preflight OPTIONS response short-circuited by
# CORSMiddleware before routing runs). A constant, bounded-cardinality
# fallback — NEVER the raw request path, which would grow unboundedly under
# probing/scanning traffic.
UNMATCHED_PATH = "__unmatched__"

# The scrape endpoint's own path — excluded from recording so scraping
# itself doesn't grow the series it's reading (see METRICS_PATH usage in
# app/main.py's middleware).
METRICS_PATH = "/metrics"

http_requests_total = Counter(
    "agnes_http_requests_total",
    "Total HTTP requests served.",
    ["method", "path_template", "status", "role", "replica"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "agnes_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path_template", "role"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Job-queue + worker runtime metrics (task 2) — see module docstring.
# ---------------------------------------------------------------------------

#: Bound on the `jobs_repo().list(status="queued")` scan the custom
#: `_QueuedJobsCollector` below runs on every `/metrics` scrape. Keeps the
#: scrape cheap and bounded regardless of true queue depth; if the scan hits
#: this cap, `agnes_jobs_queued_capped` flips to 1 so the undercount is
#: visible instead of silently wrong (see module docstring).
_QUEUED_SCAN_LIMIT = 500

#: Defensive cap on the `agnes_job_failures_total` `reason` label value. In
#: practice this is always a short exception class name (e.g.
#: `"RuntimeError"`), but nothing stops a caller from ever passing something
#: longer/emptier — collapse those to `"other"` rather than let an unbounded
#: string become a label value.
_MAX_REASON_LEN = 64

metrics_collector_errors_total = Counter(
    "agnes_metrics_collector_errors_total",
    "Errors raised inside a scrape-time metrics Collector's collect(), caught "
    "so one broken collector can't 500 the whole /metrics endpoint.",
    ["collector"],
    registry=REGISTRY,
)

jobs_running = Gauge(
    "agnes_jobs_running",
    "Number of jobs currently executing, by kind and lane. Per-replica in-process "
    "state (unlike agnes_jobs_queued) — safe to sum() across replica/role for a "
    "fleet-wide total.",
    ["kind", "lane", "role", "replica"],
    registry=REGISTRY,
)

#: Agnes jobs range from sub-second housekeeping to multi-hour BigQuery
#: materializations and heavy Keboola syncs — the default `prometheus_client`
#: buckets (top bucket ~10s) would dump nearly everything into `+Inf`,
#: making the histogram useless for its actual workload. Explicit buckets
#: span 1s to 4h.
_JOB_DURATION_BUCKETS = (1, 5, 15, 30, 60, 300, 900, 3600, 14400, float("inf"))

job_duration_seconds = Histogram(
    "agnes_job_duration_seconds",
    "Job handler execution duration in seconds, by kind and outcome.",
    ["kind", "outcome", "role"],
    buckets=_JOB_DURATION_BUCKETS,
    registry=REGISTRY,
)

worker_lane_active = Gauge(
    "agnes_worker_lane_active",
    "Number of currently-busy concurrency slots in a worker lane.",
    ["lane", "role", "replica"],
    registry=REGISTRY,
)

job_claims_total = Counter(
    "agnes_job_claims_total",
    "Total jobs claimed off the queue by a worker lane slot, by kind.",
    ["kind", "role", "replica"],
    registry=REGISTRY,
)

job_failures_total = Counter(
    "agnes_job_failures_total",
    "Total job failures, by kind and a bounded failure-reason bucket.",
    ["kind", "reason", "role", "replica"],
    registry=REGISTRY,
)


def replica_id() -> str:
    """Stable per-process identity for multi-replica scrape disambiguation."""
    return f"{socket.gethostname()}:{os.getpid()}"


# Computed once at import — stable for the life of the process, per the
# task brief (hostname/pid never change after process start).
_REPLICA_ID = replica_id()


def role_label() -> str:
    """Current process's active AGNES_ROLE set as a single label value.

    A process can serve multiple roles at once (default `all` == every
    role); join them so the label stays a single well-formed token instead
    of trying to cram a set into one Prometheus label.
    """
    from app.roles import active_roles, is_all_in_one

    if is_all_in_one():
        return "all"
    return "+".join(sorted(r.value for r in active_roles()))


def observe_http(method: str, path_template: str, status: int, duration_s: float) -> None:
    """Record one completed HTTP request. Called by the middleware in app/main.py."""
    role = role_label()
    http_requests_total.labels(
        method=method,
        path_template=path_template,
        status=str(status),
        role=role,
        replica=_REPLICA_ID,
    ).inc()
    http_request_duration_seconds.labels(
        method=method,
        path_template=path_template,
        role=role,
    ).observe(duration_s)


def metrics_response() -> Response:
    """Render the current REGISTRY snapshot in the Prometheus text exposition format."""
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Job-queue + worker runtime metrics helpers (task 2)
# ---------------------------------------------------------------------------


def _bounded_kind(kind: str) -> str:
    """Collapse an unregistered/unknown job kind to `"other"`.

    Lazily imports `app.worker.registry.JOB_KINDS` (process-wide dict of
    currently-registered kinds) rather than importing it at module level —
    same deferred-import convention `app/worker/runtime.py` already uses for
    its own registry/repo lookups, so this module carries no import-time
    dependency on the worker subsystem.
    """
    from app.worker.registry import JOB_KINDS

    return kind if kind in JOB_KINDS else "other"


def _bounded_reason(reason: str) -> str:
    """Collapse an empty/implausibly-long failure reason to `"other"`."""
    if not reason or len(reason) > _MAX_REASON_LEN:
        return "other"
    return reason


def record_job_claim(kind: str) -> None:
    """Record one successful `claim_next()` off the queue. Never raises."""
    try:
        job_claims_total.labels(kind=_bounded_kind(kind), role=role_label(), replica=_REPLICA_ID).inc()
    except Exception:
        logger.exception("metrics: record_job_claim failed (non-fatal)")


def record_job_failure(kind: str, reason: str) -> None:
    """Record one job failure (handler exception, or a terminal
    no-registered-handler fail). Never raises."""
    try:
        job_failures_total.labels(
            kind=_bounded_kind(kind),
            reason=_bounded_reason(reason),
            role=role_label(),
            replica=_REPLICA_ID,
        ).inc()
    except Exception:
        logger.exception("metrics: record_job_failure failed (non-fatal)")


def record_job_duration(kind: str, outcome: str, duration_s: float) -> None:
    """Observe one completed handler's execution duration. Never raises."""
    try:
        job_duration_seconds.labels(kind=_bounded_kind(kind), outcome=outcome, role=role_label()).observe(duration_s)
    except Exception:
        logger.exception("metrics: record_job_duration failed (non-fatal)")


def begin_job_running(kind: str, lane: str) -> None:
    """Mark one lane slot as busy running `kind`. Pair with `end_job_running`
    around the handler invocation. Never raises."""
    try:
        role = role_label()
        jobs_running.labels(kind=_bounded_kind(kind), lane=lane, role=role, replica=_REPLICA_ID).inc()
        worker_lane_active.labels(lane=lane, role=role, replica=_REPLICA_ID).inc()
    except Exception:
        logger.exception("metrics: begin_job_running failed (non-fatal)")


def end_job_running(kind: str, lane: str) -> None:
    """Undo a matching `begin_job_running` call. Never raises."""
    try:
        role = role_label()
        jobs_running.labels(kind=_bounded_kind(kind), lane=lane, role=role, replica=_REPLICA_ID).dec()
        worker_lane_active.labels(lane=lane, role=role, replica=_REPLICA_ID).dec()
    except Exception:
        logger.exception("metrics: end_job_running failed (non-fatal)")


class _QueuedJobsCollector:
    """Scrape-time collector for `agnes_jobs_queued` (+ `_capped` sibling).

    See the module docstring for the full design rationale. `collect()` is
    called by `REGISTRY.collect()` (via `generate_latest()`) on every
    `/metrics` scrape — it queries `jobs_repo().list(status="queued")`
    directly, bounded by `_QUEUED_SCAN_LIMIT`, and MUST NOT raise: any
    failure here would otherwise 500 the entire `/metrics` response.
    """

    def collect(self):
        role = role_label()
        replica = _REPLICA_ID
        try:
            from src.repositories import jobs_repo

            rows = jobs_repo().list(status="queued", limit=_QUEUED_SCAN_LIMIT + 1)
        except Exception:
            logger.exception("metrics: agnes_jobs_queued collector failed (non-fatal)")
            try:
                metrics_collector_errors_total.labels(collector="jobs_queued").inc()
            except Exception:
                logger.exception("metrics: failed to increment agnes_metrics_collector_errors_total")
            return

        capped = len(rows) > _QUEUED_SCAN_LIMIT
        rows = rows[:_QUEUED_SCAN_LIMIT]

        counts: dict[str, int] = {}
        for row in rows:
            kind = _bounded_kind(row.get("kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1

        queued_family = GaugeMetricFamily(
            "agnes_jobs_queued",
            "Number of queued jobs by kind, sampled at scrape time (bounded scan — see "
            "agnes_jobs_queued_capped). Global queue depth: every replica samples the same "
            "shared queue, so this value is identical across replica/role. Use max() by (kind), "
            "never sum() — summing N-counts the true depth by the number of scraped replicas.",
            labels=["kind", "role", "replica"],
        )
        for kind, count in counts.items():
            queued_family.add_metric([kind, role, replica], count)
        yield queued_family

        capped_family = GaugeMetricFamily(
            "agnes_jobs_queued_capped",
            "1 if the last agnes_jobs_queued scrape hit the query cap "
            "(counts may undercount true queue depth), else 0. Global flag like "
            "agnes_jobs_queued — identical across replica/role; use max(), not sum().",
            labels=["role", "replica"],
        )
        capped_family.add_metric([role, replica], 1.0 if capped else 0.0)
        yield capped_family


# Registered once at import time. Safe because REGISTRY has auto_describe=False
# (the CollectorRegistry() default) — register() therefore never calls
# collect() itself, so this does NOT touch the DB at import time (only real
# /metrics scrapes do).
REGISTRY.register(_QueuedJobsCollector())


# No auth dependency — mirrors app/api/health_probes.py's unauthenticated LB
# probes. Internal scrape only; operators must not expose this publicly (see
# docs/observability.md).
router = APIRouter(tags=["metrics"])


@router.get(METRICS_PATH)
def metrics_endpoint() -> Response:
    return metrics_response()
