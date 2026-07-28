# Database Backend State Machine — Admin-Controlled Migrations

**Status:** Design  
**Date:** 2026-05-27  
**Related:** PR #388 (Postgres app-state layer), PR #451 (revert), PR #454 (re-do + root-cause fix), Issue #452

## Problem

Today the Postgres backend is configured exclusively via the `DATABASE_URL` environment variable in `/opt/agnes/.env`, written at VM provision time by `startup-script.sh.tpl` (Pattern A) or manually by operators (Pattern B). Backend selection is made once at boot; switching it requires SSH + file edit + container restart.

Three concrete user-facing problems flow from that:

1. **No visibility.** The UI doesn't surface which backend is active. An admin reading `/admin/server-config` can't tell whether their instance state lives in DuckDB, a side-car Postgres container, or a managed cloud Postgres. Operators infer it from the VM's `.env`.
2. **No safe migration path.** When the side-car has been running for a week and the admin wants to move to a managed Cloud SQL / RDS instance, today's flow is: dump side-car PG manually with `pg_dump`, restore to cloud manually with `psql`, edit `.env`, restart compose. Every step is operator-skill-gated; mistakes lose data.
3. **No idempotent retry.** If `data-migrate` (the compose one-shot service from PR #388) fails midway, the only recovery is reading container logs and re-running compose. There's no progress feedback, no automatic resume, no audit trail of who triggered the migration.

The goal of this spec is to replace ad-hoc `.env` editing with an explicit **state machine** controlled from the admin UI and CLI, with first-class progress reporting, idempotent retry, and an audit-logged transition history.

## Approach

**Four backend states** — `duckdb`, `side_car`, `cloud`, `duckdb_quack` — stored in `instance.yaml::database.backend`. Transitions are admin-initiated through `/admin/server-config` or `agnes admin db migrate`, and run as asynchronous jobs (subprocess) with pollable status. Architecture splits responsibilities cleanly: the app handles all Python migration logic in-process (alembic, data copy, verify); a host-side systemd timer (`agnes-state-applier.timer`) handles Docker compose lifecycle changes (start/stop side-car container, recreate app+scheduler on backend flip) through a filesystem flag at `/data/state/db-state-target.flag`. The app needs no docker socket access.

**Transitions are multi-destination, not forward-only.** Any stable backend can migrate to any other stable backend — operators move between DuckDB, side-car PG, managed cloud PG, and (future) DuckDB-Quack as cost, HA needs, compliance posture, or deployment scale change. The migrator dispatches based on the (source, target) pair to the appropriate copy primitive:

| From → To | Copy primitive | Notes |
|---|---|---|
| `duckdb` → `side_car` / `cloud` | `copy_duckdb_to_pg` | Original cutover path |
| `side_car` ↔ `cloud` | `copy_pg_to_pg` | Graduate to managed PG or DR-rollback to on-VM |
| `side_car` / `cloud` → `duckdb` | `copy_pg_to_duckdb` | Cost reduction, dev snapshot, compliance re-eval. Uses DuckDB UPSERT for idempotent replay. |
| `duckdb` ↔ `duckdb_quack` | (future) Quack-aware file conversion | Production-ready in DuckDB 2.0 (~fall 2026) |

Each backend's data is preserved post-migration: DuckDB stays as a frozen file, side-car PG keeps its volume, cloud PG keeps its instance. The backup artifact under `/data/state/backups/` is the recovery point if the migrator misbehaves; reverse migrations make a UI-driven rollback equally valid.

Existing `scripts/migrate_duckdb_to_pg/` (idempotent via `ON CONFLICT DO NOTHING` + SHA-256 checksums) is reused; new `db_state_migrator.py` orchestrates alembic, data copy, verification, and backup steps in a single subprocess.

## Detailed design

### State machine

```
                        ┌─────────────────────┐
                        │   DUCKDB            │ ← fresh-install default
                        │   single-process    │   ($0; multi-process via
                        │   file-locked file  │    flock — fragile under load)
                        └──┬──────────────┬───┘
                           │              │
                           │              ▼
                           │   ┌─────────────────────┐
                           │   │  DUCKDB_QUACK       │ ← FUTURE
                           │   │  Quack client-server│   (DuckDB 2.0,
                           │   │  multi-process safe │    ~fall 2026)
                           │   └─────────────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │   SIDE_CAR          │ ← multi-process safe today.
                  │   postgres:16 on    │   Side-car container on VM,
                  │   the same VM       │   ~200 MiB RAM, $0 cloud.
                  └──┬──────────────────┘
                     │
                     ▼
            ┌─────────────────────┐
            │   CLOUD             │ ← HA, managed backups, PITR,
            │   managed Postgres  │   compliance posture. Cloud SQL /
            │   (Cloud SQL etc.)  │   RDS / Supabase / Neon via Auth
            └─────────────────────┘    Proxy.

All four stable states are reachable from each of the other three.
Arrows above only show the "happy path" cutover; reverse migrations
(cost reduction, DR rollback, dev snapshot, compliance re-evaluation)
are equally first-class.
```

**Multi-destination, not forward-only.** Operators frequently shift between backends as the deployment scale changes:

- Initial deploy: `DUCKDB` (zero infra)
- Multi-process load grows: `DUCKDB → SIDE_CAR` (still on-VM, but lock-contention gone)
- Enterprise customer: `SIDE_CAR → CLOUD` (managed HA + backups)
- Cost re-evaluation: `CLOUD → SIDE_CAR` or `→ DUCKDB`
- Future: `SIDE_CAR → DUCKDB_QUACK` (drop the PG dependency once Quack is production-ready)

Transient `*_in_progress` states (`side_car_in_progress`, `cloud_in_progress`, `duckdb_quack_in_progress`) distinguish "transition running" from "transition completed" so concurrent migration attempts can be rejected and crashed migrations can be detected on startup.

### Why DuckDB stays a first-class long-term backend

The PR #388 framing of *"DuckDB only for analytics"* is **explicitly retired**. DuckDB remains valid for app-state as well — both code paths (`src/repositories/X.py` for DuckDB, `X_pg.py` for PG) are maintained in parity through cross-engine contract tests, and the dual-repo discipline applies to every new feature (one PR → both backends). The only practical limit today is multi-process write (app + scheduler containers), which `SIDE_CAR` solves and `DUCKDB_QUACK` will solve when production-ready.

### DuckDB Quack — placeholder state

`BackendState.DUCKDB_QUACK` is reserved in the enum + transition matrix today, but `validate_transition` raises `BackendNotYetSupportedError` (a `NotImplementedError` subclass) for any transition targeting it. This makes the API + persisted-state contract stable when runtime support lands.

When DuckDB 2.0 (~fall 2026) ships production-grade Quack protocol:

1. Drop `DUCKDB_QUACK` from `_NOT_YET_SUPPORTED_TARGETS` in `src/db_state_machine.py`
2. Add a Quack server lifecycle to `scripts/ops/agnes-state-applier.sh` (start/stop Quack server container, same shape as the postgres side-car lifecycle)
3. Add Quack-aware copy primitives (`copy_duckdb_to_quack`, `copy_pg_to_quack`, …) in `scripts/db_state_migrator.py`
4. UI + CLI surface the new target with no API contract break

Operators with old instance.yaml referencing `duckdb_quack` before runtime support lands see the clear `BackendNotYetSupportedError` message rather than crashing on an unknown state — the placeholder posture means the upgrade is graceful.

### Transition contracts

#### Transition 1: `duckdb` → `side_car`

| Step | Action | On failure |
|---|---|---|
| 1. Lock | Acquire `/data/state/db-migration.lock` (flock); set `instance.yaml::database.backend = "side_car_in_progress"` | 409 if held; abort if write fails |
| 2. Side-car spinup | Write `db-state-target.flag = "side-car-enabled"`; host applier brings up `postgres` container | Fail job if container not healthy within 60 s |
| 3. Alembic | `alembic upgrade head` against side-car PG | Job failed; target rolled back; retry safe (alembic idempotent) |
| 4. Data copy | `scripts/migrate_duckdb_to_pg.py --duckdb-path /data/state/system.duckdb --target side-car` | Job failed at table N; retry skips already-migrated rows (ON CONFLICT DO NOTHING) |
| 5. Verify | Row-count compare DuckDB ↔ side-car per table | Job failed with table-level diff; admin inspects, retries |
| 6. Backup | `gzip /data/state/system.duckdb → /data/state/backups/duckdb-pre-sidecar-{ts}.duckdb.gz` | Hard fail if disk full; no flip |
| 7. Flip | `instance.yaml::database.backend = "side_car"`, `database.url = "postgresql+psycopg://agnes:$PASS@postgres:5432/agnes"` | Atomic write via tmp + rename; failure leaves state as `side_car_in_progress` for retry |
| 8. Mark success + exit | Subprocess writes job.json `status=success`, exits cleanly. Host applier (next tick) detects backend change and recreates app+scheduler. | If host applier fails to bring app back, existing observability/alerting fires; migration job remains `success` (data is safe, only app down). |
| 9. App startup verify | After restart, app reads `instance.yaml`, opens new engine against `side_car`, runs `/api/health` self-check; writes `verify_health=true` into job.json | Critical alert; backend flipped but app unhealthy. Manual ops intervention. |
| 10. Audit | `audit_log`: `db.backend.migrate_completed`, `from=duckdb`, `to=side_car`, `job_id`, `duration_sec` | — |

DuckDB `system.duckdb` stays on disk after step 6 — the live writer is now PG (factory routes writes), but the DuckDB file is **not frozen forever**: a reverse `side_car → duckdb` migration via the UI replays writes from PG back into a fresh DuckDB file, leaving the previous file as the recovery point. For emergency rollback without UI access, ops use the gzipped backup.

#### Transition 2: `side_car` → `cloud`

| Step | Action | On failure |
|---|---|---|
| 1. Validate (synchronous, pre-job) | Open connection, `SELECT 1`, `server_version()`. Min PG 15 required. | 400 immediately; no job created |
| 2. Lock + state | Acquire migration lock; `database.backend = "cloud_in_progress"` | 409 if held |
| 3. Alembic | `alembic upgrade head` against cloud PG | Retry safe |
| 4. Backup side-car | `docker exec postgres pg_dump -U agnes -F c agnes > /data/state/backups/sidecar-pre-cloud-{ts}.dump` (pg_dump custom format, compressed) | Hard fail if disk full |
| 5. Data copy | `scripts/db_state_migrator.py --source side-car --target cloud` (same idempotent script, source = side-car PG instead of DuckDB) | Retry skips matched rows |
| 6. Verify | Row-count compare side-car ↔ cloud per table | Diff inspectable |
| 7. Flip backend | `database.backend = "cloud"`, `database.url = "<cloud-url>"` (atomic write) | — |
| 8. Restart + side-car teardown | Flag `cloud-only`; host applier recreates app+scheduler with new URL, then `docker compose stop postgres` + `docker rm postgres` | App must come up healthy before side-car stop |
| 9. Verify health | `/api/health` 200 against cloud PG, `SELECT count(*) FROM users` matches expected | Alert; ops re-enables side-car via flag override |
| 10. Audit | `db.backend.migrate_completed`, `from=side_car`, `to=cloud` | — |

`/data/postgres` host bind preserved after side-car removal (still on disk). The pg_dump backup in step 4 is the canonical "before cloud cutover" restore point.

### Components

#### New files

| Path | Purpose | ~LOC |
|---|---|---|
| `src/db_state_machine.py` | State validation (allowed transitions), atomic state writes via instance.yaml overlay, audit log emission, lock handling | 150 |
| `scripts/db_state_migrator.py` | Subprocess orchestrator: takes `--to=side_car \| cloud` + optional `--cloud-url`. Runs alembic + data copy + verify + backup. Writes job status to `/data/state/db-jobs/{id}.json`. Reuses `scripts/migrate_duckdb_to_pg/` modules. | 250 |
| `app/api/db_state.py` | FastAPI router. 4 endpoints under `/api/admin/db/*`. Admin-gated via existing `require_admin`. | 120 |
| `scripts/ops/agnes-state-applier.sh` | Host-side systemd timer-driven (every 30 s). Reads `db-state-target.flag`; writes COMPOSE_FILE assembly; calls `docker compose up -d --force-recreate` for affected services. Reads `instance.yaml::database.backend` for sanity check. | 80 |
| `app/web/static/js/admin/db_state.js` | UI: render current-state card, transition buttons, modal for cloud URL input, polling status updater, progress bar rendering. | 100 |
| `cli/commands/db.py` | Click-based CLI: `agnes admin db state`, `agnes admin db migrate <target>`, `agnes admin db job <id>`, `agnes admin db cancel <id>`. Thin HTTP wrapper using existing PAT auth + `~/.agnes/credentials`. | 100 |

#### Modified files

| Path | Change |
|---|---|
| `src/db_pg.py` | `_resolve_url()` reads `instance.yaml::database.url` first; falls back to env var. New `dispose_engine()` clears singleton; `get_engine()` re-resolves. |
| `src/repositories/__init__.py` | `use_pg()` reads `instance.yaml::database.backend != "duckdb"` first; falls back to env-var presence. |
| `infra/modules/customer-instance/startup-script.sh.tpl` | Initial `instance.yaml::database.backend = "duckdb"` write. Install + enable `agnes-state-applier.timer`. |
| `app/instance_config.py` | New `get_database_config()` helper; cache invalidation on POST `/api/admin/db/migrate` success. |
| `app/web/templates/admin_server_config.html` | New "Database backend" card section: current state badge, transition button, modal for cloud URL, progress region, audit history table. |
| `cli/main.py` | Register `db` subcommand group under `admin`. |

#### API endpoints

```
GET  /api/admin/db/state
  → 200 {
      backend: "duckdb" | "side_car" | "cloud" |
               "side_car_in_progress" | "cloud_in_progress",
      url_redacted: "postgresql+psycopg://agnes:****@host:5432/agnes" | null,
      current_job_id: "uuid" | null,
      allowed_transitions: ["side_car"] | ["cloud"] | [],
      health: { reachable: bool, server_version: str | null }
    }

POST /api/admin/db/migrate
  Body: { target: "side_car" | "cloud", cloud_url?: string }
  → 202 { job_id: "uuid", status: "running" }
  → 400 invalid transition / malformed cloud_url / cloud PG unreachable
  → 409 migration already in progress (returns existing job_id)

GET  /api/admin/db/job/{job_id}
  → 200 {
      job_id,
      status: "running" | "success" | "failed" | "cancelled",
      target_backend, source_backend,
      started_at, completed_at (null if still running),
      current_step: "validate" | "alembic" | "data_copy" | "verify" |
                    "backup" | "flip_backend" | "app_restart" | "verify_health",
      progress_pct: 0..100,
      table_progress?: { current_table, tables_done, tables_total },
      summary?: { rows_total, duration_sec },
      error?: { step, message, class }
    }
  → 404 unknown job_id

POST /api/admin/db/cancel/{job_id}
  → 200 { cancelled: true }
  → 409 past point-of-no-return (step >= "flip_backend"); manual recovery required
```

#### Persisted state

| Path | Format | Lifecycle |
|---|---|---|
| `/data/state/db-state-target.flag` | Plain text: `duckdb` \| `side-car-enabled` \| `cloud-only` | Written by app on transition; read by host applier; reset by app after restart confirmed |
| `/data/state/db-migration.lock` | flock (advisory) | Held by migration subprocess; released on exit |
| `/data/state/db-jobs/{job_id}.json` | JSON | Written every status step. Kept indefinitely (small). Pruning is a follow-up. |
| `/data/state/backups/duckdb-pre-sidecar-{ts}.duckdb.gz` | gzipped DuckDB file | Written at step 6 of `duckdb→side_car`. Kept indefinitely; ops manages cleanup. |
| `/data/state/backups/sidecar-pre-cloud-{ts}.dump` | pg_dump custom format | Written at step 4 of `side_car→cloud`. Kept indefinitely. |
| `instance.yaml::database` | YAML overlay | `{ backend: "...", url: "..." }`. App reads on startup + after migration success. |

#### Audit log events

| Event | When | Fields |
|---|---|---|
| `db.backend.migrate_started` | POST `/migrate` returns 202 | from, to, job_id, cloud_url_redacted |
| `db.backend.migrate_completed` | Job reaches `success` | from, to, job_id, duration_sec, rows_migrated_total |
| `db.backend.migrate_failed` | Job reaches `failed` | from, to, job_id, failed_at_step, error_class, error_message |
| `db.backend.migrate_cancelled` | Admin cancels before point-of-no-return | from, to, job_id, cancelled_at_step |

### Data flow — Scenario A: DuckDB → side-car (typical first migration)

```
admin (UI)            app (FastAPI + subprocess)        host (systemd applier)        postgres container
─────────             ─────────────────────────         ──────────────────────         ──────────────────
[click Migrate]
   ↓
POST /api/admin/db/migrate {target:"side_car"}
                       ├─ lock acquired
                       ├─ instance.yaml: backend="side_car_in_progress"
                       ├─ write target.flag = "side-car-enabled"
                       ├─ spawn subprocess (db_state_migrator)
                       └─ return 202 {job_id}
                                                       [timer tick, t=~15s]
                                                       ├─ reads target.flag
                                                       ├─ adds postgres.yml to COMPOSE_FILE
                                                       └─ docker compose up -d postgres
                                                                                       [postgres init ~10s]
                                                                                       [postgres ready]
[poll job/{id}]        subprocess:
   ←──────────────     ├─ poll postgres healthy (~5s)
                       ├─ alembic upgrade head (~3s)
                       ├─ migrate_duckdb_to_pg.py (~10s for typical instance)
                       ├─ verify row counts (~2s)
                       ├─ gzip system.duckdb backup (~5s)
                       ├─ instance.yaml: backend="side_car", url="..."
                       ├─ write job.json: status="success", flip_backend done
                       └─ exit (subprocess no longer needed)
                                                       [timer tick, t=~75s]
                                                       ├─ sees backend != prior known
                                                       ├─ docker compose up -d --force-recreate app scheduler
                                                                                       [app restart ~20s]
                       app boots:
                       ├─ reads instance.yaml
                       ├─ _resolve_url() → postgresql+psycopg://...
                       ├─ /api/health 200 (against side-car PG)
                       └─ on startup, scans db-jobs/ for "success" jobs awaiting
                          confirmation; writes verify_health=true
[poll sees success]
   ←──────────────
[UI: "Migration complete"]
[audit_log row visible]
```

Total wall time: 2–3 minutes for a typical instance (≈10 k rows across 28 tables).

### Data flow — Scenario C: failure at data_copy step

```
subprocess hits psycopg.OperationalError mid-copy:
  ├─ catch exception in db_state_migrator
  ├─ write job.json: status="failed",
  │     failed_at_step="data_copy",
  │     error_class="OperationalError",
  │     error_message="connection terminated unexpectedly"
  ├─ instance.yaml: backend = "duckdb" (revert from side_car_in_progress)
  ├─ leave target.flag as "side-car-enabled" (host can leave postgres up;
  │     retry will reuse)
  ├─ release migration lock
  └─ exit 1

UI polling sees status=failed:
  └─ render error panel: "Migration failed at data_copy: connection
     terminated. Already-copied tables will be skipped on retry."
  └─ [Retry] button → POST /migrate {target:"side_car"} again

Retry subprocess:
  ├─ alembic upgrade head — idempotent no-op
  ├─ migrate_duckdb_to_pg — ON CONFLICT DO NOTHING skips matched rows
  └─ proceeds through remaining steps
```

**Subprocess survival.** The migration subprocess is a child of the app process; if the app container restarts, the subprocess dies with it. Design accommodates this by completing all migration work (steps 1–7) BEFORE the app restart is triggered, and by treating the subsequent app restart + health check as a separate verification phase (step 9 above) executed by the post-restart app itself reading `db-jobs/{job_id}.json`.

**App restart mid-migration** (host reboot or worker crash before the subprocess finishes): on app startup, scan `/data/state/db-jobs/` for `status=running`. For each found job, the stored subprocess PID is checked via `os.kill(pid, 0)` — if signal raises, the process is dead. The job is then marked `failed` with `error="App restart interrupted migration; retry is safe (idempotent script)."`. Admin sees the failed job in UI history and clicks Retry.

## Error handling

Listed in the order they're encountered:

| Error class | Source | Effect |
|---|---|---|
| `ConnectivityError` | Cloud PG validation (sync, pre-job) | 400 before job created; no state change |
| `MigrationInProgressError` | Lock held | 409 with existing `job_id` |
| `InvalidTransitionError` | `cloud → side_car` etc | 400 with allowed transitions |
| `AlembicMigrationError` | Schema step | Job `failed`; revert state; alembic is idempotent so retry is safe |
| `DataCopyError` | Row stream | Job `failed`; idempotent retry skips matched rows |
| `VerifyMismatchError` | Row-count diff | Job `failed` with per-table detail; admin inspects + retries |
| `BackupFailureError` | gzip / pg_dump fail (disk full) | Hard `failed`; no flip; admin frees disk + retries |
| `BackendFlipError` | atomic instance.yaml write fail | Hard `failed`; leave transient state; admin manual recovery |
| `AppRestartError` | compose recreate fails after flip | **Critical** — backend flipped, app down. Alert. Host applier retries every 30 s. |
| `CancelledError` | Admin cancel before flip | Subprocess SIGTERM; revert state; release lock |

## Testing strategy

| Layer | File | Coverage |
|---|---|---|
| Unit — state machine | `tests/test_db_state_machine.py` | Allowed transitions, lock contention, atomic state writes, cache invalidation |
| Unit — migrator | `tests/test_db_state_migrator.py` | Each step in isolation with mocked target; failure-rollback paths |
| Integration — API | `tests/test_api_db_state.py` | Full HTTP cycle via pgserver fixture: POST→poll→success/failed states |
| CLI smoke | `tests/test_cli_db.py` | All four `agnes admin db *` commands; JSON mode; PAT auth |
| E2E — full migration | `tests/db_pg/test_db_state_e2e.py` | Seed DuckDB → run migration to pgserver-backed target → verify 28-table parity → verify factory routes new requests to PG |
| Existing PG suite | `tests/db_pg/*_contract.py` | **Unchanged** — 54 cross-engine tests continue to pass; they validate parity, not state machine |
| Manual smoke (post-merge) | Documented in `docs/postgres-cutover-runbook.md` | agnes-dev: bump infra pin → terraform apply → admin clicks "Migrate to side-car" in UI → verify |

Side-car → cloud full E2E is intentionally **not** automated (requires a real managed PG endpoint; cost-prohibitive in CI). Manual smoke checklist in the runbook covers it.

## Conventions

- All admin actions gated by existing `require_admin` dependency from `app/auth/access.py`.
- Audit log uses existing `audit_repo()` factory. No new audit infrastructure.
- All paths under `/data/state/` survive container recreates (host disk).
- All persisted JSON files use the same schema versioning pattern as existing audit_log: top-level `schema_version: 1` key for forward compatibility.
- File locks use Python's `fcntl.flock(LOCK_EX | LOCK_NB)` for non-blocking acquisition.
- Subprocesses inherit the app's environment + PYTHONPATH; no separate venv.
- All YAML writes use atomic tmp-then-rename pattern (matching existing `instance.yaml` overlay writer in `app/api/admin.py`).
- CLI follows existing patterns in `cli/commands/admin.py`: Click groups, `--json` flag for machine output, `--server-url`/`--token` flags for PAT auth, fallback to `~/.agnes/credentials`.

## Out of scope (deliberate YAGNI)

- **Rollback in UI.** Manual operator path via `pg_dump`/`pg_restore` covers the rare cases; codifying it as a UI button doubles surface area for ~1% of usage.
- **Migration pause/resume.** Restart-friendly retry covers the same need with less code.
- **Multi-database support.** Single backend per instance.
- **Read replicas / connection pooling tuning.** Cloud-specific; out of state machine scope.
- **Cross-region cloud PG validation.** Operator's responsibility; we just open a connection.
- **Job history pruning.** `/data/state/db-jobs/` grows unbounded; pruning is a 1-day follow-up if it ever becomes a problem.

## Implementation scope

Approx 1000 LOC including tests, plan ~1–2 days of focused work. Concrete file/LOC breakdown in Section 3 above.

## References

- PR #388 (Postgres app-state layer): https://github.com/keboola/agnes-the-ai-analyst/pull/388
- PR #454 (re-do with root-cause fix for compose validation): https://github.com/keboola/agnes-the-ai-analyst/pull/454
- Issue #452 (auto-upgrade.service failure tracking): https://github.com/keboola/agnes-the-ai-analyst/issues/452
- Spec for PR #388 PG follow-up: `docs/superpowers/specs/2026-05-27-pg-followup-design.md`
- Operator runbook (will be extended for state machine): `docs/postgres-cutover-runbook.md`
