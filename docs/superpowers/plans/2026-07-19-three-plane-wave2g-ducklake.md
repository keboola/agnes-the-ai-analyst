# Three-Plane Wave 2-G — DuckLake Data Plane (WS E)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** `analytics.backend: ducklake` — the server-side query surface moves from the rebuilt-and-swapped `server.duckdb` file to a DuckLake catalog (PG on multi-process, DuckDB-file catalog single-process) with data files owned by DuckLake. The worker is the only writer (copy-ingest after each sync); api replicas hold a long-lived read attach with snapshot isolation. The extracts tree keeps its current contract untouched (distribution artifact + rollback truth). `legacy` backend remains the default — DuckLake is opt-in this wave.

**Architecture (POC-verified facts from spec §3.4 — build on them, don't re-litigate):** DuckLake v1.0, extension in DuckDB ≥1.5.2. Copy-ingest ONLY (extractors rewrite parquets in place; never `ducklake_add_data_files` on mutable paths). One PG connection per ATTACH, no extra per query. Persisted DuckLake views over a foreign attached alias are late-bound (works for BQ remote tables if the session attaches the extension first). Maintenance: `CALL <cat>.merge_adjacent_files()` → `CALL ducklake_expire_snapshots('<cat>', older_than => …)` → `CALL ducklake_cleanup_old_files('<cat>', cleanup_all => true)`, in that order, plus catalog VACUUM; ~2× disk between runs. Concurrent writers on different tables safe (snapshot-conflict retry). DuckDB-file catalog is hard single-process. ENCRYPTED off. Multi-process ⇒ PG catalog (SQLite rejected by POC).

**Tech Stack:** DuckDB ducklake extension, Postgres catalog, existing orchestrator/repo seams.

## Global Constraints

- `analytics.backend: legacy` stays the DEFAULT; every existing test passes unchanged with zero config. DuckLake activates only via config; the m-tier profile flips it to exercise it.
- The extract.duckdb connector contract is UNTOUCHED (no connector changes). The manifest/`agnes pull` per-table-parquet contract rides the extracts tree exactly as today.
- Startup guard: `analytics.backend=ducklake` + multi-process ⇒ PG catalog required (extend `validate_deployment`); DuckDB-file catalog allowed only in single-process `all` mode.
- Dual-backend discipline for any new repo/table; playbooks binding (`repo-parity.md`, `migration.md` — re-check SCHEMA_VERSION/alembic numbering at implementation time, main moves).
- Full suite before push; CHANGELOG in the final task; vendor-agnostic.

---

### Task 1: AnalyticsBackend seam + config + guard

**Files:** Create `src/analytics_backend.py` (or extend orchestrator module — judge; keep the seam explicit). Modify `config/loader.py`/`app/instance_config.py` accessor (`analytics.backend`, `ducklake.catalog_dsn`, `ducklake.data_path`), `app/startup_guards.py`. Tests.

Produce: `analytics_backend() -> "legacy" | "ducklake"` resolution (env `AGNES_ANALYTICS_BACKEND` > instance.yaml > "legacy"); `ducklake_catalog_dsn()` (default: single-process → `{DATA_DIR}/analytics/catalog.ducklake` file; multi-process → REQUIRED explicit PG DSN — reuse the app-state PG connection parameters with a separate database/schema by default, judge the cleanest: same PG instance, dedicated `ducklake_catalog` schema per POC/definite-blog guidance); guard: ducklake+multi-process without PG DSN ⇒ DeploymentConfigError. Tests: resolution matrix, guard cases, legacy default untouched.

- [ ] Commit `feat(ducklake): analytics backend seam, config and startup guard`

### Task 2: DuckLake session management (attach/read/write handles)

**Files:** Create `src/ducklake_session.py`. Tests (real ducklake extension against a DuckDB-file catalog + PG catalog via the db_pg pgserver fixture).

Produce: `open_ducklake_read() -> conn` (long-lived per-process attach for api-role readers: INSTALL/LOAD ducklake (+postgres when PG DSN), `ATTACH 'ducklake:...' AS lake (DATA_PATH ...)`, memory caps + threads consistent with `_apply_memory_caps`); `open_ducklake_write()` (worker's writer session); singleton management mirroring `get_analytics_db()` (module singleton + lock, `close_…` for lifecycle); remote-extension re-attach hook reusing the existing `_reattach_remote_extensions` seam so `_remote_attach` rows still work in DuckLake sessions (registry-driven — read `_remote_attach` info from the control plane copy the orchestrator already maintains, or from extract.duckdb as today — judge minimal correct). Contract tests: attach both catalogs; write in one session visible to a reader snapshot after commit; PG catalog = exactly 1 connection per attach (assert via pg_stat_activity through the fixture).

- [ ] Commit `feat(ducklake): session management for readers and writer`

### Task 3: Copy-ingest writer path in the orchestrator

**Files:** Modify `src/orchestrator.py` (backend dispatch in `rebuild()`/`rebuild_source()`), new `_do_rebuild_ducklake()`. Tests.

Contract: when backend=ducklake, a "rebuild" for a source = for each table in the source's extract (same `_meta` iteration as legacy): `CREATE OR REPLACE TABLE lake."<source>"."<table>" AS SELECT * FROM read_parquet('<extract parquet>')` (per-source schemas in the catalog — the view-ownership namespace strategy from spec §3.4); master views: maintain the same top-level view names the legacy rebuild exposes (`CREATE OR REPLACE VIEW lake.main."<view>" AS SELECT * FROM lake."<source>"."<table>"`), porting the view-ownership claim/reconcile logic (grep `view_ownership_repo` usage in the legacy rebuild — reuse the same repo, same collision semantics). Remote-mode tables (`query_mode='remote'`): expose as session views resolved from `table_registry` at read time (task 2's hook), NOT ducklake-persisted (keep it simple; POC says persisted views work, but session views from registry avoid catalog/BQ coupling — document choice). `rebuild_lease` (from W2B-4 fix) serializes cross-process exactly as legacy. sync_state hash/manifest updates: UNCHANGED (they describe the extracts artifacts). Incremental: per-source rebuild only re-ingests that source's tables (fixes the legacy full-rebuild-on-webhook pain by construction — note it).

- [ ] Tests: rebuild with 2 sources → tables + master views queryable via read session; jira-style single-source rebuild touches only that source's tables (other source's snapshot untouched — assert via snapshot id or table data); view-name collision honors ownership; legacy backend paths byte-identical (run the existing orchestrator tests under legacy).
- [ ] Commit `feat(ducklake): copy-ingest rebuild path`

### Task 4: Reader path — query endpoints ride DuckLake

**Files:** Modify `src/db.py` `get_analytics_db_readonly()` (backend dispatch: ducklake → a read session from task 2 instead of open-file+re-ATTACH loop; keep the per-request RO semantics — judge whether per-request attach (POC: cheap? one PG conn per attach — per-request would churn conns; PREFER the long-lived shared read attach + cursor per request, mirroring `get_analytics_db()`'s cursor pattern) — document the choice), `app/api/query.py` internal-table short-circuit unchanged, `app/api/query_hybrid.py` (temp registration against the ducklake session — contract test per spec §3.4). Tests.

- [ ] Tests: `/api/query` over ducklake tables (local mode) returns identical results to legacy for the same extracts; hybrid query joins BQ-sub-result against a ducklake view; internal tables unaffected; concurrent readers see consistent snapshots during a concurrent rebuild (the POC-verified property — assert reader mid-transaction sees old data while writer commits new).
- [ ] Commit `feat(ducklake): reader path for query endpoints`

### Task 5: Maintenance jobs + startup wiring + m-tier flip

**Files:** `app/worker/kinds.py` (+registry): `ducklake-maintenance` LIGHT kind running the POC-verified sequence + catalog VACUUM (PG) — scheduled via a new scheduler row (enqueue, follow W2B-6's pattern) daily; `app/main.py` lifespan: role-gated ducklake session init (readers on api/gateway… only api needs it — judge; worker opens writer lazily); `config/instance.mtier.yaml` + `docker-compose.mtier.yml`: flip m-tier to `analytics.backend: ducklake` with PG catalog (the side-car PG) so the smoke exercises it; smoke: add a ducklake assertion (a query over the lake works through the LB after a sync). Docker-gated — static-validate if daemon down. Tests for the maintenance kind (mock the session, assert call order compact→expire→cleanup).

- [ ] Commit `feat(ducklake): maintenance job and m-tier wiring`

### Task 6: Migration flip + rollback + docs + CHANGELOG + full suite

**Files:** `cli/commands/admin.py` (or admin API): `agnes admin analytics migrate --to ducklake|legacy` — for existing instances: to-ducklake = enqueue a full rebuild under the new backend (extracts are on disk — no re-extract; the rebuild IS the migration) + flip config guidance (config is operator-owned; the command validates prerequisites (PG catalog reachable, extension loadable) and prints the exact instance.yaml change + triggers the rebuild after the operator flips — or takes an explicit `--i-flipped-config` flow; judge operator ergonomics, document); to-legacy rollback = flip back + legacy rebuild from extracts (materialized outputs re-materialize on schedule — spec-stated, restate). REST+CLI+MCP coverage per ratchet for any new endpoint. docs/architecture.md (data plane section update), DEPLOYMENT.md (ducklake config, PG catalog sizing note: N api + M workers connections), jobs-classification.md row, CHANGELOG consolidated bullet. Full suite + ratchet gates.

- [ ] Commit `feat(ducklake): migration command, docs (wave 2G)`

## Self-review notes

Deferred (say so): signed-URL/bucket mirroring (WS F — next wave, builds on the extracts tree unchanged by this wave); data inlining for Jira (optional, post-type-matrix); DuckLake ENCRYPTED (pinned off); dropping the legacy backend (long-term). The legacy `server.duckdb` remains fully supported — this wave adds, doesn't remove.
