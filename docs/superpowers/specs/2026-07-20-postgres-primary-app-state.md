# Spec: Postgres as the primary app-state backend

- **Date:** 2026-07-20
- **Status:** Draft — Phase 1 proposed for approval; Phase 2 gated on an open product decision (§9)
- **Scope:** App-state storage backend only. **Explicit non-goal:** the DuckDB *analytics/data engine* is untouched.

---

## 1. Motivation

App-state (users, RBAC, `table_registry`, `sync_state`, jobs, chat sessions, usage
rollups, …) is stored today by default in a single embedded DuckDB file
(`system.duckdb`). On restart-heavy deployments this file has repeatedly hit a
DuckDB **ART-index corruption** class: a `DELETE`/`INSERT OR REPLACE` against a
session/usage table fails with *"Failed to delete all rows from index. Only
deleted N out of M rows"*, which invalidates the whole database connection
(*"database has been invalidated because of a previous fatal error"*) until the
process restarts — and re-poisons on the next write to the offending row. The
only reliable cure is an offline `EXPORT DATABASE` → `IMPORT DATABASE` rebuild.
This has recurred multiple times and is operationally painful: every deploy
restart risks exposing latent on-disk corruption, and recovery is manual.

Postgres app-state removes this failure class entirely: no ART indexes, no
per-connection WAL-replay invalidation, mature online backup/PITR, and a real
concurrent multi-writer story (which the multi-process role split — see the
three-plane work — already assumes).

The platform **already supports** Postgres app-state as a first-class backend
(state machine, per-repo PG siblings, auto-migration, cutover tooling, backup
canary — see §3). What is missing is (a) making PG the *default/primary* path on
server deployments, and (b) a decision on whether to eventually *retire* the
DuckDB app-state backend altogether.

---

## 2. Goals / Non-goals

### Goals
- **G1** — Run a Postgres side-car and make it the primary app-state backend on
  server (VM) deployments, eliminating the DuckDB-app-state corruption class.
- **G2** — Do it with a safe, reversible cutover and a documented runbook.
- **G3** — Decide and document the long-term shape: keep DuckDB app-state as an
  OSS single-VM option, or retire it entirely (Phase 2).
- **G4** — If Phase 2 is chosen: remove the now-dead DuckDB app-state code and
  the redundant half of the cross-engine test matrix, cleanly.

### Non-goals
- **N1 — The DuckDB analytics/data engine stays.** `analytics.duckdb` /
  `server.duckdb`, DuckLake, the `extract.duckdb` contract, the BigQuery/FTS
  extensions, snapshots, and `agnes query` / `agnes pull` local analysis are the
  product and are **out of scope**. "Remove DuckDB" in this spec means *app-state
  only*.
- **N2** — No change to the analytics query surface, connectors, or the
  distribution/manifest flow.
- **N3** — Managed-Postgres (`cloud`) topologies are already supported and are
  not the focus here (the focus is the on-VM `side_car`).

---

## 3. Current state (what already exists)

Accurate as of this date; cite before extending.

- **Backend selection** — `src/repositories/__init__.py::use_pg()` resolves, in
  order: `instance.yaml::database.backend` (via
  `src.db_state_machine.read_backend_state()` → `True` for `side_car`/`cloud`
  and their `_in_progress` variants) then env `DATABASE_URL` / `AGNES_DB_URL`.
  Default with neither = DuckDB.
- **Factory** — a declarative `_REGISTRY` (~70 repo keys → `{duckdb: (mod,cls),
  pg: (mod,cls)}`), `_build(key)` constructs the active backend's class; ~70
  `*_repo()` factory functions are the only sanctioned callsite entrypoint.
  `get_system_db()` hard-raises when `use_pg()` is true.
- **Repo parity** — every active-registry repo has both a DuckDB impl and a
  `*_pg.py` sibling. No repo-level parity gaps. (Chat + secrets DuckDB impls live
  under `app/` rather than `src/repositories/` — a structural asymmetry, not a
  gap.)
- **Schema** — two ladders kept in lockstep: DuckDB `src/db.py`
  (`SCHEMA_VERSION = 94`, 63 `_vN_to_v(N+1)` steps, self-migrates on every
  connect) and Alembic `migrations/` (41 revisions, head `0041_jobs_v94` = DuckDB
  v94). A sync-map rule requires both ladders to reach the same version.
- **PG schema application** — `app/main.py` lifespan calls
  `src.db_pg.ensure_pg_at_head()` when `use_pg()`, which runs `alembic upgrade
  head` **in-process under a PG advisory lock** (replica-safe) when behind, and
  fail-closes when ahead. Env: `AGNES_PG_AUTO_MIGRATE=0`,
  `AGNES_SKIP_PG_REVISION_CHECK=1`. The compose `migrate` one-shot
  (`alembic upgrade head`) is the belt to this suspenders. **This already solves
  the "PG-on-VM doesn't apply Alembic on image upgrade" hazard.**
- **Cutover tooling** — `python -m scripts.migrate_duckdb_to_pg` copies all
  app-state tables (FK-topological, `INSERT … ON CONFLICT DO NOTHING`, JSONB/array
  coercion, NOT-NULL default substitution, a data-loss guard on DuckDB-only
  columns) and validates PK-set + row-count parity (`checksum_match`). Flags:
  `--missing-source-ok` (fresh deploy → exit 0), `--reset-target` (one-time
  cutover truncate; never on the idempotent compose path), `--dry-run`,
  `--only`. Wired as the compose `data-migrate` one-shot (read-only `/data`
  mount), gated behind the `migrate` (schema) one-shot; `app`/`scheduler`
  `depend_on` both `service_completed_successfully`.
- **State machine** — `src/db_state_machine.py::BackendState` (`duckdb`,
  `side_car`, `cloud`, `duckdb_quack` placeholder + `_in_progress` variants),
  multi-destination transitions (incl. `copy_pg_to_duckdb` reverse), overlay at
  `${DATA_DIR}/state/instance.yaml` (atomic write, 0600), `MigrationLock` flock.
  Admin path: `/api/admin/db/migrate` writes overlay + runs
  `scripts/db_state_migrator.py` + restarts.
- **Side-car compose** — `docker-compose.postgres.yml` (`postgres:16-alpine`,
  `POSTGRES_PASSWORD` required no-default, `pg_isready` healthcheck, loopback
  port, `postgres_data` volume bind-mounted to the same `/data` snapshot disk).
  `docker-compose.postgres-host-mount.yml` bridges to a direct host bind for the
  `/data`-host-mount topology.
- **Backup** — `infra/modules/customer-instance/files/agnes-db-backup.{sh,service,timer}`:
  when the `side_car` backend is active, a daily `pg_dump -F c` + a
  `pg_restore` **canary** into a scratch DB (`SELECT count(*) FROM users`),
  alerting on failure, 7-day retention. `cloud` deployments defer to managed
  backups.
- **Tests** — `tests/db_pg/`: 41 `*_contract.py` run twice via the
  `state_backend` fixture (`params=["duckdb","pg"]`, PG param spins a bundled
  `pgserver` + `alembic upgrade head`); 23 endpoint-parity + status-parity
  sweeps (`_parity_sweep_util.py` diffs every parameter-free route's HTTP status
  across backends); the static `tests/test_backend_split_guard.py` ratchet
  (direct-instantiation / `get_system_db()` / raw-state-SQL detectors with
  shrink-only allow-lists).

**Implication:** Phase 1 is largely *configuration + operational*, not new code.
The machinery is built and contract-tested.

---

## 4. Design — Phase 1: Postgres primary on server deployments

Make `side_car` Postgres the app-state backend on VM deployments; keep the DuckDB
app-state backend present (unused) as a fallback. Analytics stays DuckDB.

### 4.1 What changes
- **Compose**: server deployments compose `docker-compose.yml` +
  `docker-compose.prod.yml` + `docker-compose.postgres.yml` (+ the host-mount
  bridge for `/data`-host-mount deployments), so the `postgres` side-car +
  `migrate` + `data-migrate` chain runs and `DATABASE_URL` is injected into
  `app`/`scheduler`.
- **Backend state**: set `instance.yaml::database.backend: side_car` (+
  `database.url`) so `use_pg()` is true and boot routes app-state to PG.
- **Secret**: `POSTGRES_PASSWORD` provisioned from the deployment's secret store
  (no default by design).
- **Cutover**: on first PG boot the `migrate` one-shot applies Alembic head, then
  `data-migrate` copies the existing `system.duckdb` app-state into PG and
  validates parity. For a deployment with existing data this is a one-time
  populated cutover; the compose `data-migrate` is idempotent (`ON CONFLICT DO
  NOTHING`, no `--reset-target`) so it is safe on every subsequent boot.
- **Backup**: the daily backup timer auto-detects the `side_car` backend and
  switches to `pg_dump` + restore-canary (already implemented).

### 4.2 Deploy/cutover runbook (per VM)
1. Snapshot/backup current `system.duckdb` (defense in depth; the migrator opens
   it read-only anyway).
2. Provision `POSTGRES_PASSWORD` in the secret store; render it into the env.
3. Bring up the side-car + run the schema + data cutover with the app **stopped**
   (or accept the compose-ordered one-shots on a controlled restart): the
   `migrate` one-shot (Alembic head) then `data-migrate` (row copy) must both
   exit `success` before `app`/`scheduler` start.
4. Flip `instance.yaml::database.backend: side_car`.
5. Start `app`/`scheduler`; verify `/api/health` `db_schema: ok` at the current
   `SCHEMA_VERSION`, `/readyz` ready, and a representative admin read (users,
   RBAC, table_registry) matches pre-cutover counts.
6. Confirm the backup timer picks up the PG path (a manual canary run).

### 4.3 Rollback
Reversible: the DuckDB `system.duckdb` is untouched (migrator reads it
read-only). To revert, set `database.backend: duckdb` (or clear the overlay +
unset `DATABASE_URL`) and restart. Any app-state written to PG *after* cutover is
not automatically back-ported — so rollback is clean only within the cutover
window; after the instance has run on PG for a while, use
`scripts/db_state_migrator.py::copy_pg_to_duckdb` (the reverse primitive) if a
true rollback is needed. Document this window in the runbook.

### 4.4 Risks & mitigations (Phase 1)
- **Cutover data fidelity** — mitigated by the migrator's PK-set + row-count
  `checksum_match` validation and the DuckDB-only-column data-loss guard. Add a
  post-cutover spot-check of the highest-value tables (users, RBAC grants, group
  members, table_registry, sync_state).
- **Resource** — the side-car adds ~200–400 MB RAM + disk on the `/data`
  snapshot volume. Confirm VM headroom; PG shares the same daily-snapshot disk.
- **Alembic-on-upgrade** — already handled by `ensure_pg_at_head()` (in-process,
  advisory-locked). Verify it fires on an image upgrade on a real VM as part of
  acceptance.
- **`POSTGRES_PASSWORD` absence** — compose fails fast (no default); ensure the
  secret is wired before cutover.
- **Two writers mid-cutover** — the compose ordering (app depends on
  `data-migrate` completed) prevents the app from writing to a half-migrated PG;
  keep the app stopped during a manual cutover.

### 4.5 Acceptance (Phase 1)
- Both target VMs run app-state on `side_car` PG; `system.duckdb` no longer the
  app-state store; the corruption class cannot recur (no DuckDB app-state
  writes).
- Image upgrade applies Alembic head automatically and boots healthy.
- Daily `pg_dump` + restore-canary green.
- No regression in the full `tests/db_pg/` suite (both arms still pass — DuckDB
  arm still exercised in CI even though VMs run PG).

---

## 5. Design — Phase 2 (optional): retire the DuckDB app-state backend

Only if the product decision in §9 is "PG-only" (no OSS single-VM zero-dep
story). This is a large, deliberate code change, separable from and *after* a
stable Phase 1.

### 5.1 What gets removed / simplified
- The DuckDB branch of `_REGISTRY` + `_ARG_PROVIDERS[DUCKDB]`; `_build` collapses
  to PG-only.
- `get_system_db()` and its ~16 grandfathered direct callers (re-route each to a
  `*_repo()` or delete). The `tests/test_backend_split_guard.py` ratchet becomes
  moot and is removed.
- The DuckDB app-state migration ladder in `src/db.py` (the 63 `_vN_to_v(N+1)`
  steps + `SCHEMA_VERSION` + self-migrate-on-connect). Alembic becomes the *only*
  app-state schema ladder. **Caution:** `src/db.py` also hosts analytics/system
  DuckDB helpers (`_open_duckdb`, the analytics attach path); only the
  *app-state* schema/migration parts are removed.
- The DuckDB app-state repos (`src/repositories/X.py` where an `X_pg.py` exists;
  the chat/secrets DuckDB impls under `app/`).
- The state machine's `DUCKDB` / `DUCKDB_QUACK` stable states + the
  `copy_pg_to_duckdb` reverse primitive; a state-rewrite migration for any
  instance whose overlay says `duckdb`.
- The `data-migrate` compose one-shot (once no instance still holds a
  `system.duckdb` needing cutover) — or keep it one release longer as a safety
  net.
- **Tests (the "duplication"):** the `state_backend=["duckdb","pg"]`
  parametrization collapses to PG-only; the 41 contract tests become
  single-backend; the status-parity sweeps + split guard delete. This is the
  bulk of the "remove duplicate tests" the request refers to — but it is only
  safe to delete *after* DuckDB app-state is genuinely gone (those tests are the
  net that caught the backend-split bug class).

### 5.2 Invariants reversed (must be rewritten, not silently broken)
- `CLAUDE.md` §"Dual-backend discipline" — the "both first-class, add the PG
  sibling in the same PR, both ladders reach the same version" rules, and the
  line that *explicitly retires* the "DuckDB only for analytics, Postgres for
  state" framing. Phase 2 re-instates exactly that retired framing and must
  rewrite the section to "Postgres is the app-state backend; DuckDB is the
  analytics engine."
- `CONTRIBUTING.md` sync-map BLOCKING rows for repo-pair parity, symmetric
  factory dispatch, and the two-ladder rule.

### 5.3 Migration & compatibility (Phase 2)
- Any existing instance on `database.backend: duckdb` must cut over to PG
  *before* upgrading to the DuckDB-app-state-removed release, or the release must
  ship a forced-cutover-on-boot (run the migrator, flip state, then boot PG) with
  a clear failure mode if PG isn't configured.
- A hard cutoff release note + a deprecation window (≥1 release where DuckDB
  app-state logs a deprecation warning and the docs push PG) is strongly
  recommended before deletion.

### 5.4 Risk (Phase 2)
- Large blast radius (every repo + `src/db.py` + the whole `tests/db_pg/`
  matrix). Do it as a series of scoped PRs (re-route `get_system_db()` callers →
  freeze the DuckDB ladder → delete DuckDB repos + registry branch → collapse
  tests → rewrite docs), each independently green.
- Irreversibility: once the DuckDB app-state ladder + repos are deleted, there is
  no `copy_pg_to_duckdb`. Accept explicitly.

---

## 6. The pivotal decision (blocks Phase 2, not Phase 1)

**Does the product keep an OSS "single VM, zero external dependencies" option?**

- **Keep it** → Phase 2 should *not* fully delete DuckDB app-state. Instead:
  make PG the recommended/default path for real deployments, keep DuckDB
  app-state as the zero-dep single-VM default. This is *exactly the current
  dual-backend design* — "removal" then means flipping deploy defaults + docs to
  PG, not deleting code. Phase 1 alone satisfies the operational need.
- **Drop it** (Agnes only targets PG-operated deployments) → Phase 2 full removal
  is coherent and the test-matrix simplification is real. Accept the OSS
  regression (every self-hoster must run Postgres).

Phase 1 is valuable and low-risk *regardless* of this answer and should proceed
first.

---

## 7. Rollout plan

1. **Phase 1a — validate on non-prod**: cut a dev VM over to `side_car`, run the
   acceptance checks (§4.5), soak through at least one image upgrade + one daily
   backup/canary cycle.
2. **Phase 1b — prod**: cut prod over during a maintenance window using the
   runbook (§4.2); keep the pre-cutover `system.duckdb` snapshot for the rollback
   window.
3. **Phase 1c — infra defaults**: make the PG side-car + `side_car` backend the
   default rendered compose/overlay for server deployments (infra templates), so
   new instances start on PG.
4. **Decision gate (§6).**
5. **Phase 2 (if chosen)** — the scoped-PR removal series in §5.4, each green,
   with the deprecation window in §5.3.

---

## 8. Testing & verification

- Phase 1 leans on the existing `tests/db_pg/` suite (unchanged) + a real
  end-to-end cutover rehearsal on a copy of production app-state (run the
  migrator against a `system.duckdb` copy, diff counts, boot a PG app against
  it).
- Add a cutover-fidelity test if not covered: for a representative seeded
  `system.duckdb`, assert every app-state table's PK-set + row-count matches
  post-migration (the migrator's own `validate()` extended into a pytest).
- Phase 2 collapses the dual-parametrization; before deleting the DuckDB arm,
  confirm the PG arm alone still covers each contract's assertions.

---

## 9. Open questions

- **Q1 (blocking Phase 2):** keep the OSS single-VM zero-dep story? (§6)
- **Q2:** for existing DuckDB-app-state instances in the wild (if any beyond the
  operator's own), what is the forced-cutover / deprecation-window policy?
- **Q3:** does `cloud` (managed PG) become the recommended prod topology over
  `side_car`, or is `side_car` the default and `cloud` opt-in? (Affects backup
  ownership — self-managed pg_dump vs managed PITR.)
- **Q4:** timing — Phase 1 now; Phase 2 only after N weeks of stable Phase 1?

---

## 10. Summary

Phase 1 (PG primary on VMs) is a low-risk, mostly-operational change that
**permanently removes the DuckDB app-state corruption class** using machinery
that is already built and contract-tested; it is reversible within a cutover
window and should proceed first. Phase 2 (delete DuckDB app-state + collapse the
dual-backend test matrix) is a large, deliberate follow-up that reverses a
documented invariant and hinges on one product decision — whether to keep the OSS
single-VM zero-dependency option. The DuckDB *analytics engine* is out of scope
throughout.
