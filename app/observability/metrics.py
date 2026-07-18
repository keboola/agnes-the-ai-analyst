"""Prometheus `/metrics` endpoint + core HTTP request metrics.

Three-plane wave 2D (observability), task 1. Spec:
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
"""

from __future__ import annotations

import os
import socket

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# Dedicated registry — see module docstring. Every metric below is
# registered on THIS registry, never prometheus_client's global default.
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


# No auth dependency — mirrors app/api/health_probes.py's unauthenticated LB
# probes. Internal scrape only; operators must not expose this publicly (see
# docs/observability.md).
router = APIRouter(tags=["metrics"])


@router.get(METRICS_PATH)
def metrics_endpoint() -> Response:
    return metrics_response()
