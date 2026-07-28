# Three-Plane Wave 2-D — Observability (WS G)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Prometheus `/metrics` on every role (HTTP latency histograms, job-queue depth/lag, coordination/redis health, worker lane occupancy, per-process identity), a harness-local Prometheus + cAdvisor in the m-tier profile, and request-id → job-id log correlation — so the branch's load test can be measured and its chaos scenarios diagnosed.

**Architecture:** New `app/observability/metrics.py` using `prometheus_client` (multiprocess-safe registry disabled — each replica exposes its own `/metrics`, scraped separately; label every series with `role` + `replica`). Middleware records HTTP metrics; job repo + worker loop update gauges/counters; a small collector reads queue depth on scrape. Spec §3.7. `/metrics` is unauthenticated like `/healthz` (internal scrape only; document that operators must not expose it publicly, or gate by network).

**Tech Stack:** Python/FastAPI, `prometheus_client`, pytest.

## Global Constraints

- Default behavior unchanged; `/metrics` served on all roles with zero config. No new required deps beyond `prometheus_client` (add to core deps).
- `/metrics` is NOT under `/api/*` (like the probes) — no auth, no coverage-ratchet entry; add to the endpoint-smoke sweep the same way the probes were.
- Metric names follow Prometheus conventions (`agnes_` prefix, `_total`/`_seconds` suffixes, snake_case); every series carries `role` and `replica` labels.
- Full suite before push; CHANGELOG in the final task; vendor-agnostic.

---

### Task 1: Metrics module + registry + core HTTP metrics + middleware

**Files:** Create `app/observability/__init__.py`, `app/observability/metrics.py`; modify `app/main.py` (middleware + router). Test `tests/test_metrics.py`.

**Interface (Produces):**
```python
# metrics.py
REGISTRY: CollectorRegistry  # dedicated, not the global default
http_requests_total: Counter    # labels: method, path_template, status, role, replica
http_request_duration_seconds: Histogram  # labels: method, path_template, role
def replica_id() -> str  # hostname:pid, stable per process
def metrics_response() -> Response  # generate_latest(REGISTRY), correct content-type
def observe_http(method, path_template, status, duration_s) -> None
router: APIRouter  # GET /metrics (no auth) → metrics_response()
```
Middleware records method/route-template (use the matched route's path template, NOT the raw path — cardinality!)/status/duration; skip `/metrics` itself. Register `router` + middleware in `app/main.py`.

- [ ] Tests: `/metrics` returns 200 text/plain with `agnes_http_requests_total`; a request increments the counter; path_template used (not raw path with ids → no cardinality blowup); role+replica labels present.
- [ ] Commit `feat(observability): prometheus /metrics with HTTP request metrics`

### Task 2: Job-queue + worker metrics

**Files:** `app/observability/metrics.py` (add collectors), `app/worker/runtime.py` (lane occupancy), a queue-depth collector reading `jobs_repo()`; test.

**Produces:** `agnes_jobs_queued` (gauge, by kind), `agnes_jobs_running` (gauge, by kind+lane), `agnes_job_duration_seconds` (histogram, by kind+outcome), `agnes_worker_lane_active` (gauge, by lane), `agnes_job_claims_total` / `agnes_job_failures_total` (counters). Queue-depth gauge populated by a custom collector invoked at scrape time (a `Collector` subclass registered in REGISTRY that queries `jobs_repo().list(status=...)` counts — keep it cheap, cap the query). Worker loop updates lane-active + duration + outcome counters around each handler.

- [ ] Tests: enqueue N jobs → scrape shows queued gauge N by kind; run a fake job → duration observed + outcome counter; lane-active reflects a running heavy job. Guard the scrape-time collector against a DB error (must not 500 the whole /metrics).
- [ ] Commit `feat(observability): job queue and worker runtime metrics`

### Task 3: Coordination/redis health metric + readiness integration

**Files:** `app/observability/metrics.py`, wire a `/readyz` extra-check reuse; test.

**Produces:** `agnes_coordination_up` (gauge 1/0 via `coordination().ping()` at scrape), `agnes_coordination_backend_info` (info metric with backend=memory|redis), `agnes_readiness` (gauge 1/0 from the wave-1 ReadinessState). The coordination ping at scrape must be timeout-bounded (the redis client already has socket timeouts) and never raise out of /metrics.

- [ ] Tests: memory backend → coordination_up=1, backend_info memory; readiness gauge reflects tripped state.
- [ ] Commit `feat(observability): coordination and readiness metrics`

### Task 4: Request-id → job-id log correlation

**Files:** find the existing request-id mechanism (grep `request_id` in app/logging_config.py / middleware — the structured logs already carry `request_id`), thread it into job payloads: when an API handler enqueues a job, stamp the current request-id into `payload_json` (`_enqueued_by_request`); the worker, when running a job, binds that id into its logging context so worker logs for that job carry the originating request-id. Test.

- [ ] Tests: enqueue within a request → job payload carries the request-id; worker log context includes it when running (assert via caplog / the structured logger's bound fields).
- [ ] Commit `feat(observability): correlate job execution logs with originating request-id`

### Task 5: m-tier Prometheus + cAdvisor + docs + CHANGELOG + suite

**Files:** `docker-compose.mtier.yml` (add `prometheus` + `cadvisor` services, profile mtier), `deploy/prometheus/prometheus.yml` (scrape api1/api2/gateway/worker `/metrics` + cadvisor), `scripts/dev/mtier-smoke.sh` (assert prometheus scrapes at least one agnes target `up==1`), `docs/DEPLOYMENT.md` + `docs/observability.md` (metrics section: endpoint, key series, scrape config, the "don't expose publicly" note), `CHANGELOG.md`.

- [ ] Prometheus config scrapes all four role containers by compose DNS name:8000/metrics + cadvisor:8080. Smoke: after boot, `curl prometheus:9090/api/v1/query?query=up` shows an agnes target up. (Docker-gated — if daemon down, static-validate the config with `promtool check config` if available, else yaml-lint; note live run deferred.)
- [ ] Full suite green.
- [ ] Commit `docs: observability metrics (wave 2D)`

## Self-review notes

Deferred (say so): OpenTelemetry tracing (explicit non-goal, spec §3.7); Grafana dashboards (operator concern, out of repo); pushgateway (not needed — scrape model); per-tenant metrics (single-tenant). PostHog stays for product analytics — Prometheus is the ops/scrape signal, not a replacement.
