# Three-Plane Wave 2-B — Job Queue + Worker Runtime (WS B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durable dual-backend job queue + role-gated worker runtime with heavy/light lanes; the four heaviest background operations (data sync, marketplace sync, session collector, corporate memory) and the Jira webhook become enqueued jobs consumed by the worker role instead of running inside the API process.

**Architecture:** New `jobs` table (DuckDB migration v91→v92 + Alembic 0039) behind a repo pair reached via the factory; claim via `FOR UPDATE SKIP LOCKED` on PG / single-writer semantics on DuckDB; worker loop starts in lifespan when `role_enabled(Role.WORKER)` (wave-1 module `app/roles.py`); `/api/jobs` REST + CLI + MCP per command-UX; scheduler switches the migrated kinds to an enqueue call. Spec §3.3 of `docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md`.

**Tech Stack:** Python/FastAPI, DuckDB + Postgres via `src/repositories` factory, pytest.

## Global Constraints

- Read the playbooks BEFORE coding: `.claude/skills/agnes-conventions/references/repo-parity.md` (repo pair + factory + contract test), `migration.md` (DuckDB ladder + Alembic in lockstep), `endpoint-rbac.md` (gates), `command-ux.md` (CLI/MCP vocabulary). They are the binding convention; this plan defers to them on mechanics.
- Dual-backend discipline: DuckDB repo method ⇒ PG sibling in the SAME task; contract test in `tests/db_pg/` parametrizing both backends.
- API coverage ratchet: every new `/api/*` endpoint MUST gain a CLI invocation and MCP tool in the same task (`tests/test_api_coverage_ratchet*` guards this — run it).
- Default `all`-mode behavior stays user-equivalent: the worker loop runs in-process in `all` mode, so enqueued work still executes on a single-container deployment within seconds; no new deployment requirements.
- Current SCHEMA_VERSION is 91 (`src/db.py:50`) and latest Alembic is `0038_store_lint_v91.py`; main moves fast — RE-CHECK both at implementation time and renumber if needed.
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q` (pixeltable_pgserver initdb floods under -n auto are a known flake — verify via focused re-runs).
- CHANGELOG bullet ships in the final task of this plan.
- Vendor-agnostic content everywhere.

---

### Task 1: `jobs` table + repo pair + factory + contract test

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION bump + `_v91_to_v92` ladder step + fresh-install branch)
- Create: `migrations/versions/0039_jobs_v92.py`
- Create: `src/repositories/jobs.py`, `src/repositories/jobs_pg.py`
- Modify: `src/repositories/__init__.py` (factory entry `jobs_repo()`)
- Test: `tests/db_pg/test_jobs_contract.py`

**Interfaces (Produces):**

```python
# Both repos implement, identical signatures (see repo-parity.md for the pattern):
class JobsRepository:  # DuckDB; JobsRepositoryPG mirrors it
    def enqueue(self, kind: str, payload: dict, *, priority: int = 0,
                run_after: datetime | None = None, max_attempts: int = 3,
                idempotency_key: str | None = None) -> dict:
        """Insert a queued job and return its row (dict). If idempotency_key
        matches an existing job in status ('queued','running'), return THAT
        row unchanged (dedup) — no new insert."""
    def get(self, job_id: str) -> dict | None: ...
    def list(self, *, status: str | None = None, kind: str | None = None,
             limit: int = 50) -> list[dict]: ...
```

Schema (both ladders, same endpoint):

```sql
CREATE TABLE jobs (
    id VARCHAR PRIMARY KEY,            -- uuid4 hex, generated in repo
    kind VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|cancelled
    priority INTEGER NOT NULL DEFAULT 0,
    run_after TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_expires_at TIMESTAMP,
    leased_by VARCHAR,
    idempotency_key VARCHAR,
    error VARCHAR,
    created_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP
);
CREATE INDEX idx_jobs_claim ON jobs(status, priority, run_after);
CREATE UNIQUE INDEX idx_jobs_idem ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
```

(If partial unique indexes are unsupported on the DuckDB side, enforce idempotency dedup in the repo method inside the write lock instead, and say so in a comment — the CONTRACT is dedup behavior, not the index.)

- [ ] Step 1: failing contract test — enqueue returns row with id/status=queued; get roundtrip; idempotency dedup returns the same job id for a duplicate key while queued; list filters by status/kind. Parametrize both backends per the existing pattern in `tests/db_pg/test_*_contract.py` (copy a neighbor's fixture usage).
- [ ] Step 2: run → fails (no table/repo).
- [ ] Step 3: implement — migration steps per `migration.md` (BOTH: `_v91_to_v92` + fresh-install schema in `src/db.py`, Alembic `0039` upgrade/downgrade), repos per `repo-parity.md`, factory dispatch entry.
- [ ] Step 4: contract test green + `.venv/bin/pytest tests/test_db_schema_version.py -q` green (ladder endpoints match).
- [ ] Step 5: commit `feat(jobs): jobs table + dual-backend repository`

### Task 2: Claim/lease/complete lifecycle + lanes

**Files:** extend both repos + contract test from Task 1.

**Interfaces (Produces):**

```python
    HEAVY_LANE = "heavy"; LIGHT_LANE = "light"
    def claim_next(self, *, kinds: list[str], worker_id: str,
                   lease_seconds: int = 120) -> dict | None:
        """Atomically claim the oldest eligible queued job of the given kinds
        (status='queued', run_after IS NULL or <= now, ORDER BY priority DESC,
        created_at ASC). Sets status='running', leased_by, started_at,
        lease_expires_at, attempts += 1. PG: SELECT ... FOR UPDATE SKIP LOCKED.
        DuckDB: plain transaction (single writer process is guaranteed).
        Also RECLAIMS: a 'running' job whose lease_expires_at < now is eligible
        again (crash recovery) as long as attempts < max_attempts."""
    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool: ...
    def complete(self, job_id: str, worker_id: str) -> None: ...
    def fail(self, job_id: str, worker_id: str, error: str, *,
             retry_in_seconds: int | None = None) -> None:
        """attempts < max_attempts and retry_in_seconds is not None ⇒ back to
        'queued' with run_after=now+retry; else status='failed'."""
```

- [ ] Contract tests must cover: claim order (priority then FIFO); claim skips future run_after; expired-lease reclaim; heartbeat extends; fail-with-retry requeues; fail at max_attempts finalizes; two threads claiming concurrently on PG never double-claim (real threads test, like `test_seed_lease_contract.py` does).
- [ ] Commit `feat(jobs): claim/lease/retry lifecycle`

### Task 3: Worker runtime loop

**Files:**
- Create: `app/worker/__init__.py`, `app/worker/runtime.py`, `app/worker/registry.py`
- Modify: `app/main.py` (start/stop the loop in lifespan when `role_enabled(Role.WORKER)` — same task-create/cancel pattern as the canary loop)
- Test: `tests/test_worker_runtime.py`

**Interfaces (Produces):**

```python
# app/worker/registry.py
JOB_KINDS: dict[str, "JobKind"] = {}
@dataclass
class JobKind:
    name: str
    handler: Callable[[dict], None]     # sync callable, runs in a thread
    lane: str                            # HEAVY_LANE | LIGHT_LANE
    lease_seconds: int = 120
    retry_in_seconds: int | None = 300
def register_kind(kind: JobKind) -> None: ...

# app/worker/runtime.py
async def worker_loop(*, worker_id: str, poll_interval_s: float = 5.0) -> None:
    """Two lanes: heavy concurrency 1, light concurrency 2 (spec §3.3 / Q2).
    Each lane: claim_next(kinds of that lane) → run handler via
    asyncio.to_thread → heartbeat task every lease/3 → complete/fail.
    Before each HEAVY job: sweep stale scratch (kbc-export-*, *.tmp older
    than 24h under the temp dir — reuse/extract the existing orphan-sweep
    logic referenced in app/main.py lifespan, grep 'orphan'/'kbc-export')."""
```

- [ ] Tests with fake registered kinds + in-memory DuckDB backend: heavy lane never runs 2 concurrently while light lane proceeds; handler exception → fail-with-retry; graceful cancel mid-poll.
- [ ] Wire in lifespan (worker role only). In `all` mode this runs too — that is by design (single-container deployments keep working).
- [ ] Commit `feat(worker): role-gated worker loop with heavy/light lanes`

### Task 4: Job kinds for the four heavy operations + Jira webhook conversion

**Files:**
- Create: `app/worker/kinds.py` (registration module, imported from lifespan before loop start)
- Modify: `connectors/jira/service.py` or its webhook route (grep `rebuild_source("jira")` / the webhook handler in `app/api/jira_webhooks.py`) — replace the inline rebuild call with `jobs_repo().enqueue("jira-refresh", ..., idempotency_key="jira-refresh")`
- Test: `tests/test_worker_kinds.py`

Kinds to register (handlers WRAP the existing functions — do not reimplement logic; locate each by the endpoint its scheduler job posts today):
- `data-refresh` (HEAVY, idempotency `sync`): body of `/api/sync/trigger`'s `_run_sync` path (grep `_run_sync` in `app/api/sync.py`; the handler calls `_run_sync(...)` with the same defaults; the old `_sync_lock` fast-fail stays in place for the legacy HTTP path but the job path relies on idempotency dedup).
- `marketplaces-sync` (LIGHT): function behind `/api/marketplaces/sync-all`.
- `session-collector` (LIGHT): function behind `/api/admin/run-session-collector`.
- `corporate-memory` (LIGHT): function behind `/api/admin/run-corporate-memory`.
- `jira-refresh` (HEAVY): `SyncOrchestrator().rebuild_source("jira")`.

- [ ] Tests: registry contains the five kinds with correct lanes; jira webhook route now enqueues (assert a jobs row exists, no inline rebuild — monkeypatch the orchestrator to fail if called inline); each handler is the existing function (identity/monkeypatch check, no logic duplication).
- [ ] Update any existing webhook tests that asserted inline rebuild side-effects.
- [ ] Commit `feat(worker): job kinds for sync, marketplace, session, memory + async jira webhook`

### Task 5: /api/jobs endpoints + CLI + MCP

**Files:**
- Create: `app/api/jobs.py` (router), CLI command (grep `cli/` for the `admin` command group pattern), MCP foundation tool in `app/api/mcp/foundation_tools.py`
- Modify: `app/main.py` (router registration)
- Test: `tests/test_jobs_api.py` (+ ratchet: `tests/test_api_coverage_ratchet*`, `tests/test_mcp_tool_parity.py`)

Endpoints (all per `endpoint-rbac.md`):
- `POST /api/jobs` `{kind, payload?, idempotency_key?}` → enqueue, 202 `{job}` — gate: `require_admin` OR scheduler token (grep how existing admin-run endpoints accept `app/auth/scheduler_token`; follow the same dual-accept pattern).
- `GET /api/jobs/{job_id}` → `{job}` — same gate.
- `GET /api/jobs?status=&kind=&limit=` → `{jobs: [...]}` — same gate.
- Unknown `kind` on enqueue ⇒ 400 listing registered kinds.

CLI: `agnes admin jobs enqueue <kind>`, `agnes admin jobs show <id>`, `agnes admin jobs list [--status --kind --limit --json]` — vocabulary per `command-ux.md`. MCP: one foundation tool exposing get/list (and enqueue if the existing foundation-tool pattern includes admin mutations — follow parity with a comparable admin tool).

- [ ] Ratchet + parity tests green.
- [ ] Commit `feat(jobs): REST + CLI + MCP surface`

### Task 6: Scheduler → enqueuer for migrated kinds + classification doc

**Files:**
- Modify: `services/scheduler/__main__.py` — the four migrated rows (`data-refresh`, `marketplaces`, `session-collector`, `corporate-memory`) switch their target to `POST /api/jobs` with the matching kind + idempotency key; every other row is UNCHANGED.
- Create: `docs/jobs-classification.md` — table of all scheduler rows: kind / migrated-to-queue (4+jira) / stays-HTTP (with one-line why: sub-second poke, or deferred to a named later workstream).
- Test: extend the scheduler's existing tests (grep `tests/` for scheduler tests) asserting the four rows now post to `/api/jobs` with correct body.

- [ ] Commit `feat(scheduler): enqueue migrated job kinds via /api/jobs`

### Task 7: `/api/sync/trigger` rides the queue

**Files:** `app/api/sync.py` + its tests.

Change: the endpooint enqueues `data-refresh` (idempotency `sync`) instead of `background_tasks.add_task(_run_sync, ...)`. Response keeps the existing success shape PLUS `job_id`; the already-running case returns the EXISTING job (200, `{status: "already_running", job_id}`) instead of 409 — grep existing tests/clients for 409 reliance (`_recent_trigger_at`, CLI, web UI) and update them; if a caller genuinely depends on 409, keep returning 409 with `job_id` included and note it.

- [ ] Commit `feat(sync): trigger enqueues data-refresh job`

### Task 8: Docs + CHANGELOG + full suite

- `docs/DEPLOYMENT.md` Multi-process section: add a paragraph on the worker role consuming the job queue + lanes; `docs/architecture.md` short jobs section; CHANGELOG bullet (Added) covering wave 2-B.
- Full suite + api-coverage ratchet + schema-version gate green.
- [ ] Commit `docs: job queue + worker runtime`

## Self-review notes

Deliberately deferred (say so in docs/jobs-classification.md): collections/corpus ingest and admin register-table conversion (they are analytics-writer paths that WS E rebuilds on DuckLake — converting them twice is waste); remaining scheduler rows stay HTTP until classified needs emerge; LISTEN/NOTIFY wakeup (polling at 5 s is fine for v1 — note as future optimization); job-payload request-id correlation lands with WS G structured logging.
