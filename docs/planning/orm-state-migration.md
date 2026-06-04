# ORM-on-state migration plan — v2

**Date**: 2026-06-04
**Branch**: `vr/orm-migration-plan` (off `origin/main` @ `46943e9b`)
**Status**: research + plan, no code change
**Supersedes**: v1 (commit `a8555f8a`). Restructured per the adversary review at `docs/planning/orm-migration-adversary-review.md` — every CRITICAL/HIGH finding addressed below; the doc tracks where.

## Goal

Eliminate raw SQL from non-repo code paths. Single SQLAlchemy ORM layer for application state (today: 37 DuckDB repos + 41 Postgres mirrors via a declarative `_REGISTRY` factory at `src/repositories/__init__.py`). Same ORM models run on DuckDB or Postgres via the dialect layer (`duckdb-engine` for DuckDB, `psycopg` for PG) — **iff** the duckdb-engine spike (Phase 2) clears. DuckDB stays for analytics: `analytics.duckdb`, `extract.duckdb`, BQ extension, parquet views, FTS extension.

## TL;DR

User invariant under inspection: **"there should be no raw SQL hardcoded except in the analytical part"**.

**Verdict: REFUTED today; achievable as a sequenced cleanup gated by a real spike.**

- 73 raw-SQL spots outside `src/repositories/` (audit at `docs/planning/agnes-orm-rawsql-audit.md`).
  - 65 bug-class (state tables).
  - 17 acceptable analytics escapes.
  - 1 needs-discussion → resolved in this v2 (see §"Admin LLM-SQL classification").
- 2 misplaced repository-pattern files (`app/chat/persistence.py`, `app/secrets_vault.py`) — **not mechanical moves**, see Phase 5.
- Today: 62 mapped tables across 15 `src/models/*.py` files; 45 registry keys in `_REGISTRY`; 15 cross-engine contract tests (14 parametrize both backends, 1 via shared `state_backend` fixture). **Knowledge + slack-binding clusters have NO contract test yet.**

**Single cost estimate**: ~9–13 engineer-weeks, split:

| Tranche | Phases | Weeks | Standalone value |
|---|---|---:|---|
| **Invariant cleanup (no new dep)** | 0–5 | 4–5 | Ships the 65 bug-class spots into repos. Lint baseline locks future drift. **Delivers the invariant even if everything below stalls.** |
| **Spike + boundary resolution** | 2 (in parallel) + 6–7 | 2 | Validates `duckdb-engine` for SA on DuckDB. Resolves usage / chat / knowledge boundary designs as named contract tests. |
| **ORM consolidation** | 8–11 | 3–6 | Single ORM. Only runs if Phase 2 spike clears. |

The invariant ships first. The unified ORM is a *follow-on bet* gated by Phase 2.

---

## What changed vs. v1 (adversary fixes)

| Adversary finding | v1 said | v2 says |
|---|---|---|
| **CRIT-1**: Lint rule blocks own cleanup PRs | Phase 0 ships rule, "Existing 65 spots fail until lifted" | Phase 0 ships **baseline-snapshot** lint (existing offenders in a frozen allowlist file; new offenders fail; allowlist shrinks per cleanup PR via codeowners gate) |
| **CRIT-2**: `RETURNING` on DuckDB UPDATE not validated | Phase 7 spike covers RETURNING as one checkbox | Phase 2 spike is *the gate*, with explicit pass/fail criteria for `update().returning()` cross-dialect. Phases 8+ can't start until spike report ships. Listed CAS-pattern fallbacks. |
| **CRIT-3**: Usage telemetry split-brain | "Keep `usage_*` on DuckDB even under PG state" | Phase 6 names this as a decision *before* allowing PG state. Two paths costed: (a) usage stays DuckDB and the marketplace_plugins/store_entities lookups stay DuckDB too in the processor; (b) usage moves to PG fully. **Bias to (b)** — explicit rationale below. |
| **CRIT-4**: `src/db.py` deletion removes recovery code | Phase 10: "Delete `_v1_to_v(N)` chain from `src/db.py`" | Phase 10 split: DDL ladder retires; WAL salvage / pre-migrate snapshot / CHECKPOINT helpers / schema_version backfills *stay* until DuckDB-state is no longer supported (which the plan never claims). |
| **HIGH-5**: Chat move not mechanical | "Move `app/chat/persistence.py` → `src/repositories/chat.py`" | Phase 5 is *facade redesign*: transaction-parity tests pinning DuckDB no-multi-txn vs PG single-txn behavior, then move. Adapter pattern preserved (or explicitly retired). |
| **HIGH-6**: Knowledge FTS parity promised before it exists | Phase 9 ships PG `tsvector + GIN + unaccent` + Czech-diacritic contract | New Phase 7 (preceding Phase 8): land `tests/db_pg/test_knowledge_contract.py` parametrized cross-dialect WITH Czech diacritics + BM25-rank parity + ILIKE-fallback parity. PG `unaccent` + `search_vector` GENERATED column ship in the **same** PR. |
| **HIGH-7**: Contract-test safety net overstated | "15 cross-engine contract tests… mandated safety net" | Pre-Phase 1: ship a coverage matrix (`tests/db_pg/test_registry_coverage.py`) gating every `_REGISTRY` key to a named contract test or a documented exemption. Knowledge + slack are explicit gaps to close. |
| **HIGH-8**: Model/registry completeness via file count | "15 files = full mapping" | Per-table matrix at end of doc: table → model class → DD repo method-owner → PG repo method-owner → contract test → cluster phase. Replaces the file-count framing. |
| **HIGH-9**: Migration tooling cited as proof; later retooled | "operational pipeline" | Phase 11 is named *migration-tool rework* with explicit risk: dry-run / rollback / row-count / checksum / compose-startup tests. No "easy" claim. |
| **MED-10**: Lint rule misses f-strings/variables | "AST grep on string literals" | Lint rule v2: catches all DBAPI/SA execution outside the allowlist regardless of literal vs variable. Implemented as call-site detection: any `<obj>.execute(...)` or `<obj>.exec_driver_sql(...)` outside the allowlist fails. Allowlist file lists exact exemptions with reason strings. |
| **MED-11**: DuckDB pool config underspecified | "NullPool — already understood pattern" | Phase 2 spike output includes a written engine-factory spec: pool class, pool size, `DATA_DIR` change handling, write retry policy, transaction scope, concurrent-write test. |
| **MED-12**: Quack timing supports spike-first | "DuckDB-Quack lands fall 2026" as a risk row | v2 explicitly frames Phases 0–5 as invariant cleanup that ships regardless of Quack timing. Phases 6–11 are conditional on the Phase 2 spike + Quack roadmap check. |
| **MED-13**: Admin LLM SQL unclassified | "needs-discussion" | Classified: **analytics escape, DuckDB-only**. Lint allowlist entry: `app/api/admin_usage.py` with reason "LLM-generated SQL validated by `validate_select_only`; surface is analytical-tier; DuckDB-pinned". Phase 6 decision (b) doesn't apply here (this is read-only export, not transactional writes). |
| **LOW-14**: Registry grep brittle | Used `_REGISTRY\s*=` | Audit scripts updated to match `_REGISTRY\s*:\s*` (typed assignment) OR plain `=`. Documented in §"Audit scripts" appendix. |
| **LOW-15**: Script deletion overreach | "Delete after run" list | Split into "unreferenced by runtime" (safe to remove from import path; keep in repo for operator history) vs "delete entirely" (none in v2 — operator tooling stays). |
| **LOW-16**: Two effort estimates | 5–8w and 7–9w | One number: **9–13 engineer-weeks** total, broken into three tranches above. |

---

## Architecture target

End state (only reachable if Phase 2 spike clears):

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
│  DATABASE_URL=duckdb+quack:///... (fall 2026, ETA)         │
│    → quack extension → DuckDB-Quack (when GA)              │
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

If the spike doesn't clear: stop at end of Tranche 1. Invariant still holds; dual-repo continues; revisit in 6–12 months (likely after DuckDB-Quack v2.0).

---

## Tranche 1 — Invariant cleanup (Phases 0–5)

**Goal**: bring the 65 bug-class spots into repos; lock future drift via baseline lint. **Ships standalone value even if Tranche 2/3 never run.**

### Phase 0 — Baseline lint + per-file allowlist (small PR)

**Adversary fix CRIT-1.**

- Add AST-aware lint rule that fails on ANY `<obj>.execute(...)` / `<obj>.exec_driver_sql(...)` / `sa.text(...).execute(...)` call OUTSIDE the explicit allowlist file (`tools/lint/sql_allowlist.txt`).
- Detection target: call-site, NOT string-literal shape. Catches `conn.execute(local_sql_variable)`, f-strings, multi-line triple-quoted strings, LLM-validated paths. (Adversary fix MED-10.)
- Allowlist file at commit time: snapshot of today's 73 spots + their justifications.
  - Each allowlist line: `path:line  reason  cluster` (e.g., `app/api/marketplace.py:412  rollup-state-DuckDB-pinned  usage`).
- New raw-SQL spots fail CI. Removing an allowlist entry requires the cleanup PR that lifts that spot into a repo. CODEOWNERS gates the file so the allowlist only shrinks.

**Outcome**: future drift impossible; existing offenders explicit + addressed in priority order in subsequent phases.

### Phase 1 — Coverage matrix + quick wins (~30 hits)

**Adversary fix HIGH-7, HIGH-8.**

**1a.** Ship `tests/db_pg/test_registry_coverage.py`: assertion that every `_REGISTRY` key has either:
- a named `tests/db_pg/test_<cluster>_contract.py` parametrizing both backends, or
- an entry in `tests/db_pg/contract_exemptions.txt` with a reason.

Today's 14-of-15 parametrized contract tests pass immediately. **Knowledge** + **slack-binding** (Phase 3 work) + chat clusters fail the coverage gate — addressed in their respective phases. Initial exemptions: knowledge (Phase 7), chat clusters (Phase 5), slack-binding (Phase 3).

**1b.** Quick-win PRs that lift the ~30 single-hit / no-design-needed spots into existing repos. Per-cluster PRs:

| File | Repo method to add | Hits |
|---|---|---:|
| `app/chat/audit.py` | `audit_repo().log()` (exists) | 1 |
| `app/chat/copresence_summary.py` | `chat_session_repo().title_for()` | 1 |
| `app/api/chat_copresence.py` | `users_repo().get_by_email()` (exists) | 1 |
| `app/api/sync.py` | `users_repo().mark_last_pull()` | 1 |
| `app/api/me_debug.py` | `user_group_members_repo().google_sync_summary()` | 1 |
| `app/api/store.py` | `store_entities_repo().synthetic_name_collision()` + `.revert_archive()` | 2 |
| `app/api/my_stack.py` | existing repos | 2 |
| `app/api/admin_user_sessions.py` | `usage_session_summary_repo().for_user()`, `users_repo().get()`, `audit_repo().count_for_user()` | 3 |
| `src/store_guardrails/purge.py` | `store_submissions_repo().purgeable()` | 1 |
| `src/store_guardrails/reaper.py` | `store_submissions_repo().reap_stuck_reviews()` + `.mark_review_error()` | 2 |
| `src/claude_md.py` | `table_registry_repo().list_all()` / `.list_by_ids()`, `metric_definitions_repo().category_counts()`, `marketplace_registry_repo().names_by_ids()` | 3 |
| `src/rbac.py` | `resource_grants_repo().has_grant_for_user()` | 2 |
| `connectors/internal/registry.py` | `table_registry_repo().prune_internal_except()` | 1 |
| `services/slack_bot/events.py` | `users_repo().get_by_slack_id()` | 1 |
| `services/session_pipeline/runner.py` | `users_repo().get()` (exists) | 1 |
| `services/verification_detector/__main__.py` | `session_processor_state_repo().delete()` | 1 |
| `app/web/router.py` | ~12 small COUNT / SELECT 1 helpers → cluster-split | ~12 |

Each PR drops the corresponding allowlist line. ~30 hits gone.

### Phase 2 — `duckdb-engine` spike (parallel with Phase 1)

**Adversary fix CRIT-2, MED-11, MED-12.**

**Phase 2 is the gate.** Tranche 3 (Phases 8–11) cannot start until Phase 2 ships a green spike report.

**Time-box**: 8 working days (was 5d — adversary noted underestimation).

**Pass/fail criteria** (each a binary):

| Capability | Test | Pass = |
|---|---|---|
| Alembic upgrade head on `duckdb:///...` URL | Run full `alembic upgrade head` against a fresh DuckDB file | ladder lands; final schema matches `Base.metadata.create_all()` |
| `update(...).returning(...)` for CAS | `Session.execute(update(Tok).where(...).returning(Tok.id))` | Single row returned iff WHERE matched; works on both PG and DuckDB. **Fallback documented if DuckDB fails** (manual SELECT-then-UPDATE-with-WHERE-clause-on-version_token; loses some atomicity guarantees) |
| `insert(...).returning(...)` | Same on `insert()` | Works on both |
| `delete(...).returning(...)` | Same on `delete()` | Works on both |
| JSONB on PG ↔ JSON on DuckDB | Roundtrip `dict[str, Any]` through model with `JSON` column | Survives `commit()` + `refresh()` on both |
| Computed column for FTS | PG `GENERATED ALWAYS AS (...) STORED` mirrored by DuckDB's `GENERATED ALWAYS AS (...) VIRTUAL`/STORED | Either supported or documented escape (Phase 7) |
| FK enforcement | `user_group_members.group_id → user_groups.id` | DELETE parent with child row raises on both |
| `RETURNING id` rowcount semantics | UPDATE matching 0 rows | Returns empty result, not 1-row-with-None |
| SA pool config | `NullPool` + DuckDB single-writer | Concurrent app+scheduler write workload completes; no `TransactionContext Error: catalog write-write conflict` |
| Re-open on `DATA_DIR` change | Test fixture swaps `DATA_DIR` mid-process | Engine factory rebuilds cleanly |
| Reading PostHog/observability JSON cols | Dict round-trip with nested arrays | Works on both |
| Connection lifecycle under FastAPI threadpool | 50-thread concurrent read | No connection leaks; pool config holds |

**Deliverable**: `docs/planning/duckdb-engine-spike-report.md` with each capability tagged **PASS** / **FAIL** / **WORKAROUND-AVAILABLE** + GH issue references for any blockers found in `Mause/duckdb_engine`.

**External evidence to consult during spike**:
- `https://github.com/Mause/duckdb_engine` issues list
- `https://github.com/duckdb/duckdb/issues/9915` (RETURNING for UPDATE — currently undocumented per adversary)
- DuckDB 1.5.3 release notes, especially anything about RETURNING / SA compatibility
- DuckDB-Quack beta status (`https://duckdb.org/2026/05/20/announcing-duckdb-153`): GA target fall 2026

### Phase 3 — Slack-binding repo + models (new repo + new contract test)

**Adversary fix HIGH-7 (slack-binding gap).**

- New models in `src/models/slack.py`: `SlackBindingCode`, `SlackBindingIssueLog`, `SlackBindingRedeemLog`.
- New repo `src/repositories/slack_bindings.py` (DuckDB) + `slack_bindings_pg.py` (PG) — dual-repo for now, consolidated in Phase 8.
- New `tests/db_pg/test_slack_bindings_contract.py` parametrizing both backends.
- Alembic migration adding the three tables to PG; matching `_v(N)_to_v(N+1)` step in `src/db.py` (per CLAUDE.md dual-backend discipline).
- Rewrite `services/slack_bot/binding.py` to call the repo. Drop allowlist entry for binding.py.

### Phase 4 — Facet + aggregate routes (medium repo work, ~25 hits)

**Adversary fix MED-13 (admin LLM SQL).**

- `app/api/activity.py`: `audit_repo().latest_scheduler_tick()`, `sync_history_repo().status_counts_since()`, `audit_repo().distinct_active_users()`, `session_processor_state_repo().verification_summary()`.
- `app/api/health.py`: compose existing repos. `SELECT 1` liveness ping documented in allowlist with reason "no-table liveness check".
- `app/api/observability.py`: `audit_repo().facets_for_window(since, scheduler_actions)`; existing `observability_views_repo` methods.
- `app/api/memory.py`: lifts to `user_group_members_repo().group_names_for()`, `resource_grants_repo().granted_resource_ids_for_user()`, `knowledge_votes_repo().for_user()`, existing `audit_repo().query()`, `memory_domains_repo().slugs_by_ids()`.
- `app/api/access.py`: `user_group_members_repo().delete_all_for_group()` (exists), `resource_grants_repo().delete_all_for_group()` (exists), `user_stack_subscriptions_repo().downgrade_fanout(group_id, resource_type, resource_id)` (transactional INSERT FROM JOIN — DuckDB and PG syntax checked in contract test).
- `app/api/marketplaces.py`: `marketplace_plugins_repo().exists()`, `.mark_system()`, `.unmark_system()`; `user_groups_repo().all_ids()`; `marketplace_plugins_repo().delete_all_for_marketplace()` + `resource_grants_repo().delete_for_marketplace_prefix()`.
- `app/api/admin.py`: `sync_state_repo().error_and_sync_summaries()`, `sync_history_repo().delete_for_table()`, `sync_state_repo().delete_for_table()`, `store_submissions_repo().delete()`.
- `app/resource_types.py`: lift each projection delegate to its respective repo.
- `app/api/me.py`, `app/api/me_stats.py`: NEW DECISION needed iff Phase 6 chooses path (b) → `users_repo().enrich_self()`, `usage_session_summary_repo` methods. Iff path (a): keep raw, allowlist with reason "rollup-state-DuckDB-pinned".

**Admin LLM SQL classification** (adversary MED-13): `app/api/admin_usage.py` LLM-validated SELECT surface = analytics escape, lint-allowlisted with reason "LLM Text-to-SQL validated by `validate_select_only`; pinned to DuckDB; analytical-tier read-only". Stays raw permanently. Documented in allowlist file.

### Phase 5 — Chat + secrets repository moves (with facade design)

**Adversary fix HIGH-5.**

**5a. Chat (NOT a mechanical move).**

Step-by-step:

1. Ship `tests/db_pg/test_chat_contract.py` parametrized over both backends with explicit transaction-parity assertions:
   - Atomic-fork semantics on PG (`chat_session_participants_pg:136-146`)
   - No-multi-statement-transaction on DuckDB (`app/chat/persistence.py:518-520`)
   - Private-fork message copy atomicity (PG-only? DuckDB-best-effort?)
2. **Decide**: should DuckDB acquire transactional fork semantics, or stay best-effort?
   - Option A: gain transactions on DuckDB (`BEGIN`/`COMMIT` wrap the inline SQL block). Risk: lock contention given single-writer; needs concurrency stress test.
   - Option B: keep DuckDB best-effort, document as known-limitation in `ChatRepository` docstring + contract test.
   - **Recommendation**: B (DuckDB single-writer means a contended fork could starve the rest of the app).
3. Split `app/chat/persistence.py` into 4 cluster repos (each dual-DD+PG until Phase 8):
   - `src/repositories/chat_sessions.py` + `chat_sessions_pg.py` exists
   - `src/repositories/chat_messages.py` + `chat_messages_pg.py` exists
   - `src/repositories/chat_session_participants.py` + `_pg.py` exists
   - `src/repositories/user_workdirs.py` + `_pg.py` exists
4. `app/chat/persistence.py:ChatRepository` becomes a thin facade that delegates to the four repos via the factory. Callers unchanged.
5. Drop allowlist entries for `app/chat/persistence.py` (31 spots).

**5b. Secrets vault (mechanical-ish move).**

- Three classes today in `app/secrets_vault.py` (`SharedSecretsRepository`, `SystemSecretsRepository`, `PerUserSecretsRepository`).
- PG mirrors already exist in `src/repositories/secrets_vault_pg.py`.
- The DuckDB-side declarations stay in `app/secrets_vault.py` BUT only because `_REGISTRY` already routes there:
  - `("app.secrets_vault", "PerUserSecretsRepository")` at `src/repositories/__init__.py:342-345`
  - Same for SharedSecrets, SystemSecrets at lines 346-353
- Move target: create `src/repositories/{mcp_secrets,system_secrets,mcp_user_secrets}.py` files; update `_REGISTRY`; delete the classes from `app/secrets_vault.py` (file shrinks to a thin top-level helper module or vanishes entirely).
- New `tests/db_pg/test_{mcp_secrets,system_secrets,mcp_user_secrets}_contract.py`.
- Drop 13 allowlist entries.

**End of Tranche 1**:
- All 65 bug-class spots in repos (or explicitly allowlisted with documented reason).
- Lint rule catches new drift.
- Coverage matrix turns green: every `_REGISTRY` key has a contract test.
- ~9 weeks elapsed if everything goes well (4-5w Tranche 1 + 2w Phase 2 spike running in parallel).

---

## Tranche 2 — Boundary resolution (Phases 6–7)

**Goal**: resolve the cross-engine boundaries that block ORM collapse. Each phase produces a *decision* + contract tests.

### Phase 6 — Usage telemetry backend ownership

**Adversary fix CRIT-3.**

Current state (adversary evidence):
- `UsageProcessor.process_session` accepts a `duckdb.DuckDBPyConnection` at `services/session_processors/usage.py:33-39`.
- Builds `MarketplaceItemLookup(conn)` at `services/session_processors/usage.py:53` — DuckDB cursor read of `marketplace_plugins` + `store_entities`.
- Writes events via `repo = usage_repo()` at `services/session_processors/usage.py:103-105` — factory-dispatched.
- Rollup INSERT-SELECT uses the same DuckDB cursor at `services/session_processors/usage_lib.py:670-742`.

If `DATABASE_URL=postgresql+psycopg://...` today: events go to PG; lookup attribution and rollups read empty DuckDB. **Split-brain.**

**Decision** (must be made BEFORE any PG-state deployment):

**Path (a): `usage_*` pins to DuckDB across the board.**
- Marketplace plugin lookup also pins to DuckDB (`marketplace_plugins` shadowed in DuckDB).
- Conflicts with the goal of "DuckDB only for analytics" — `marketplace_plugins` IS state.
- Requires keeping `marketplace_plugins` in both DuckDB AND PG; sync on every write. Operational tax.

**Path (b): `usage_*` moves to PG fully. RECOMMENDED.**
- `UsageProcessor` is changed to take no DuckDB cursor; lookups go through `marketplace_plugins_repo()` + `store_entities_repo()`.
- Rollup INSERT-SELECT becomes SA Core `insert().from_select()`; verified cross-dialect during Phase 2 spike.
- Admin export `COPY TO PARQUET` keeps as a DuckDB-only escape: write a small helper that ATTACHes the PG database to a fresh DuckDB session (`ATTACH 'postgres:...' AS pg`) then runs `COPY (SELECT … FROM pg.usage_events …) TO 'file.parquet'`. **Or** export via Python parquet write (pyarrow) sourcing rows from PG via SA Core. (Both options costed in spike phase.)
- LLM-validated text-to-SQL admin surface stays DuckDB-pinned per Phase 4 classification (uses a DuckDB-attached view of PG; same ATTACH escape).

**Deliverable**: `docs/planning/usage-backend-decision.md` with the chosen path + the implementation steps. **No code change in this phase yet** — just the decision + contract test design.

### Phase 7 — Knowledge FTS contract + PG search shipping

**Adversary fix HIGH-6.**

Reality today (adversary evidence):
- DuckDB uses `fts_main_knowledge_items.match_bm25` with `strip_accents=1` at `src/fts.py:14-15, 57-58`; `src/repositories/knowledge.py:545-552`.
- PG uses `to_tsvector('english', ...)` + `plainto_tsquery('english', :q)` at `src/repositories/knowledge_pg.py:292-316`.
- `KnowledgeItem` has NO `search_vector` column (`src/models/knowledge.py:32-75`).
- `migrations/versions/0010_knowledge.py:25-59` creates only ordinary indexes.
- Czech-diacritic test is **DuckDB-only** at `tests/test_knowledge_fts_search.py:239-260`. PG knowledge tests are at `tests/db_pg/test_knowledge_pg.py` — PG-only, no diacritic coverage.

**Deliverables in one Phase 7 PR**:

1. New `tests/db_pg/test_knowledge_contract.py` parametrized cross-dialect with:
   - Czech-diacritic match (`cesky` ↔ `česky`)
   - BM25-equivalent ranking (top-k order parity within tolerance)
   - ILIKE fallback parity
   - Count parity
2. Alembic migration adding `search_vector` GENERATED ALWAYS AS (`to_tsvector('unaccent_simple', coalesce(title,'') || ' ' || coalesce(body,''))`) STORED on PG.
3. PG `unaccent` extension install (in `migrations/env.py` or the migration script itself).
4. Rewrite `src/repositories/knowledge_pg.py` FTS query to use `search_vector @@ plainto_tsquery('unaccent_simple', :q)` + `ts_rank_cd(search_vector, query)` for ranking.
5. Contract test passes on BOTH backends before merge.

This is THE prerequisite to consolidating the knowledge repo in Phase 8.

---

## Tranche 3 — ORM consolidation (Phases 8–11)

**Conditional**: ONLY runs if Phase 2 spike clears AND Phase 6 decision is path (b) [or path (a) operational tax accepted].

### Phase 8 — Cluster-by-cluster ORM consolidation

Same content as v1 Phase 8 but each PR adds:
- Engine factory spec for DuckDB pool config (from Phase 2 spike).
- Updated `_REGISTRY` to single-class (or two with explicit dialect-aware methods only where the spike found gaps).
- Drop the DuckDB `_v(N)_to_v(N+1)` migration step from `src/db.py` for the cluster, asserting the equivalent Alembic step landed.

Cluster order: small → large.

1. `users`, `audit`, `access_tokens`, `setup_tokens`, `cli_auth_codes`, `profiles`
2. `user_groups`, `user_group_members`, `resource_grants` (RBAC core)
3. `claude_md_template`, `welcome_template`, `news_template`, `instance_templates`
4. `table_registry`, `sync_state`, `sync_history`, `sync_settings`, `view_ownership`, `column_metadata`, `bq_metadata_cache`
5. `notifications` cluster
6. `marketplace_registry`, `marketplace_plugins`
7. `store_entities`, `user_store_installs`, `user_curated_subscriptions`, `store_submissions`, `user_stack_subscriptions`
8. `data_packages`, `recipes`, `memory_domains`, `memory_domain_suggestions`
9. `personal_access_tokens`, `setup_banner`, `metric_definitions`, `table_profiles`
10. `session_processor_state`, `observability_views`, `usage` (depends on Phase 6 path)
11. `mcp_sources`, `tool_registry`, `mcp_secrets`, `system_secrets`, `mcp_user_secrets`
12. `chat_sessions`, `chat_messages`, `chat_session_participants`, `user_workdirs`
13. `slack_bindings`
14. **`knowledge`** (depends on Phase 7 done)

### Phase 9 — Sweep: remove dead DuckDB repo files

Once Phase 8 completes every cluster, every `<name>_pg.py` file gets renamed to `<name>.py` and the DuckDB-class file gets deleted (or vice versa — pick by which file is currently the "lead" with fewer drift items).

### Phase 10 — Retire `src/db.py` DDL ladder (KEEP recovery helpers)

**Adversary fix CRIT-4.**

This is **not** "delete src/db.py". It is "delete the `_v1_to_vN` ladder from src/db.py" — a careful surgical removal:

- **Delete**: every `_v<N>_to_v<N+1>(conn)` function. Every state-table `CREATE TABLE IF NOT EXISTS` (those are now Alembic-owned).
- **KEEP**: WAL salvage (`src/db.py:1397-1436`), pre-migrate snapshot (`src/db.py:1280-1390`), `CHECKPOINT` post-migration (`src/db.py:5453-5477`), `system.duckdb.pre-migrate` copy (`src/db.py:5116-5127`), `_try_open_system_db` recovery logic. These are the DuckDB-state lifecycle. They run iff `DATABASE_URL` points at `duckdb://`.
- **KEEP**: `get_analytics_db()`, BQ extension bootstrap, FTS install — analytics path.
- **MOVE**: schema_version backfill logic moves to `migrations/env.py` if it's needed for the Alembic transition (one-shot).

Estimated post-deletion `src/db.py` LOC: ~2500 (down from 5565). Not the ~1500 v1 claimed.

`tests/test_db_schema_version.py` updates: assert Alembic head equals expected, instead of asserting `schema_version` table contents.

### Phase 11 — Factory simplification + migration-tool rework

**Adversary fix HIGH-9.**

- `_REGISTRY` collapses to single-entry per repo key (one `(module, class)` tuple per repo). The PG dialect IS the DuckDB dialect IS the implementation.
- `use_pg()` → `active_dialect()` returning SA dialect string. Still used by analytics paths that branch (e.g., knowledge search query selects FTS strategy by dialect).
- **Migration-tool rework, named risk**:
  - `scripts/db_state_migrator.py` + `scripts/migrate_duckdb_to_pg/tasks.py` re-tooled.
  - Test plan: dry-run, rollback, row-count parity, checksum parity, compose-startup ordering, idempotent retry.
  - The migration tooling is its own PR with full integration test of the SIDE_CAR → CLOUD transition path.

---

## Risk register (v2)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `duckdb-engine` Alembic + `UPDATE RETURNING` gap | **High** | Blocks Tranche 3 | Phase 2 gates everything; fallback to dual-repo + lint locks invariant |
| DuckDB single-writer + SA pool config | Medium | Medium | Phase 2 explicit `NullPool` test under concurrent app+scheduler workload |
| Knowledge FTS Czech-diacritic regression on PG | Medium | Medium | Phase 7 contract test must pass before Phase 8 includes `knowledge` |
| Usage backend split-brain on PG-state deploys | **High** today | **High** today | Phase 6 named decision; **disable PG-state for any new install until Phase 6 ships** |
| `src/db.py` lifecycle helper accidentally deleted | Medium | High (loses crash recovery) | Phase 10 split — DDL ladder vs lifecycle helpers as explicit checklist |
| Chat fork semantics drift during repo move | Medium | Medium | Phase 5 transaction-parity contract test pinned BEFORE move |
| Migration-tool rework breaks deploy ordering | Medium | High | Phase 11 dry-run + integration test of full state-machine transition |
| Lint baseline allowlist creeps upward | Low | Low | CODEOWNERS gate on `tools/lint/sql_allowlist.txt`; shrink-only diff check in CI |
| DuckDB-Quack ships before Tranche 3 done | Medium | Variable | Tranche 1 ships standalone value; Tranche 3 paths re-evaluated post-Quack |
| Cost overrun on Tranche 1 | Medium | Low | Quick wins parallelizable across people; Phase 2 spike runs in parallel with Phase 1 |

---

## Sequencing summary (v2)

| Phase | What | PRs | Weeks | Depends on |
|---|---|---:|---|---|
| **Tranche 1 — invariant cleanup** | | | **4–5** | |
| 0 | Baseline lint rule + allowlist | 1 | 1d | — |
| 1 | Coverage matrix + quick wins (~30 hits) | ~10 | 1.5w | 0 |
| 2 | duckdb-engine spike (PARALLEL) | 1 throwaway | 1.5w | — |
| 3 | Slack-binding new repo + models + contract | 1 | 0.5w | 0, 1a (coverage matrix) |
| 4 | Facet + aggregate routes (~25 hits) | ~10 | 1.5w | 0 |
| 5 | Chat facade redesign + move (44 hits, 31+13) | 2 | 1w | 0, 4 (some shared repos) |
| **Tranche 2 — boundary resolution** | | | **2** | |
| 6 | Usage backend decision doc + tests | 1 | 1w | 1a |
| 7 | Knowledge FTS contract + PG search shipping | 1 | 1w | 1a |
| **Tranche 3 — ORM collapse (GATED on Phase 2)** | | | **3–6** | |
| 8 | Cluster-by-cluster ORM (~14 clusters) | ~14 | 2.5–5w | 2 GREEN, 6 done, 7 done |
| 9 | Sweep dead repo files | 1 | 0.5w | 8 |
| 10 | Retire src/db.py DDL ladder (keep lifecycle helpers) | 1 | 0.5w | 8, 9 |
| 11 | Factory simplification + migration-tool rework | 1 (multi-commit) | 1w | 10 |

**Total**: ~9–13 engineer-weeks if Tranche 3 ships. **~4–7 weeks** if Tranche 3 stops at Phase 2 spike fail (invariant still locked).

---

## Per-table coverage matrix (replaces "15 files" framing)

**Adversary fix HIGH-8.** Source of truth for completeness: this table, not file counts. Populated during Phase 1a coverage-matrix PR; reproduced here as a sketch.

| Table | Model class | DD repo | PG repo | Contract test | Cluster phase |
|---|---|---|---|---|---|
| `users` | `User` (src/models/__init__.py) | `users.UserRepository` | `users_pg.UsersPgRepository` | `test_users_contract.py` ✓ | 8 (cluster 1) |
| `user_groups` | `UserGroup` | `user_groups.UserGroupsRepository` | `user_groups_pg.UserGroupsPgRepository` | `test_rbac_contract.py` ✓ | 8 (cluster 2) |
| `user_group_members` | `UserGroupMember` | `user_group_members.UserGroupMembersRepository` | `user_group_members_pg.UserGroupMembersPgRepository` | `test_rbac_contract.py` ✓ | 8 (cluster 2) |
| `resource_grants` | `ResourceGrant` | `resource_grants.ResourceGrantsRepository` | `resource_grants_pg.ResourceGrantsPgRepository` | `test_rbac_contract.py` ✓ | 8 (cluster 2) |
| `audit_log` | `AuditLog` | `audit.AuditRepository` | `audit_pg.AuditPgRepository` | `test_audit_contract.py` ✓ | 8 (cluster 1) |
| `table_registry` | `TableRegistry` | `table_registry.TableRegistryRepository` | `table_registry_pg.TableRegistryPgRepository` | (gap — schedule contract) | 8 (cluster 4) |
| `sync_state` | `SyncState` | `sync_state.SyncStateRepository` | `sync_state_pg.SyncStatePgRepository` | (gap) | 8 (cluster 4) |
| ... (continues for 62 tables) | | | | | |

The full matrix lands as a CSV/markdown file in the Phase 1a PR: `docs/planning/orm-coverage-matrix.md`. Coverage gate fails CI iff:
- A `_REGISTRY` key has no row in the matrix, OR
- A row points to a non-existent test/file, OR
- A model exists in `src/models/` with no row.

---

## Fact-check verdict (v2)

| Claim | Reality |
|---|---|
| "No raw SQL hardcoded except in the analytical part" today | **REFUTED** — 65 bug-class spots |
| Tranche 1 (~5w) delivers the invariant standalone | **YES** — baseline lint + 65 lifts; no new dep required |
| Tranche 3 ORM collapse cost is firm | **NO — gated on Phase 2 spike**. If spike fails, Tranche 3 doesn't start |
| Postgres becomes mandatory | **NO** — `duckdb-engine` is the laptop/dev path. If it fails the spike, dual-repo continues |
| DuckDB stays for analytics | **YES** — explicit fence; `src/db.py` lifecycle helpers kept |
| Cross-engine contract tests safety net | **PARTIAL today** — knowledge + slack + chat clusters explicitly addressed in Phase 1a coverage gate + Phases 3, 5, 7 |
| `src/db.py` retires entirely | **NO** — only the DDL ladder. WAL/snapshot/CHECKPOINT helpers stay |

---

## Audit scripts (appendix)

Used by Phase 0 lint + Phase 1a coverage matrix:

```bash
# Find all execute() call sites outside the allowlist.
# (AST-based; pseudo-code — actual implementation in tools/lint/sql_check.py)
python tools/lint/sql_check.py --allowlist tools/lint/sql_allowlist.txt

# Find every model class.
rg -n '__tablename__\s*=' src/models/*.py

# Find every registry key (handles typed AND plain assignment).
rg -nP '^\s+"[a-z_]+":\s*\{' src/repositories/__init__.py

# Compare to actual repo class list.
rg -nP 'class\s+[A-Z][A-Za-z]*Repository' src/repositories/*.py
```

---

## Appendices

- **`docs/planning/orm-migration-adversary-review.md`** — Codex adversary review v1, the source of every v2 fix
- **`docs/planning/agnes-orm-inventory-src.md`** — file-by-file `src/` inventory
- **`docs/planning/agnes-orm-inventory-app.md`** — file-by-file `app/` inventory
- **`docs/planning/agnes-orm-inventory-cli-conn-svc.md`** — file-by-file `cli/`, `connectors/`, `services/`, `scripts/` inventory
- **`docs/planning/agnes-orm-rawsql-audit.md`** — 73 numbered raw-SQL findings

Future appendices (land with the phases that produce them):
- `docs/planning/duckdb-engine-spike-report.md` — Phase 2 deliverable
- `docs/planning/usage-backend-decision.md` — Phase 6 deliverable
- `docs/planning/orm-coverage-matrix.md` — Phase 1a deliverable
