# ORM-on-state migration plan — v3

**Date**: 2026-06-05
**Branch**: `vr/orm-migration-plan` (off `origin/main` @ `46943e9b`)
**Status**: research + plan, no code change
**Supersedes**: v2 (in this branch). Restructured per `docs/planning/orm-migration-adversary-review-v2.md`.

## Goal

Eliminate raw SQL from non-repo code paths. Single SQLAlchemy ORM layer for application state (today: 37 DuckDB repos + 41 Postgres mirrors via a declarative `_REGISTRY` factory at `src/repositories/__init__.py`). Same ORM models run on DuckDB or Postgres via the dialect layer (`duckdb-engine` for DuckDB, `psycopg` for PG) — **iff** the Phase 2 spike clears. DuckDB stays for analytics: `analytics.duckdb`, `extract.duckdb`, BQ extension, parquet views, FTS extension.

## TL;DR

User invariant: **"there should be no raw SQL hardcoded except in the analytical part"**.

**Verdict**: REFUTED today; achievable as a sequenced cleanup, gated by a real spike, with no permanent lint-baseline debt.

- 73 raw-SQL spots outside `src/repositories/` (65 bug-class, 17 acceptable, 1 needs-discussion → resolved as DuckDB-only analytics escape).
- 2 misplaced repository-pattern files (`app/chat/persistence.py`, `app/secrets_vault.py`).
- Today: 62 mapped tables across 15 `src/models/*.py` files; 45 registry keys in `_REGISTRY`; 15 cross-engine contract tests (14 parametrize both backends, 1 via shared `state_backend` fixture). Knowledge, slack-binding, and chat clusters have NO contract test today.

**Single cost estimate**: ~10–14 engineer-weeks (revised from v2's 9–13 to account for the spike + Phase 7 + Phase 11 expansions identified by adversary v2):

| Tranche | Phases | Weeks | Standalone value |
|---|---|---:|---|
| **Invariant cleanup** | 0–5 | 5–6 | Ships the 65 bug-class spots into repos. Lint baseline locks future drift. **Delivers the invariant even if everything below stalls.** |
| **Spike + boundary resolution** | 2 (parallel) + 6–7 | 2–3 | Validates `duckdb-engine` for SA on DuckDB. Resolves usage / chat / knowledge boundary designs as named contract tests. |
| **ORM consolidation** | 8–11 | 3–5 | Single ORM. Only runs if Phase 2 spike clears AND Phase 6/7 decisions ship. |

The invariant ships first. The unified ORM is a *follow-on bet* gated by Phase 2 + Phase 6/7.

---

## What changed vs. v2 (adversary v2 fixes)

| v2 adversary finding | v3 fix |
|---|---|
| **NEW-CRIT-1** Phase 7 FTS unimplementable (`unaccent_simple` doesn't exist; `ts_rank_cd` ≠ BM25) | **Phase 7 redesigned.** Default path: `tsvector + GIN` with an explicit custom text-search config `public.cs_unaccent` (real DDL: `CREATE TEXT SEARCH CONFIGURATION public.cs_unaccent (COPY = pg_catalog.simple); ALTER TEXT SEARCH CONFIGURATION public.cs_unaccent ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple;`). Drop "BM25-equivalent" — replace with explicit "lexical match parity + top-K-overlap relevance" contract. Optional: evaluate `pg_search` extension as 7b if user feedback demands BM25-grade ranking. |
| **NEW-CRIT-2** Phase 11 omits existing migrator safety (cancel/liveness/PII/backup/streaming/flip-lock) | **Phase 11 prefaced by an inventory pass.** Each existing behavior (`scripts/db_state_migrator.py`: job JSON + heartbeat at 68-127; cancel sentinels at 198-234 + flip-lock at 1179-1208; URL password masking at 249-265; audit PII scrub at 361-388 + 488-535; backup hard-fail at 914-997; streaming chunked copy at 573-721; halt-on-first-failure at `scripts/migrate_duckdb_to_pg/__init__.py:194-285`) gets a named regression test in Phase 11. Rework PRs may not delete these behaviors; they may change implementation only if the named test stays green. |
| **NEW-CRIT-3** Phase 6 ATTACH-postgres + COPY TO PARQUET under-specified | **Phase 6 specifies syntax + memory-bound test.** Documented form: `ATTACH '<libpq-uri-or-conninfo>' AS pg (TYPE postgres, READ_ONLY)`. Phase 6 ships an integration test running both filtered (`event_date >= ?`) AND unfiltered exports against a 30-day usage_events fixture; asserts peak RSS stays under a configured cap (default 512 MiB) and EXPLAIN shows pushdown. Two-step fallback if ATTACH path fails: SA Core paginated SELECT → pyarrow `RecordBatchFileWriter` to parquet. |
| **NEW-HIGH-4** Shrink-only `path:line` allowlist brittle; CODEOWNERS is review, not CI | **Phase 0 stable-callsite IDs.** Allowlist entries keyed by `<sha256-12-of(repo_path)>:<qualified_callsite>:<ast_node_hash>` (not file:line). `qualified_callsite` = `<module>.<class>.<function>#<lexical-index>`. Lint tool generates the snapshot once; CI compares discovered IDs to the committed snapshot — failures: new IDs not in snapshot OR snapshot IDs not discovered (snapshot must shrink). CODEOWNERS keeps the file requiring approval but CI enforces the shrink-only invariant mechanically. |
| **NEW-HIGH-5** Phase 2 spike missing isolation / lock-wait / savepoints / JSON NULL | **Phase 2 checklist expanded** with: transaction isolation level (READ COMMITTED), lock-wait timeout / statement_timeout, savepoints / nested transactions, SQL NULL vs JSON `null`, JSON path/index operator behavior, retry/backoff policy under transient write conflicts. Each gets a binary pass/fail row. |
| **NEW-HIGH-6** Chat contract test circular (parity + intentional divergence) | **Phase 5 splits the tests.** Two new files: `tests/db_pg/test_chat_invariants_contract.py` (parametrized over both backends, common invariants: schema, indexes, CRUD round-trip, FK enforcement) and `tests/test_chat_transaction_semantics.py` (backend-specific behavior, **not** parametrized: PG atomic-fork test + DuckDB best-effort test, each pinning the divergent semantic as a public contract). |
| **NEW-HIGH-7** Coverage matrix gate blocks split PRs | **Phase 1a defines exemption protocol.** Registry key without contract test fails CI unless `tools/lint/contract_exemptions.yaml` lists it with `expiry: YYYY-MM-DD` (max 30 days) and `owner: <gh-handle>`. CI fails after expiry. Bundling repo+contract in one PR is the preferred path; exemption is for legitimate sequencing. |
| **NEW-HIGH-8** Phase 8 cluster list not normalized; FK ordering wrong | **Phase 8 cluster matrix table** below replaces the freeform list. Each row: registry_key, owned tables, FK targets, depends-on-clusters, migration_function_name (`_v(N)_to_v(N+1)`), contract test, models. FK-topologically sorted: `resource_grants` moves from cluster 2 to last position before sweep. Tables with FKs across multiple clusters get an Alembic migration that defers FK constraint creation to after both referenced clusters consolidate. |
| **NEW-HIGH-9** "Analytics escape" survives PG-via-DuckDB-attach | **Invariant rewrite (Phase 0).** Allowlist reason taxonomy: `analytics-domain` (table NEVER lives in state DB — parquet, BQ, FTS index, server.duckdb views), `state-via-duckdb-attach` (table lives in PG but read via DuckDB ATTACH for export/LLM), `liveness` (`SELECT 1`-class), `dialect-bootstrap` (extension `INSTALL`/`LOAD`). The `state-via-duckdb-attach` category requires: explicit security review (no privilege escalation through DuckDB), performance bound test, and re-classification trigger if the underlying table ever moves back to DuckDB. |
| **NEW-MED-10** "Contract test exists" ≠ behavior covered | **Phase 1a coverage matrix includes method-list.** Each contract test enumerates the repo methods it covers; CI matches enumeration against repo's public methods. New repo method without a contract assertion fails the coverage gate. |
| **NEW-MED-11** Tranche-1-only permanent state isn't sustainable | **Phase 5 ends with a baseline cleanup PR.** Regenerates the allowlist to contain ONLY `analytics-domain` + `dialect-bootstrap` + `liveness` entries. `state-via-duckdb-attach` entries must have an owner + expiry (max 90 days; expiry forces re-evaluation). No path-or-AST-identified-state entries survive. Periodic CI report (monthly) lists remaining raw SQL escapes by reason. |

---

## Architecture target

Same as v2:

```
┌─ Application state ────────────────────────────────────────┐
│ Single SQLAlchemy declarative model layer (src/models/*)   │
│ Single Alembic ladder (migrations/versions/)               │
│                                                            │
│  DATABASE_URL=duckdb:///./data/state/system.duckdb         │
│    → duckdb-engine SA dialect → DuckDB                     │
│                                                            │
│  DATABASE_URL=postgresql+psycopg://...                     │
│    → psycopg SA dialect → Postgres                         │
│                                                            │
│  DATABASE_URL=duckdb+quack:///... (DuckDB v2.0, fall 2026) │
└────────────────────────────────────────────────────────────┘
┌─ Analytics ────────────────────────────────────────────────┐
│ Raw DuckDB python client (unchanged)                       │
│   analytics.duckdb, ATTACH extract.duckdb,                 │
│   BQ extension, Keboola extension, FTS extension,          │
│   read_parquet views, COPY TO PARQUET                      │
└─ DuckDB-state lifecycle helpers (kept, NOT in scope) ──────┐
│ src/db.py — WAL salvage, pre-migrate snapshot,             │
│ CHECKPOINT, system.duckdb open/close, schema_version       │
│ backfills (only when DATABASE_URL points at duckdb://)     │
└────────────────────────────────────────────────────────────┘
```

If spike fails: stop at end of Tranche 1. Invariant still holds; dual-repo continues; revisit post-DuckDB-Quack-v2.0.

---

## Tranche 1 — Invariant cleanup (Phases 0–5)

**Goal**: bring the 65 bug-class spots into repos; lock future drift via stable-callsite-ID baseline; end Tranche with a sustainable allowlist taxonomy.

### Phase 0 — Stable-callsite-ID lint + reason taxonomy (small PR)

**Adversary v2 fix NEW-HIGH-4 + NEW-HIGH-9 + NEW-MED-11.**

- New tool `tools/lint/sql_check.py`:
  - AST visitor identifies every call site matching the DBAPI/SA execute shape: `<obj>.execute(<args>)`, `<obj>.exec_driver_sql(<args>)`, `<obj>.executemany(<args>)`, `sa.text(<args>).execute(<args>)`, `sa.text(<args>)` in any expression chained to `.execute`, `Session.execute(<args>)`.
  - For each match, computes a stable callsite ID: `<sha256-12 of repo-relative-path>:<qualified-callsite>:<ast-node-hash>` where:
    - `qualified-callsite` = `<module-dot-path>.<class-name or "">.<function-name>#<lexical-index-within-function>`
    - `ast-node-hash` = stable hash of the parent function's AST normalized to ignore comments/whitespace (covers code-shape changes; insensitive to unrelated formatting)
  - Output: `tools/lint/sql_allowlist.yaml` (one entry per ID with reason + owner).
- Reason taxonomy:
  - `analytics-domain` — table never lives in state DB. Permanent. Example: `read_parquet`, BQ TVF, FTS extension, analytics.duckdb views.
  - `dialect-bootstrap` — extension `INSTALL`/`LOAD`, `SET GLOBAL TimeZone='UTC'`, `INSTALL bigquery`. Permanent.
  - `liveness` — `SELECT 1`-class. Permanent.
  - `state-via-duckdb-attach` — state table read via DuckDB ATTACH for export/LLM (admin LLM SQL, usage_* export). **Requires owner + 90-day expiry + security/performance review.**
- CI invariant: discovered IDs **must equal** snapshot IDs. New IDs not in snapshot → CI fail. Snapshot IDs not discovered → CI fail (snapshot is shrink-only; removing requires the cleanup PR landing in the same diff).
- Snapshot lives at `tools/lint/sql_allowlist.yaml`; CODEOWNERS requires review on changes but CI enforces shape.
- Initial snapshot: 73 spots, classified per the audit in `docs/planning/agnes-orm-rawsql-audit.md`.

**Outcome**: future drift impossible; existing offenders explicit and tagged for resolution in subsequent phases.

### Phase 1 — Coverage matrix + quick wins

**Adversary v2 fix NEW-HIGH-7 + NEW-MED-10.**

**1a. Coverage matrix gate.**

- New `tools/lint/registry_coverage.py`:
  - For each `_REGISTRY` key: must have either (a) a `tests/db_pg/test_<key>_contract.py` parametrized over both backends, OR (b) a `tools/lint/contract_exemptions.yaml` entry with `owner: <handle>` + `expiry: YYYY-MM-DD` (max 30 days from creation, CI fails after expiry).
  - For each contract test: must enumerate covered repo methods in a `COVERED_METHODS = [...]` module-level constant. CI matches against the repo class's public methods (`dir()` filtered to non-`_` prefix); missing methods → CI fail.
- Initial exemptions: `knowledge` (until Phase 7), `chat_sessions` / `chat_messages` / `chat_session_participants` / `user_workdirs` (until Phase 5), `slack_bindings` (until Phase 3, doesn't exist yet).

**1b. Quick-win PRs** (~30 hits). Same content as v2, abbreviated here. Each PR drops the corresponding allowlist entry by its stable ID.

| File | Repo method to add | Hits |
|---|---|---:|
| `app/chat/audit.py` | `audit_repo().log()` (exists) | 1 |
| `app/chat/copresence_summary.py` | `chat_session_repo().title_for()` | 1 |
| `app/api/chat_copresence.py` | `users_repo().get_by_email()` (exists) | 1 |
| `app/api/sync.py` | `users_repo().mark_last_pull()` | 1 |
| `app/api/me_debug.py` | `user_group_members_repo().google_sync_summary()` | 1 |
| `app/api/store.py` | `store_entities_repo().synthetic_name_collision()` + `.revert_archive()` | 2 |
| `app/api/my_stack.py` | existing repos | 2 |
| `app/api/admin_user_sessions.py` | usage_session_summary, users, audit repo methods | 3 |
| `src/store_guardrails/purge.py` | `store_submissions_repo().purgeable()` | 1 |
| `src/store_guardrails/reaper.py` | `store_submissions_repo().reap_stuck_reviews()` + `.mark_review_error()` | 2 |
| `src/claude_md.py` | `table_registry_repo`, `metric_definitions_repo`, `marketplace_registry_repo` methods | 3 |
| `src/rbac.py` | `resource_grants_repo().has_grant_for_user()` | 2 |
| `connectors/internal/registry.py` | `table_registry_repo().prune_internal_except()` | 1 |
| `services/slack_bot/events.py` | `users_repo().get_by_slack_id()` | 1 |
| `services/session_pipeline/runner.py` | `users_repo().get()` (exists) | 1 |
| `services/verification_detector/__main__.py` | `session_processor_state_repo().delete()` | 1 |
| `app/web/router.py` | ~12 small COUNT / SELECT 1 helpers → per-cluster splits | ~12 |

### Phase 2 — `duckdb-engine` spike (parallel with Phase 1)

**Adversary v2 fix NEW-HIGH-5.**

**Phase 2 is THE GATE.** Tranche 3 (Phases 8–11) cannot start until Phase 2 ships a green spike report.

**Time-box**: 10 working days (was 8d in v2; added items below).

**Pass/fail criteria** (every row a binary):

| Capability | Test | Pass = |
|---|---|---|
| Alembic upgrade head on `duckdb:///...` URL | `alembic upgrade head` against fresh DuckDB | ladder lands; schema matches `Base.metadata.create_all()` |
| `update(...).returning(...)` for CAS | `Session.execute(update(Tok).where(...).returning(Tok.id))` | Single row returned iff WHERE matched; works on both backends |
| `insert(...).returning(...)` | Same on `insert()` | Works on both |
| `delete(...).returning(...)` | Same on `delete()` | Works on both |
| JSONB on PG ↔ JSON on DuckDB | Roundtrip `dict[str, Any]` through model with `JSON` column | Survives `commit()` + `refresh()` on both |
| SQL NULL vs JSON `null` | Insert `None` (SQL NULL) and `json.dumps(None)` (JSON `null`); query for each | Distinguishable on both backends |
| JSON path/index operator | `model.col["nested"][0]` filter via SA | Returns expected rows on both |
| Computed column for FTS | PG `GENERATED ALWAYS AS (...) STORED` + DuckDB equivalent | Supported on both OR documented escape (Phase 7) |
| FK enforcement | DELETE parent with child row | Raises on both |
| `RETURNING id` rowcount on empty match | UPDATE matching 0 rows | Returns empty result, not 1-row-with-None |
| Transaction isolation level | `READ COMMITTED` setter | Honored on both; documented if dialect differs |
| Lock-wait timeout / statement_timeout | Concurrent write contention | Times out cleanly (no hang); configurable per session |
| Savepoints / nested transactions | `with session.begin_nested():` | Inner rollback preserves outer state on both |
| Retry/backoff under conflict | Race two writers; one should retry | Retry policy documented; SA `OperationalError` retryable on both |
| SA pool config | `NullPool` + DuckDB single-writer | Concurrent app+scheduler workload completes; no `TransactionContext Error: catalog write-write conflict` |
| Re-open on `DATA_DIR` change | Test fixture swaps `DATA_DIR` mid-process | Engine factory rebuilds cleanly |
| Connection lifecycle under FastAPI threadpool | 50-thread concurrent read | No connection leaks; pool config holds |

**Deliverable**: `docs/planning/duckdb-engine-spike-report.md` with each row tagged **PASS** / **FAIL** / **WORKAROUND-AVAILABLE** + concrete GH issue references for blockers found in `Mause/duckdb_engine`.

If any of `update().returning()`, FK enforcement, savepoints, or SA pool config is FAIL with no workaround: **stop. Tranche 3 abandoned. Dual-repo continues; revisit post-Quack-v2.0.**

### Phase 3 — Slack-binding repo + models + contract

**Adversary v2 fix unchanged from v2 (already addressed slack-binding gap).**

- New models in `src/models/slack.py`: `SlackBindingCode`, `SlackBindingIssueLog`, `SlackBindingRedeemLog`.
- New repo `src/repositories/slack_bindings.py` + `slack_bindings_pg.py`.
- New `tests/db_pg/test_slack_bindings_contract.py` parametrized over both backends with `COVERED_METHODS = [...]`.
- Alembic migration + matching `_v(N)_to_v(N+1)` in `src/db.py`.
- Rewrite `services/slack_bot/binding.py` to call the repo. Drop allowlist entries.

### Phase 4 — Facet + aggregate routes (~25 hits)

Same as v2. Each lift drops the corresponding allowlist entry. Notable: `app/api/admin_usage.py` LLM-validated SELECT surface classified as `state-via-duckdb-attach` (was `analytics-domain` in v2 — corrected per adversary v2's NEW-HIGH-9) with owner + 90-day expiry until Phase 6 ships.

### Phase 5 — Chat + secrets repository moves + Tranche-1 baseline cleanup

**Adversary v2 fix NEW-HIGH-6.**

**5a. Chat (NOT a mechanical move).**

1. Ship `tests/db_pg/test_chat_invariants_contract.py` parametrized over both backends — common invariants only (schema, indexes, CRUD round-trip, FK enforcement).
2. Ship `tests/test_chat_transaction_semantics.py` — NOT parametrized; two named test classes:
   - `TestPgAtomicFork`: PG-only; asserts `chat_session_participants_pg:136-146` atomic fork behavior.
   - `TestDuckDbBestEffortFork`: DuckDB-only; asserts best-effort semantics + documents as public contract.
3. Decision: DuckDB stays best-effort. Documented as known-limitation in `ChatRepository` docstring + above test.
4. Split `app/chat/persistence.py` into 4 cluster repos (each dual-DD+PG until Phase 8):
   - `src/repositories/chat_sessions.py` + `chat_sessions_pg.py`
   - `src/repositories/chat_messages.py` + `chat_messages_pg.py`
   - `src/repositories/chat_session_participants.py` + `_pg.py`
   - `src/repositories/user_workdirs.py` + `_pg.py`
5. `app/chat/persistence.py:ChatRepository` becomes thin facade → factory. Callers unchanged.
6. Drop 31 allowlist entries.

**5b. Secrets vault.**

- Move three classes from `app/secrets_vault.py` to `src/repositories/{mcp_secrets,system_secrets,mcp_user_secrets}.py`.
- Update `_REGISTRY` (currently routes to `app.secrets_vault`).
- New `tests/db_pg/test_{mcp_secrets,system_secrets,mcp_user_secrets}_contract.py` with `COVERED_METHODS`.
- Drop 13 allowlist entries.

**5c. Tranche-1 baseline cleanup (adversary v2 fix NEW-MED-11).**

After 5a + 5b complete: regenerate `tools/lint/sql_allowlist.yaml`. Audit each remaining entry:

- `analytics-domain`: keep. Permanent.
- `dialect-bootstrap`: keep. Permanent.
- `liveness`: keep. Permanent.
- `state-via-duckdb-attach`: must have owner + 90-day expiry (default to Phase 6 ship date). After expiry CI fails until either re-classified, re-extended, or resolved.
- Any state-table entry NOT in the four categories above: FAIL. Must be lifted before Phase 5 ends.

**End of Tranche 1**: 65 bug-class spots in repos; lint rule catches drift via stable-callsite-ID baseline; coverage matrix green for every registry key + method; baseline contains ONLY durable analytics escapes + time-bounded state-via-attach exceptions. **Invariant met sustainably.**

---

## Tranche 2 — Boundary resolution (Phases 6–7)

### Phase 6 — Usage telemetry backend ownership

**Adversary v2 fix NEW-CRIT-3.**

Current state (adversary v1 evidence, unchanged):
- `UsageProcessor.process_session` takes DuckDB cursor at `services/session_processors/usage.py:33-39`
- `MarketplaceItemLookup(conn)` at `usage.py:53` — DuckDB cursor read of state tables
- Writes events via `repo = usage_repo()` factory at `usage.py:103-105`
- Rollup INSERT-SELECT uses same DuckDB cursor at `usage_lib.py:670-742`

**Recommendation: path (b) — usage moves to PG fully.**

**Phase 6 deliverables**:

1. `docs/planning/usage-backend-decision.md` documenting the decision + rollout plan.
2. Integration test `tests/integration/test_usage_export_attach_path.py`:
   - Setup: PG fixture with 30-day synthetic `usage_events` (~10M rows).
   - DuckDB attach: `ATTACH '<libpq-uri>' AS pg (TYPE postgres, READ_ONLY)`.
   - Filtered export: `COPY (SELECT * FROM pg.usage_events WHERE event_date >= ?) TO 'tmp.parquet' (FORMAT 'parquet')`. Assert: EXPLAIN shows predicate pushdown; peak RSS < 512 MiB.
   - Unfiltered export: `COPY (SELECT * FROM pg.usage_events) TO 'tmp.parquet' (FORMAT 'parquet')`. Assert: peak RSS < 1 GiB (configurable cap).
   - Fallback path: if ATTACH-COPY route fails (DuckDB postgres extension issue), SA Core paginated SELECT → `pyarrow.parquet.RecordBatchFileWriter` streams to disk. Test both paths.
3. Read primitive change in `services/session_processors/usage.py`: `MarketplaceItemLookup` rebuilt to use `marketplace_plugins_repo()` + `store_entities_repo()` factory access (no DuckDB cursor).
4. Rollup INSERT-SELECT rewritten as SA Core `insert().from_select()` (verified by Phase 2 spike).
5. Risk register row added: **PG-state deployment disabled** in `db_state_machine` until Phase 6 ships (the integration test green).

**Path (a) is documented but not recommended**: keeping `usage_*` + `marketplace_plugins` mirror on DuckDB costs sync-on-write to both engines + analytics layer reaching into state. Higher operational tax than path (b).

### Phase 7 — Knowledge FTS contract + PG search shipping

**Adversary v2 fix NEW-CRIT-1.**

Reality today (unchanged from v2 acknowledgment):
- DuckDB uses `fts_main_knowledge_items.match_bm25` with `strip_accents=1` at `src/fts.py:14-15, 57-58`.
- PG uses `to_tsvector('english', ...)` + `plainto_tsquery('english', :q)` at `knowledge_pg.py:292-316`.
- `KnowledgeItem` has NO `search_vector` column.
- Czech-diacritic test is DuckDB-only.

**Phase 7 redesign (drop BM25-equivalent ambition):**

1. New `tests/db_pg/test_knowledge_contract.py` parametrized cross-dialect with:
   - **Lexical-match parity**: query `česky` matches body `cesky` and vice versa on both backends.
   - **Top-K-overlap relevance**: top-10 results across both backends share ≥ 7 items for a curated query set (relaxed from "BM25-equivalent"). Acknowledges PG `ts_rank` is lexicographic, not statistical; DuckDB BM25 is statistical; perfect ranking parity is NOT a requirement.
   - **Count parity**: same query returns same row count on both (within a non-ranking match-set sense).
   - **ILIKE fallback parity**: when FTS extension unavailable, fallback returns same rows on both.

2. Alembic migration adds custom text-search configuration on PG:
   ```sql
   CREATE EXTENSION IF NOT EXISTS unaccent;
   CREATE TEXT SEARCH CONFIGURATION public.cs_unaccent (COPY = pg_catalog.simple);
   ALTER TEXT SEARCH CONFIGURATION public.cs_unaccent
     ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple;
   ```
   (Built on `pg_catalog.simple` to retain whitespace tokenization; `unaccent` strips diacritics. This is the documented PG idiom.)

3. Add `search_vector` GENERATED ALWAYS AS column on PG `knowledge_items`:
   ```sql
   ALTER TABLE knowledge_items ADD COLUMN search_vector tsvector
     GENERATED ALWAYS AS (
       to_tsvector('public.cs_unaccent',
         coalesce(title, '') || ' ' || coalesce(body, ''))
     ) STORED;
   CREATE INDEX knowledge_items_search_vector_idx
     ON knowledge_items USING GIN (search_vector);
   ```

4. Rewrite `src/repositories/knowledge_pg.py` FTS query:
   ```sql
   WHERE search_vector @@ plainto_tsquery('public.cs_unaccent', :q)
   ORDER BY ts_rank(search_vector, plainto_tsquery('public.cs_unaccent', :q)) DESC
   ```
   (Uses `ts_rank` not `ts_rank_cd`; both are lexicographic but `ts_rank` is the conventional default for FTS without cover-density boost.)

5. **Optional Phase 7b** (deferred unless user feedback demands it): evaluate `pg_search` extension (paradedb.com) for BM25-native ranking. Operational cost: extension install, deployment story, licensing review. NOT default; ships only if Phase 7 default ranking is provably insufficient via user metrics.

6. Contract test must pass on BOTH backends before Phase 8 includes the `knowledge` cluster.

**This is the prerequisite to consolidating the knowledge repo in Phase 8.**

---

## Tranche 3 — ORM consolidation (Phases 8–11)

**Conditional**: ONLY runs if Phase 2 spike clears AND Phase 6 decision is path (b) AND Phase 7 contract test green.

### Phase 8 — Cluster matrix (FK-topologically sorted)

**Adversary v2 fix NEW-HIGH-8.**

Cluster definition: each cluster is identified by registry keys (not table names, not repo names). Each cluster lists owned tables, FK targets to other clusters, dependencies, migration_function_name, contract test, models.

| Cluster | Registry keys | Owned tables | FKs into | Depends on | Migration fn | Contract test | Model files |
|---:|---|---|---|---|---|---|---|
| 1 | `users`, `audit`, `cli_auth_codes`, `setup_tokens`, `profile` | `users`, `audit_log`, `cli_auth_codes`, `setup_tokens`, `table_profiles` | — (root) | — | varies | `test_users_contract.py`, `test_audit_contract.py`, `test_profile_contract.py` | `models/audit.py`, `models/misc.py`, `models/config.py` |
| 2 | `user_groups`, `user_group_members` | `user_groups`, `user_group_members` | cluster 1 (users) | 1 | varies | `test_rbac_contract.py` (partial) | `models/rbac.py` |
| 3 | `claude_md_template`, `welcome_template`, `news_template` | `instance_templates`, `news_template` | — | — | varies | gap → exemption | `models/lookup.py` |
| 4 | `table_registry`, `sync_state`, `sync_settings`, `view_ownership`, `column_metadata`, `bq_metadata_cache` | corresponding tables | cluster 1, 2 | 1, 2 | varies | `test_table_registry_contract.py` (gap → schedule) | `models/ops.py`, `models/misc.py` |
| 5 | `notifications_telegram`, `notifications_pending_code`, `notifications_script` | `telegram_links`, `pending_codes`, `script_registry` | cluster 1 | 1 | varies | gap → exemption | `models/misc.py` |
| 6 | `marketplace_registry`, `marketplace_plugins` | `marketplace_registry`, `marketplace_plugins` | — | — | varies | `test_marketplace_plugins_grants_contract.py` (partial; covers grants JOIN only) | `models/store.py` |
| 7 | `store_entities`, `user_store_installs`, `user_curated_subscriptions`, `store_submissions`, `user_stack_subscriptions` | corresponding tables | cluster 1, 6 | 1, 6 | varies | `test_store_contract.py` (partial) | `models/store.py` |
| 8 | `data_packages` | `data_packages`, `data_package_tables`, `data_package_tools` | cluster 1, 4 | 1, 4 | varies | `test_data_packages_contract.py` | `models/data_packages.py` |
| 9 | `recipes` | `recipes` | cluster 1, 8 | 1, 8 | varies | `test_recipes_contract.py` | `models/recipes.py` |
| 10 | `memory_domains`, `memory_domain_suggestions` | `memory_domains`, `knowledge_item_domains`, `memory_domain_suggestions` | cluster 1 | 1 | varies | `test_memory_domains_contract.py`, `test_memory_domain_suggestions_contract.py` | `models/knowledge.py` |
| 11 | `usage` (conditional on Phase 6 path) | `usage_events`, `usage_session_summary`, `usage_tool_daily`, `usage_marketplace_item_daily`, `usage_marketplace_item_window` | cluster 1, 6 | 1, 6, **Phase 6 done** | varies | gap → schedule | `models/telemetry.py` |
| 12 | `personal_access_token`, `access_token`, `setup_banner`, `metric`, `session_processor_state`, `observability_views` | `personal_access_tokens`, `setup_banner`, `metric_definitions`, `session_processor_state`, `user_observability_views`, etc. | cluster 1 | 1 | varies | `test_access_tokens_contract.py` (partial), others gap | `models/config.py`, `models/misc.py` |
| 13 | `mcp_sources`, `tool_registry`, `mcp_secrets`, `system_secrets`, `mcp_user_secrets` | corresponding tables | cluster 1 | 1, **Phase 5b done** | varies | `test_mcp_sources_contract.py`, `test_system_secrets_contract.py`, others gap → schedule | `models/mcp.py`, `models/vault.py` |
| 14 | `chat_session`, `chat_message`, `chat_session_participant`, `user_workdir` | corresponding tables | cluster 1 | 1, **Phase 5a done** | varies | `test_chat_invariants_contract.py` (Phase 5) | `models/chat.py` |
| 15 | `slack_bindings` (added Phase 3) | `slack_binding_codes`, `slack_binding_issue_log`, `slack_binding_redeem_log` | cluster 1 | 1, **Phase 3 done** | varies | `test_slack_bindings_contract.py` (Phase 3) | `models/slack.py` |
| 16 | `knowledge` | `knowledge_items`, `knowledge_contradictions`, `knowledge_item_relations`, `verification_evidence`, `knowledge_votes`, `knowledge_item_user_dismissed` | cluster 1, 10 | 1, 10, **Phase 7 done** | varies | `test_knowledge_contract.py` (Phase 7) | `models/knowledge.py` |
| 17 | `resource_grants` (LAST — has FKs to most other clusters) | `resource_grants` | clusters 1, 2, 4, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16 | all of the above | new fn | `test_rbac_contract.py` (extend) | `models/rbac.py` |

Per cluster PR pattern (unchanged from v2 mechanically):
- Drop DD repo file.
- Promote PG repo to single file; rewrite to use SA dialect-agnostic API.
- Update `_REGISTRY` to single backend entry.
- Add/extend `tests/db_pg/test_<key>_contract.py` to parametrize over `duckdb+engine://` AND `postgresql+psycopg://`.
- Add `COVERED_METHODS` list per coverage matrix gate.
- Delete the cluster's `_v(N)_to_v(N+1)` migration step from `src/db.py`; assert equivalent Alembic step landed.

**FK constraint deferral** (for `resource_grants` and any other cluster with cross-cluster FKs): Alembic migration in cluster 17 (or wherever the resource_grants consolidation lands) creates FK constraints AFTER all referenced clusters' migrations have run. SA model declarations carry the FK metadata but constraint creation is sequenced explicitly.

### Phase 9 — Sweep: remove dead DuckDB repo files

Same as v2. Each `<name>_pg.py` renamed to `<name>.py` and DuckDB-class file deleted, OR vice versa.

### Phase 10 — Retire `src/db.py` DDL ladder (KEEP recovery helpers)

**Adversary v2 fix unchanged — already addressed correctly in v2.**

- DELETE: every `_v<N>_to_v<N+1>(conn)` function; every state-table `CREATE TABLE IF NOT EXISTS`.
- KEEP: WAL salvage (`src/db.py:1397-1436`), pre-migrate snapshot (`src/db.py:1280-1390`), `CHECKPOINT` post-migration (`src/db.py:5453-5477`), `system.duckdb.pre-migrate` copy (`src/db.py:5116-5127`), `_try_open_system_db` recovery — DuckDB-state lifecycle, runs iff `DATABASE_URL` points at `duckdb://`.
- KEEP: `get_analytics_db()`, BQ extension bootstrap, FTS install.
- MOVE: schema_version backfill logic to `migrations/env.py` if needed for Alembic transition.

Estimated post-deletion `src/db.py` LOC: ~2500 (down from 5565). `tests/test_db_schema_version.py` updates to assert Alembic head equals expected.

### Phase 11 — Factory simplification + migration-tool inventory + rework

**Adversary v2 fix NEW-CRIT-2.**

**Phase 11a — migration-tool inventory** (one PR, no code change, ships test stubs):

For each existing migrator behavior, ship a named regression test in `tests/integration/test_migrator_*.py`:

| Behavior | Location today | Named regression test |
|---|---|---|
| Job JSON + liveness heartbeat | `scripts/db_state_migrator.py:68-127` | `test_migrator_heartbeat.py::test_heartbeat_updates_during_long_copy` |
| Cancel sentinels | `scripts/db_state_migrator.py:198-234` | `test_migrator_cancel.py::test_cancel_sentinel_aborts_copy_mid_task` |
| Final cancel-before-flip lock | `scripts/db_state_migrator.py:1179-1208` | `test_migrator_cancel.py::test_cancel_before_flip_lock_holds` |
| URL password masking | `scripts/db_state_migrator.py:249-265` | `test_migrator_logging.py::test_alembic_timeout_error_redacts_password` |
| Audit PII scrub before copy | `scripts/db_state_migrator.py:361-388, 488-535` | `test_migrator_pii.py::test_audit_log_email_redacted_in_copied_rows` |
| Backup hard-fail + cleanup | `scripts/db_state_migrator.py:914-997` | `test_migrator_backup.py::test_half_written_backup_removed_on_failure` |
| Streaming chunked PG-to-PG copy | `scripts/db_state_migrator.py:573-721` | `test_migrator_streaming.py::test_pg_to_pg_chunked_copy_bounded_memory` |
| Halt-on-first-failure | `scripts/migrate_duckdb_to_pg/__init__.py:194-285` | `test_migrator_halt.py::test_first_task_failure_aborts_subsequent` |
| Idempotent retry | (implicit, derived from upserts) | `test_migrator_idempotent.py::test_retry_on_partial_completion_resumes_cleanly` |

Phase 11a ships these tests against the current implementation (they all pass today). Acts as the regression net.

**Phase 11b — rework** (subsequent PRs):

- `_REGISTRY` collapses to single-entry per repo key.
- `use_pg()` → `active_dialect()` returning SA dialect name.
- `scripts/db_state_migrator.py` + `scripts/migrate_duckdb_to_pg/tasks.py` re-tooled. **Phase 11a tests gate the rework: every named test must stay green.**
- Plus new tests from v2 (dry-run, rollback, row-count parity, checksum parity, compose-startup ordering, SIDE_CAR → CLOUD integration).

---

## Risk register (v3)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `duckdb-engine` Alembic + `UPDATE RETURNING` gap | High | Blocks Tranche 3 | Phase 2 gates; fallback dual-repo |
| DuckDB single-writer + SA pool config | Medium | Medium | Phase 2 explicit `NullPool` + isolation + lock-wait tests |
| Knowledge FTS ranking divergence on PG | Medium | Low (relaxed contract) | Phase 7 contract uses top-K-overlap not BM25-equivalent |
| Usage backend split-brain on PG-state | High today | High today | Phase 6 named decision; PG-state DISABLED until Phase 6 ships |
| `src/db.py` lifecycle helper accidentally deleted | Medium | High | Phase 10 split — DDL vs lifecycle helpers as explicit checklist |
| Chat fork semantics drift during repo move | Medium | Medium | Phase 5 splits invariant-parity + semantic-divergence tests |
| Phase 11 regresses migrator safety (cancel/PII/backup/etc.) | High | High | Phase 11a inventory ships first; tests gate the rework |
| Phase 7 PG FTS impl details wrong | Low (v3) | Medium | v3 uses real `cs_unaccent` config + `ts_rank` (not `ts_rank_cd`) |
| Phase 6 ATTACH-postgres memory blowup | Medium | Medium | Phase 6 integration test asserts peak RSS bound + EXPLAIN pushdown |
| Allowlist becomes noisy permanent baseline | Low (v3) | Low | Phase 5c regenerates with reason taxonomy + 90d expiry on state-via-attach |
| Coverage matrix blocks healthy split PRs | Medium | Low | Phase 1a defines 30-day exemption with owner + expiry |
| DuckDB-Quack ships before Tranche 3 done | Medium | Variable | Tranche 1 ships standalone value; Tranche 3 re-evaluated post-Quack |

---

## Sequencing summary (v3)

| Phase | What | PRs | Weeks | Depends on |
|---|---|---:|---|---|
| **Tranche 1 — invariant cleanup** | | | **5–6** | |
| 0 | Stable-callsite lint + reason taxonomy | 1 | 1.5d | — |
| 1 | Coverage matrix + quick wins (~30 hits) | ~10 | 1.5w | 0 |
| 2 | duckdb-engine spike (PARALLEL) | 1 throwaway | 2w | — |
| 3 | Slack-binding repo + models + contract | 1 | 0.5w | 0, 1a |
| 4 | Facet + aggregate routes (~25 hits) | ~10 | 1.5w | 0 |
| 5 | Chat facade redesign + secrets move + baseline cleanup | 3 | 1.5w | 0, 4 |
| **Tranche 2 — boundary resolution** | | | **2–3** | |
| 6 | Usage backend decision + ATTACH integration test + processor rewrite | 1 | 1.5w | 1a |
| 7 | Knowledge FTS contract + PG search shipping | 1 | 1w | 1a |
| **Tranche 3 — ORM collapse (GATED)** | | | **3–5** | |
| 8 | Cluster-by-cluster ORM (17 clusters, FK-sorted) | ~17 | 2–4w | 2 GREEN, 6, 7 done |
| 9 | Sweep dead repo files | 1 | 0.5w | 8 |
| 10 | Retire src/db.py DDL ladder | 1 | 0.5w | 8, 9 |
| 11a | Migrator behavior inventory (tests against current impl) | 1 | 0.5w | — (parallel with 8) |
| 11b | Factory simplification + migrator rework | multi-PR | 1w | 10, 11a |

**Total**: ~10–14 engineer-weeks if Tranche 3 ships. **~5–9 weeks** if Tranche 3 stops at Phase 2 spike fail (invariant still locked via Tranche 1).

---

## Fact-check verdict (v3)

| Claim | Reality |
|---|---|
| "No raw SQL hardcoded except in the analytical part" today | **REFUTED** — 65 bug-class spots |
| Tranche 1 (~5w) delivers invariant standalone | **YES** — stable-callsite lint + 65 lifts; reason taxonomy; baseline cleanup at Phase 5c |
| Tranche 3 ORM collapse cost firm | **NO — gated on Phase 2 spike**. If spike fails, Tranche 3 doesn't start; dual-repo continues |
| Postgres becomes mandatory | **NO** — `duckdb-engine` carries laptop/dev path |
| DuckDB stays for analytics | **YES** — explicit fence; lifecycle helpers in `src/db.py` kept |
| Cross-engine contract tests safety net | **PARTIAL today**, addressed in Phase 1a coverage gate + Phases 3, 5, 7 (knowledge + slack + chat covered by end of Tranche 1) |
| Phase 11 preserves existing migrator safety | **YES** — Phase 11a inventory ships tests against current impl BEFORE rework |
| Permanent state sustainable (no path:line allowlist debt) | **YES** — Phase 5c baseline cleanup; only durable analytics escapes remain; state-via-attach exceptions have expiry |
| FTS parity on PG | **PARTIAL by design** — lexical-match + top-K-overlap, NOT BM25-equivalent. Documented limitation |

---

## Audit scripts (appendix)

```bash
# Find all execute() call sites outside the allowlist (AST-based).
python tools/lint/sql_check.py --allowlist tools/lint/sql_allowlist.yaml --check

# Regenerate the allowlist snapshot (CODEOWNERS-gated).
python tools/lint/sql_check.py --allowlist tools/lint/sql_allowlist.yaml --generate

# Coverage matrix gate (every _REGISTRY key has a contract + method coverage).
python tools/lint/registry_coverage.py --exemptions tools/lint/contract_exemptions.yaml

# Models / registry / repo class consistency.
rg -n '__tablename__\s*=' src/models/*.py | wc -l                      # → 62 today
rg -nP '^\s+"[a-z_]+":\s*\{' src/repositories/__init__.py | wc -l       # → 45 today
rg -nP 'class\s+[A-Z][A-Za-z]*Repository' src/repositories/*.py | wc -l # → DD repo classes
```

---

## Appendices

- **`docs/planning/orm-migration-adversary-review.md`** — Codex adversary v1
- **`docs/planning/orm-migration-adversary-review-v2.md`** — Codex adversary v2 (which informed v3)
- **`docs/planning/agnes-orm-inventory-src.md`** — file-by-file `src/` inventory
- **`docs/planning/agnes-orm-inventory-app.md`** — file-by-file `app/` inventory
- **`docs/planning/agnes-orm-inventory-cli-conn-svc.md`** — file-by-file `cli/`, `connectors/`, `services/`, `scripts/` inventory
- **`docs/planning/agnes-orm-rawsql-audit.md`** — 73 numbered raw-SQL findings

Future appendices (land with the phases that produce them):
- `docs/planning/duckdb-engine-spike-report.md` — Phase 2
- `docs/planning/usage-backend-decision.md` — Phase 6
- `docs/planning/orm-coverage-matrix.md` — Phase 1a (full per-table matrix)
