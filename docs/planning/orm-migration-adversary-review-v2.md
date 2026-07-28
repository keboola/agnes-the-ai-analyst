# Codex adversary review v2 — ORM migration plan

**Date**: 2026-06-04
**Target**: `docs/planning/orm-state-migration.md` (v2, commit ahead of `46943e9b`)
**Previous review**: `docs/planning/orm-migration-adversary-review.md` (v1)

---

## v1 finding tracker

1. **Phase 0 self-blocking lint gate — PARTIAL**
   - what v2 does: replaces fail-whole-repo lint with a baseline allowlist; today's 73 spots snapshotted; cleanup PRs drop entries (`orm-state-migration.md:98-108`).
   - whether that's enough: no. Allowlist key is `path:line`, brittle under unrelated edits. CODEOWNERS does not by itself enforce a shrink-only CI invariant (`orm-state-migration.md:375`).

2. **`duckdb-engine` + `RETURNING` not validated — PARTIAL**
   - what v2 does: moves spike to Phase 2 as a gate, tests `update/insert/delete.returning()` + rowcount semantics (`orm-state-migration.md:144-169`).
   - whether that's enough: better, still incomplete. UPDATE RETURNING fallback admits lost atomicity (`orm-state-migration.md:157`). Spike omits transaction isolation, lock-wait timeout, savepoint behavior, JSON SQL NULL vs JSON `null` (`orm-state-migration.md:152-168`).

3. **Usage telemetry split-brain — PARTIAL**
   - what v2 does: names the split-brain, requires Phase 6 decision before PG-state deploy, recommends moving `usage_*` fully to PG (`orm-state-migration.md:250-275`).
   - whether that's enough: not yet. Phase 6 produces only decision doc + contract-test design, "No code change" (`orm-state-migration.md:275`). Risk register says PG-state must be disabled until Phase 6 ships (`orm-state-migration.md:371`).

4. **Retiring `src/db.py` is operational recovery code — FIXED**
   - what v2 does: narrows Phase 10 to deleting DDL ladder while keeping WAL salvage / pre-migrate snapshots / CHECKPOINT / analytics DB / BQ bootstrap / FTS install (`orm-state-migration.md:336-347`).

5. **Chat migration is not mechanical — PARTIAL**
   - what v2 does: Phase 5 facade redesign with transaction tests before move; `app/chat/persistence.py` becomes a thin facade over four repos (`orm-state-migration.md:203-225`).
   - whether that's enough: only partially. Plan says "transaction-parity assertions" while also recommending intentionally different DuckDB best-effort semantics (`orm-state-migration.md:211-218`).

6. **Knowledge search parity promised before it exists — PARTIAL**
   - what v2 does: Phase 7 cross-dialect knowledge contract with Czech diacritics, BM25-equivalent ranking, fallback parity, count parity, generated `search_vector`, `unaccent`, and `ts_rank_cd` (`orm-state-migration.md:277-300`).
   - whether that's enough: **no**. Proposed PG FTS implementation is technically suspect: `unaccent_simple` is not created by `CREATE EXTENSION unaccent`; `ts_rank_cd` is NOT BM25-equivalent.

7. **Contract-test safety net overstated — PARTIAL**
   - what v2 does: admits knowledge/slack/chat gaps; adds registry coverage gate plus exemptions (`orm-state-migration.md:23,114-119,420-423`).
   - whether that's enough: better but not complete. Gates file/matrix-row presence, not method-level behavioral coverage. Can block split PRs unless temporary exemptions are part of the process.

8. **Model/registry completeness internally inconsistent — PARTIAL**
   - what v2 does: replaces file-count framing with per-table matrix sketch; full matrix lands in Phase 1a (`orm-state-migration.md:405-423`).
   - whether that's enough: partially. Plan still mixes repo keys and table names in Phase 8 (`orm-state-migration.md:317-330`); matrix is only a future deliverable.

9. **Existing migration tooling not proof Phase 11 is easy — PARTIAL**
   - what v2 does: names Phase 11 as migration-tool rework with dry-run, rollback, row-count, checksum, compose-startup, idempotent retry, SIDE_CAR → CLOUD integration (`orm-state-migration.md:351-360`).
   - whether that's enough: not enough. Existing migrator behavior includes cancellation, job liveness, backup hard-fail, PII scrub, timeout masking, streaming PG-to-PG copy, flip locking — none named in Phase 11 test plan.

10. **Lint rule misses non-literal SQL — PARTIAL**
    - what v2 does: call-site detection for any `.execute(...)`, `.exec_driver_sql(...)`, `sa.text(...).execute(...)` outside allowlist (`orm-state-migration.md:102-103`).
    - whether that's enough: closes variable/f-string false-negative, but plan does not describe how AST identifies actual DB execute calls without false positives, nor how wrappers / alternate call shapes are handled (`orm-state-migration.md:443-448`).

11. **DuckDB pooling/concurrency underspecified — PARTIAL**
    - what v2 does: Phase 2 requires engine-factory spec, `NullPool`, concurrent app+scheduler writes, `DATA_DIR` reopen, threadpool lifecycle (`orm-state-migration.md:164-168,310-312`).
    - whether that's enough: better, missing lock-wait timeout, retry/backoff details, savepoints, transaction isolation checks.

12. **Quack timing supports spike-first — FIXED**
    - what v2 does: Phases 0-5 standalone invariant cleanup; Tranche 3 conditional on Phase 2; stop after Tranche 1 if spike fails (`orm-state-migration.md:29-33,90,376`).

13. **Admin usage SQL not cleanly classified — PARTIAL**
    - what v2 does: classifies `app/api/admin_usage.py` as DuckDB-only analytics escape, permanently allowlisted (`orm-state-migration.md:201`).
    - whether that's enough: only partly. Under Phase 6 path b, same surface uses a DuckDB-attached view of PG state (`orm-state-migration.md:272-273`) — boundary is "state queried through DuckDB", not clean analytics.

14. **Registry search pattern brittle — FIXED**

15. **Script deletion evidence narrow — FIXED**

16. **Effort estimate inconsistent — FIXED**

---

## NEW critical issues in v2

### 1. Phase 7 PG FTS design cannot satisfy its own contract

- **finding**: `unaccent_simple` treated as if it exists; `ts_rank_cd` treated as BM25-equivalent.
- **evidence**: v2 requires BM25-equivalent ranking and `to_tsvector('unaccent_simple', ...)` + `ts_rank_cd` (`orm-state-migration.md:290-298`). PostgreSQL docs say `CREATE EXTENSION unaccent` creates a dictionary named `unaccent`, NOT `unaccent_simple`; custom configurations must be created/altered explicitly. `ts_rank_cd` is cover-density ranking — built-in ranking does NOT use global corpus information, which BM25 requires. `pg_search` extension provides BM25 in Postgres natively.
- **impact**: Phase 7 fails before implementation, or worse, passes a weakened test that doesn't preserve DuckDB BM25 behavior.
- **fix**: create an explicit `public.unaccent_simple` text-search configuration, OR drop that name; replace "BM25-equivalent" with a realistic relevance contract, OR evaluate `pg_search` extension with deployment/licensing constraints.

### 2. Phase 11 migration-tool test plan omits existing state-machine safety behavior

- **finding**: v2 names migration-tool rework but tests are narrower than the current migrator's operational contract.
- **evidence**: v2 lists only dry-run, rollback, row-count parity, checksum parity, compose-startup ordering, idempotent retry, SIDE_CAR → CLOUD integration (`orm-state-migration.md:357-360`). Existing `db_state_migrator` has:
  - job JSON + liveness heartbeat (`scripts/db_state_migrator.py:68-127`)
  - cancel sentinels + final cancel-before-flip lock (`scripts/db_state_migrator.py:198-234,1179-1208`)
  - URL password masking for Alembic timeout errors (`scripts/db_state_migrator.py:249-265`)
  - audit PII scrubbing before copy (`scripts/db_state_migrator.py:361-388,488-535`)
  - backup hard-fail + half-written artifact cleanup (`scripts/db_state_migrator.py:914-997`)
  - streaming chunked PG-to-PG copy (`scripts/db_state_migrator.py:573-721`)
  - halt-on-first-failure semantics (`scripts/migrate_duckdb_to_pg/__init__.py:194-285`)
- **impact**: rework can pass v2's tests while regressing operator-visible safety + rollback behavior.
- **fix**: Phase 11 must inventory and preserve/test each current state-machine behavior, not just row movement.

### 3. Phase 6 path b relies on DuckDB-attached PG export not specified enough to gate

- **finding**: concept works in documented form, but v2's exact syntax + streaming assumptions not proven.
- **evidence**: v2 proposes `ATTACH 'postgres:...' AS pg` + `COPY (SELECT … FROM pg.usage_events …) TO 'file.parquet'` (`orm-state-migration.md:269-273`). DuckDB docs require `ATTACH ... (TYPE postgres)` or documented libpq/URI connection string; `COPY (SELECT...) TO` is supported but Postgres-extension scan behavior under filtered admin queries isn't memory-bounded by default.
- **impact**: recommended path b can move usage to PG but leave admin export / LLM usage fragile, slow, or memory-heavy under production filters.
- **fix**: use documented `ATTACH '<libpq-or-postgresql-uri>' AS pg (TYPE postgres, READ_ONLY)` syntax; add Phase 6/2 load test for filtered + unfiltered `usage_events` export; require EXPLAIN/profile evidence for bounded memory.

---

## NEW high/medium issues in v2

### 4. Shrink-only lint allowlist not implementable as written

- **finding**: `path:line` entries make unrelated edits look like allowlist growth/shrink; CODEOWNERS is review policy, not a CI invariant.
- **evidence**: v2 defines entries as `path:line reason cluster`, says allowlist only shrinks (`orm-state-migration.md:104-107`), relies on CODEOWNERS + shrink-only CI (`orm-state-migration.md:375`).
- **impact**: Tranche 1 becomes a permanent noisy baseline instead of a sustainable invariant.
- **fix**: store stable callsite IDs — path + qualified function/class + AST node hash + normalized callee. Generate snapshot. CI compares discovered violations to the committed baseline.

### 5. Phase 2 spike lacks transaction + JSON edge-case checkboxes

- **finding**: spike covers common ORM features but omits isolation level, lock-wait timeout, savepoints/nested transactions, SQL NULL vs JSON `null`.
- **evidence**: pass/fail criteria at `orm-state-migration.md:152-168`; none of these appear.
- **impact**: Tranche 3 starts with green CRUD tests but fails under real FastAPI/scheduler contention or JSON filter semantics.
- **fix**: add binary pass/fail rows for isolation, lock timeout, savepoints, nested rollback, JSON SQL NULL vs JSON `null`, JSON path/index behavior, retry policy.

### 6. Chat "contract" tests cannot be both parity tests and divergence tests

- **finding**: v2 says contract tests parametrize over both backends with transaction-parity assertions, then recommends preserving intentionally different DuckDB best-effort semantics.
- **evidence**: `test_chat_contract.py` required to assert PG atomic-fork AND DuckDB no-multi-transaction behavior (`orm-state-migration.md:211-218`).
- **impact**: single parametrized contract hides divergence behind backend branches — that's documentation, not parity.
- **fix**: split tests into common behavioral invariants AND backend-specific semantic tests; mark divergent behavior as explicit public contract.

### 7. Coverage matrix gate blocks healthy split PRs

- **finding**: adding a registry key/model in one PR + its contract in a follow-up will fail unless exemption is added first.
- **evidence**: Phase 1a requires every registry key to have named contract or exemption (`orm-state-migration.md:114-118`); matrix gate fails on missing rows/tests/models (`orm-state-migration.md:420-423`).
- **impact**: teams either bundle oversized PRs or add churny temporary exemptions.
- **fix**: require repo+contract in same PR for production registry entries, OR define short-lived exemption protocol with expiry + CI failure after expiry.

### 8. Phase 8 cluster list not normalized to registry keys; has duplicate/misordered identities

- **finding**: cluster list mixes table names, repo names, registry keys; some duplicated under different names; FK dependencies misordered.
- **evidence**:
  - `access_tokens` in cluster 1 (`orm-state-migration.md:317`); `personal_access_tokens` in cluster 9 (`orm-state-migration.md:325`). Registry key is `access_token`; repo operates on `personal_access_tokens` (`src/repositories/__init__.py:236-242`, `src/models/config.py:75-97`).
  - `profiles` in cluster 1; `table_profiles` in cluster 9. Registry key `profile` owns `table_profiles` (`src/repositories/__init__.py:240-242`, `src/models/misc.py:27-36`).
  - `resource_grants` in cluster 2 (`orm-state-migration.md:317-318`) but has FKs to `table_registry`, `data_packages`, `memory_domains`, `knowledge_items`, `recipes` — all in LATER clusters (`src/models/rbac.py:173-195`).
- **impact**: Phase 8 PR boundaries can delete the wrong DuckDB DDL step or consolidate a repo before its dependent resource-table contracts exist.
- **fix**: define clusters by registry key, owned table set, FK dependencies, migration function name. Topologically sort OR document why dependency order is irrelevant.

### 9. Hidden assumption: "analytics escape" remains true after PG-via-DuckDB-attach

- **finding**: v2 assumes admin usage is analytical because DuckDB executes it, even when data source is PG state.
- **evidence**: admin usage permanently DuckDB-pinned + allowlisted (`orm-state-migration.md:201`); Phase 6 path b says it uses DuckDB-attached view of PG (`orm-state-migration.md:272-273`).
- **impact**: raw SQL over state can survive forever by being routed through DuckDB.
- **fix**: define invariant by data domain, not engine — state tables queried through DuckDB are still state SQL, need named exception with security/performance tests.

### 10. Hidden assumption: "contract test exists" = repo behavior is covered

- **finding**: matrix checks file existence, not method coverage or semantic assertions.
- **evidence**: coverage gate fails if row/test/model missing (`orm-state-migration.md:420-423`); says every `_REGISTRY` key has contract test at end of Tranche 1 (`orm-state-migration.md:239-241`).
- **impact**: shallow contract file can satisfy gate while untested methods diverge.
- **fix**: generate repo-method-to-test matrix, OR require each contract to enumerate method names/assertions.

### 11. Tranche-1-only worst case: lint baseline isn't sustainable permanently

- **finding**: v2 says if spike fails, stop at Tranche 1 + invariant still holds (`orm-state-migration.md:90,431-432`). But permanent state still depends on path-line allowlists + permanent analytics exceptions (`orm-state-migration.md:104-107,201,239-241`).
- **impact**: project ends with frozen but noisy lint baseline that operators learn to bypass.
- **fix**: after Phase 5, regenerate baseline to contain only durable analytics exceptions, forbid `path:line` identifiers, require reason owners/expiry for state exceptions, periodic CI report of remaining raw SQL escapes.

---

## FINAL VERDICT

**Top 3 things still wrong with v2**:

1. Phase 7 FTS is not implementable as written — `unaccent_simple` is undefined and `ts_rank_cd` is not BM25-equivalent.
2. Phase 11 under-tests existing migration state machine — regresses cancellation, backups, liveness, PII scrub, flip safety.
3. Tranche 1's permanent safety story depends on brittle path-line allowlist + file-existence coverage gates.

**Recommended next step**: revise v2 before implementation. Patch Phase 0 allowlist identity, Phase 2 spike checklist, Phase 6 export proof, Phase 7 FTS design, Phase 8 cluster matrix, Phase 11 migration safety inventory.
