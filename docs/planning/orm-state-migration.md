# ORM-on-state migration plan

**Date**: 2026-06-04
**Branch**: `vr/orm-migration-plan` (off `origin/main` @ `46943e9b`)
**Status**: research + plan, no code change

## Goal

Eliminate raw SQL from non-repo code paths. Single SQLAlchemy ORM layer for application state (today: 37 DuckDB repos + 41 Postgres mirrors via a declarative `_REGISTRY` factory at `src/repositories/__init__.py`). Same models run on DuckDB or Postgres via the dialect layer (`duckdb-engine` for DuckDB, `psycopg` for PG). DuckDB stays for analytics: `analytics.duckdb`, `extract.duckdb`, BQ extension, parquet views, FTS extension.

## TL;DR

User invariant under inspection: **"there should be no raw SQL hardcoded except in the analytical part"**.

**Verdict: REFUTED today, structurally achievable.**

- 73 raw-SQL spots outside `src/repositories/`.
- 65 bug-class (state tables).
- 17 acceptable analytics escapes (BQ extension, parquet, FTS, `SELECT 1`, `information_schema`, session settings).
- 1 needs-discussion (admin LLM-generated SQL surface).
- 2 misplaced repository-pattern files (`app/chat/persistence.py`, `app/secrets_vault.py`).

Migration shape: **retire DuckDB-branch repos, keep PG-branch on `src/models/` declarative, run both backends through the same models via SQLAlchemy dialect dispatch**. Postgres is not mandatory — `duckdb-engine` carries the laptop/dev case.

Cost: ~5–8 engineer-weeks. Risk: `duckdb-engine` maturity (single-week spike gates the rest).

---

## Current architecture

### What already exists

- **`src/models/` (15 files)** — full declarative SQLAlchemy mapping for every state table. Already the destination for the PG repos. **No green-field model authoring needed.**
- **`src/repositories/__init__.py::_REGISTRY`** (PR #547, 2026-06-04) — declarative `key → {backend: (module, class)}` table. Adding a new backend (e.g. `DUCKDB_QUACK`, fall 2026) is a one-column change. Per-repo dispatch already trivial.
- **`src/db_state_machine.py::BackendState`** — `StrEnum { DUCKDB, SIDE_CAR, CLOUD, DUCKDB_QUACK }` + in-progress variants + migration transition validator. Backend evolution already first-class.
- **`tests/db_pg/test_*_contract.py`** — 15 cross-engine contract tests. Mandated safety net.
- **`docker-compose.postgres.yml`** + `scripts/db_state_migrator.py` + `scripts/migrate_duckdb_to_pg/tasks.py` — operational migration pipeline DuckDB → PG, with admin UI surface (`/admin/db/*`).
- **`tests/test_repository_registry.py` + `test_repo_method_parity.py`** — structural parity gates.

### What this plan changes

- Collapse `src/repositories/x.py` (DuckDB) + `src/repositories/x_pg.py` (PG) → single `src/repositories/x.py` running through SA on either dialect.
- Add `duckdb-engine` to `pyproject.toml`.
- Lift 65 caller-side raw-SQL spots into repo methods.
- Move 2 misplaced repos (`app/chat/persistence.py`, `app/secrets_vault.py`) into `src/repositories/`.
- Add 1 brand-new repo + 3 brand-new models for `services/slack_bot/binding.py` (slack_binding_codes / issue_log / redeem_log — currently no repo at all).
- Retire `src/db.py::_v1_to_v(N)` DuckDB schema ladder. Single Alembic ladder becomes source of truth.
- Add lint rule that fails CI on `conn.execute(<SQL string literal>)` outside the explicit allowlist.

---

## Inventory summary

Sources: parallel research agents on 2026-06-04 producing:

- `/tmp/agnes-orm-inventory-src.md` (149 files under `src/`)
- `/tmp/agnes-orm-inventory-app.md` (132 files under `app/`)
- `/tmp/agnes-orm-inventory-cli-conn-svc.md` (197 files under `cli/`, `connectors/`, `services/`, `scripts/`)
- `/tmp/agnes-orm-rawsql-audit.md` (73 raw-SQL findings outside repos)

### File counts

| Tree | Files | Migration touch |
|---|---:|---|
| `src/` | 149 | 37 DuckDB repos retire (collapsed into PG side) + `src/db.py` ladder retires |
| `app/` | 132 | 30 files contain raw state-SQL hits; 10 stay-as-is (analytics) |
| `cli/`, `connectors/`, `services/`, `scripts/` | 197 | 7 service/script files contain raw state-SQL; most files use repos already |
| `tests/` | (not enumerated, ~150 tests/) | contract tests stay green at every step |

### Category sweep across the repo

| Category | Count | Migration touch |
|---|---:|---|
| state-repo (DuckDB CRUD, `src/repositories/*.py`) | 37 | **retire** branch, keep PG → single ORM |
| state-repo-pg (Postgres mirror) | 41 | **collapse with DuckDB sibling, becomes the only repo** |
| state-other (`src/db.py` schema ladder, `src/db_pg.py` engine, `src/db_state_machine.py`) | 4 | `db.py` ladder retires; `db_pg.py` stays; `db_state_machine.py` stays |
| analytics (DuckDB-pinned) | ~10 (`orchestrator`, `profiler`, `remote_query`, `duckdb_conn`, `fts`, plus most of `app/api/v2_*.py`, `app/api/query*.py`, `cli/commands/explore.py`, `cli/commands/query.py`, `connectors/*/extractor.py`) | **keep-as-is** |
| marketplace-server | 6 | `marketplace_filter.py` already factory-routed |
| infra (DB-agnostic helpers, validators, config) | ~80 | no touch |
| service-glue, cli-glue | ~60 | most files already use repos; relink imports |
| models (`src/models/*.py`, SA declarative) | 15 | **the migration target — already there** |
| dead | 0 | — |

---

## Raw-SQL audit — the fact check

**73 total spots outside `src/repositories/`, `migrations/`, `tests/`, `src/db.py`, `src/db_pg.py`, `src/fts.py`, `src/orchestrator.py`, `connectors/*/extractor.py`.**

### Bug-class (65 spots) — must move into repos

Files with state-table raw SQL, sorted by hit count:

| File | Hits | Tables touched | Verdict |
|---|---:|---|---|
| `app/chat/persistence.py` | 31 | chat_sessions, chat_messages, chat_session_participants, user_workdirs | **misplaced repo** — move to `src/repositories/chat.py` |
| `app/secrets_vault.py` | 13 | mcp_secrets, system_secrets, mcp_user_secrets | **misplaced repos** — move to `src/repositories/{mcp_secrets,system_secrets,mcp_user_secrets}.py` |
| `app/web/router.py` | 12 | various small COUNT/SELECT 1 helpers | lift into existing repos |
| `app/api/marketplace.py` | 12 | usage_marketplace_item_*, store_entities, users, user_plugin_optouts | new `usage_marketplace_repo` + lift owner/subscriber queries |
| `app/api/observability.py` | 11 | audit_log, user_observability_views | new `audit_repo().facets_for_window()` + use existing `observability_views_repo` |
| `app/api/admin_usage.py` | 8 + 1 unclear | usage_events, usage_session_summary, usage_tool_daily, usage_marketplace_item_*, session_processor_state | `usage_repo` orchestration methods (`reprocess_all`, `prune_older_than`, `export`); admin LLM-SQL surface flagged separately |
| `app/api/memory.py` | 8 | user_group_members, user_groups, resource_grants, knowledge_votes, audit_log, memory_domains | lift into existing repos |
| `app/api/access.py` | 7 | user_group_members, resource_grants, user_stack_subscriptions | cascade-delete + transactional downgrade-fanout repo methods |
| `app/api/marketplaces.py` | 7 | marketplace_plugins, resource_grants, user_groups | mark-system fanout into repo |
| `app/api/me_stats.py` | 7 | usage_session_summary, session_processor_state | new aggregate repo methods (daily / by_model / top_sessions / totals) |
| `app/resource_types.py` | 7 | per ResourceType projection delegates | lift to repos |
| `app/api/activity.py` | 5 | audit_log, sync_history, session_processor_state | `audit_repo().latest_scheduler_tick()`, `sync_history_repo().status_counts_since()`, `session_processor_state_repo` |
| `app/api/health.py` | 5 | users, audit_log, session_processor_state, schema_version | repos (+ `SELECT 1` stays raw) |
| `app/api/store.py` | 4 | store_entities | `synthetic_name_collision`, `revert_archive` |
| `app/api/admin.py` | 4 | sync_state, sync_history, store_submissions | repo methods on existing repos |
| `app/api/admin_user_sessions.py` | 3 | usage_session_summary, users, audit_log | repo methods |
| `app/auth/providers/email.py` | 3 | setup_tokens / pending_codes | **CAS pattern** — needs careful ORM method design with row-count check |
| `app/auth/providers/password.py` | 2 | personal_access_tokens | same CAS shape |
| `app/api/me.py` | 1 | users + usage_session_summary CTE | boundary case (state ⋈ rollup) |
| `app/api/chat_copresence.py` | 1 | users | `users_repo().get_by_email()` exists |
| `app/api/my_stack.py` | 2 | marketplace_plugins, user_curated_subscriptions | lift to repos |
| `app/api/sync.py` | 1 | users | lift to repo |
| `app/api/me_debug.py` | 1 | user_group_members | lift to repo |
| `app/chat/audit.py` | 1 | audit_log | `audit_repo().log()` |
| `app/chat/copresence_summary.py` | 1 | chat_sessions | repo |
| `services/session_processors/usage_lib.py` | 6 | usage_events, usage_tool_daily, usage_marketplace_item_*, session_processor_state, marketplace_plugins, store_entities | **biggest single block** — rollup INSERT-SELECT; design choice (raw `Session.execute(text(...))` ok or rewrite as SA Core) |
| `services/slack_bot/binding.py` | (many) | slack_binding_codes, slack_binding_issue_log, slack_binding_redeem_log | **no repo exists** — add 3 models + `SlackBindingRepository` |
| `services/slack_bot/events.py` | 1 | users | repo |
| `services/session_pipeline/runner.py` | 1 | users | repo |
| `services/verification_detector/__main__.py` | 1 | session_processor_state | repo |
| `src/claude_md.py` | 3 | table_registry, metric_definitions, marketplace_registry | lift to repos |
| `src/rbac.py` | 2 | resource_grants, user_group_members | lift to existing `resource_grants_repo` |
| `src/store_guardrails/purge.py` | 1 | store_submissions | repo |
| `src/store_guardrails/reaper.py` | 1 | store_submissions | repo |
| `connectors/internal/registry.py` | 1 | table_registry | repo |

### Acceptable analytics escapes (17 spots) — stay raw on purpose

- `app/api/mcp_per_table.py:69, 134` — `DESCRIBE` + filtered SELECT against analytics views.
- `app/api/health.py:365` — `SELECT 1` liveness ping.
- `src/duckdb_conn.py:46, 54` — `SET GLOBAL TimeZone='UTC'` + probe.
- `src/remote_query.py:366` — analyst remote-query SQL into BQ-attached DuckDB.
- `connectors/internal/access.py:256, 336` — `information_schema` catalog scan + per-request ephemeral DuckDB CREATE TABLE.
- `cli/commands/explore.py:47, 55` + `cli/commands/query.py:90` — CLI analytics introspection.
- `cli/mcp/server.py:203-204` — MCP query wrapping.
- `cli/lib/pull.py:699-731` — analyst-local DuckDB view rebuild.
- `connectors/bigquery/access.py`, `connectors/bigquery/metadata.py`, `connectors/keboola/access.py`, `connectors/mcp/*.py` — extension lifecycle + TVF dispatch.
- `connectors/jira/extract_init.py` + `connectors/jira/scripts/consistency_check.py` — extract.duckdb DDL + parquet consistency.
- `src/profiler.py` — analytics data profiler.
- `scripts/build_demo_extract.py`, `scripts/duckdb_manager.py`, `scripts/generate_sample_data.py` — extract.duckdb / parquet seed.
- `app/api/v2_sample.py`, `v2_scan.py`, `v2_schema.py` — BQ extension calls + parquet DESCRIBE.

### Needs-discussion (1 spot)

`app/api/admin_usage.py:286` — admin "ask LLM → SQL → execute" surface. SQL validated by `validate_select_only(sql)` then handed to `conn.execute(validated_sql)`. No clean ORM signature for "execute arbitrary user-supplied SELECT". Options:

a. Keep raw with validator as the seam (current). Lint allowlist this single line.
b. Add a thin `RawValidatedQueryRepository.run(validated_sql) → rows` wrapper so lint rule doesn't need a file-specific exemption.

Recommend (a) — the validator is the security boundary; wrapping it in a repo adds nothing.

### Boundary: state-rollup tables that read like analytics

`usage_events`, `usage_session_summary`, `usage_tool_daily`, `usage_marketplace_item_daily`, `usage_marketplace_item_window` are written transactionally by the session processor but read as a small analytical warehouse (DuckDB `COPY TO PARQUET` for export, time-window aggregates, dimension cross-tabs).

**Recommendation: keep `usage_*` on DuckDB even after the ORM migration, OR migrate to PG with `COPY usage_events TO STDOUT (FORMAT csv/binary)` + Python parquet write.** Either is defensible; the decision belongs to Phase 4 of the plan (after the spike).

---

## Architecture target

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
│  DATABASE_URL=duckdb+quack:///... (fall 2026)              │
│    → duckdb-quack SA dialect → DuckDB-Quack                │
│                                                            │
│ Same models. Same queries. SA dialect normalizes           │
│ DISTINCT/ORDER BY/RETURNING/DDL.                           │
└────────────────────────────────────────────────────────────┘
┌─ Analytics ────────────────────────────────────────────────┐
│ Raw DuckDB python client (unchanged)                       │
│   analytics.duckdb, ATTACH extract.duckdb,                 │
│   BQ extension, Keboola extension, FTS extension,          │
│   read_parquet views, COPY TO PARQUET                      │
└────────────────────────────────────────────────────────────┘
```

### What "no raw SQL" means in this plan

| Layer | Raw SQL allowed? | Why |
|---|---|---|
| `src/repositories/*.py` | ✅ via SA Core / ORM query API | Some queries need dialect-specific tuning; SA `text()` is a controlled escape |
| `src/models/*.py` | ✅ table/column DDL (declarative) | The DDL IS the schema definition |
| `migrations/versions/*.py` (Alembic) | ✅ | Migration DSL |
| `src/db.py` (analytics half) | ✅ DuckDB-only | Analytics engine bootstrap |
| `src/orchestrator.py`, `src/profiler.py`, `src/remote_query.py`, `src/duckdb_conn.py`, `src/fts.py` | ✅ DuckDB-only | Analytics path |
| `connectors/*/extractor.py`, `connectors/*/access.py` | ✅ DuckDB-only | Extract.duckdb producers + extension lifecycle |
| `app/api/v2_*.py`, `app/api/query*.py`, `app/api/data.py`, `app/api/catalog.py`, `app/api/mcp_per_table.py`, `app/api/bq_metadata_refresh.py`, `app/api/jira_webhooks.py`, `app/api/admin_bigquery_test.py`, `app/api/admin_keboola_test.py` | ✅ DuckDB-only | Analytics route layer |
| `cli/commands/explore.py`, `cli/commands/query.py`, `cli/mcp/server.py`, `cli/lib/pull.py` | ✅ DuckDB-only | Analyst-side analytics |
| `scripts/build_demo_extract.py`, `scripts/duckdb_manager.py`, `scripts/generate_sample_data.py` | ✅ DuckDB-only | Analytics tooling |
| **Everywhere else** | ❌ | Lint rule enforces |

The lint allowlist becomes the canonical "this file is in the analytical part" registry.

---

## Migration phases

Each phase ships as one or more PRs. Each PR keeps the test suite green; cross-engine contract tests (`tests/db_pg/test_*_contract.py`) are the per-cluster gate.

### Phase 0 — lint rule + acceptable-escape registry (small PR)

**Goal**: lock in the invariant going forward. Cost-free; catches regressions.

- Add an AST-based pre-commit / CI rule that fails on `conn.execute(<string-literal>)` or `sa.text(<string-literal>)` in any file NOT in the allowlist.
- Allowlist:
  - `src/repositories/`
  - `src/models/`
  - `migrations/versions/`
  - `tests/`
  - `src/db.py`, `src/db_pg.py`, `src/duckdb_conn.py`, `src/fts.py`, `src/orchestrator.py`, `src/profiler.py`, `src/remote_query.py`
  - `connectors/`
  - `scripts/`
  - The analytics-route files enumerated above
- Allowed literals everywhere (even outside allowlist): `SELECT 1`, `BEGIN`, `COMMIT`, `ROLLBACK`, `SET …`, `INSTALL …`, `LOAD …`, `DESCRIBE …`, `PRAGMA …`, `CHECKPOINT`.
- Allowed because they hit `information_schema` (catalog reads): `information_schema.tables`, `information_schema.columns`.

**Outcome**: a new bug-class spot cannot land. Existing 65 spots fail until lifted into repos.

### Phase 1 — quick wins (lift to existing repos)

**Goal**: ~30 of the 65 bug-class spots have a clear repo home and need no new method (or a one-liner method).

Per-cluster PRs, each PR ships one file or one tight cluster:

- `app/chat/audit.py` → `audit_repo().log()` (1 hit)
- `app/chat/copresence_summary.py` → `chat_session_repo().title_for()` (1 hit)
- `app/api/chat_copresence.py` → `users_repo().get_by_email()` (1 hit)
- `app/api/sync.py` → `users_repo().mark_last_pull(user_id, ts)` (1 hit)
- `app/api/me_debug.py` → `user_group_members_repo().google_sync_summary()` (1 hit)
- `app/api/store.py` → `store_entities_repo().synthetic_name_collision()` + `.revert_archive()` (2 hits)
- `app/api/my_stack.py` → existing repos (2 hits)
- `app/api/admin_user_sessions.py` → `usage_session_summary_repo().for_user()`, `users_repo().get()`, `audit_repo().count_for_user()` (3 hits)
- `src/store_guardrails/purge.py` → `store_submissions_repo().purgeable()` (1 hit)
- `src/store_guardrails/reaper.py` → `store_submissions_repo().reap_stuck_reviews()` + `.mark_review_error()` (2 hits)
- `src/claude_md.py` → `table_registry_repo().list_all()` / `.list_by_ids()`, `metric_definitions_repo().category_counts()`, `marketplace_registry_repo().names_by_ids()` (3 hits)
- `src/rbac.py` → `resource_grants_repo().has_grant_for_user()` (2 hits)
- `connectors/internal/registry.py` → `table_registry_repo().prune_internal_except()` (1 hit)
- `services/slack_bot/events.py` → `users_repo().get_by_slack_id()` (1 hit)
- `services/session_pipeline/runner.py` → `users_repo().get()` (1 hit)
- `services/verification_detector/__main__.py` → `session_processor_state_repo().delete()` (1 hit)
- `app/web/router.py` → ~12 small COUNT / SELECT 1 helpers (one PR per cluster of related ones)

**Outcome**: ~30 hits gone. Lint rule starts passing on those files.

### Phase 2 — facet + aggregate routes (medium repo work)

**Goal**: lift composite queries into new repo aggregate methods.

- `app/api/activity.py`: `audit_repo().latest_scheduler_tick()`, `sync_history_repo().status_counts_since()`, `audit_repo().distinct_active_users()`, `session_processor_state_repo().verification_summary()`.
- `app/api/health.py`: composite `health_repo` or extend existing repos (`SELECT 1` stays raw with explicit allowlist).
- `app/api/observability.py`: `audit_repo().facets_for_window(since, scheduler_actions)` (one composite method replaces 5 raw SELECTs); `observability_views_repo().count_for_user()`, `.has(user_id, name)` (already exist — just use them).
- `app/api/memory.py`: `user_group_members_repo().group_names_for()`, `resource_grants_repo().granted_resource_ids_for_user(resource_type)`, `knowledge_votes_repo().for_user()`, `audit_repo().query(action_prefix=…)` (mostly already exists), `memory_domains_repo().slugs_by_ids()`.
- `app/api/access.py`: `user_group_members_repo().delete_all_for_group()` (exists), `resource_grants_repo().delete_all_for_group()` (exists), `user_stack_subscriptions_repo().downgrade_fanout(group_id, resource_type, resource_id)` (transactional INSERT … FROM JOIN).
- `app/api/marketplaces.py`: `marketplace_plugins_repo().exists()`, `.mark_system()`, `.unmark_system()`; `user_groups_repo().all_ids()`; `marketplace_plugins_repo().delete_all_for_marketplace()` + `resource_grants_repo().delete_for_marketplace_prefix()`.
- `app/api/admin.py`: `sync_state_repo().error_and_sync_summaries()`, `sync_history_repo().delete_for_table()`, `sync_state_repo().delete_for_table()`, `store_submissions_repo().delete()`.
- `app/api/me.py`, `app/api/me_stats.py`: `users_repo().enrich_self()` (CTE), `usage_session_summary_repo().daily()`, `.by_model()`, `.top_sessions()`, `.totals()`.
- `app/resource_types.py`: lift each projection delegate to its respective repo.

**Outcome**: ~25 more hits gone. App-layer state SQL effectively eliminated outside the remaining clusters.

### Phase 3 — CAS + transactional patterns (careful design)

**Goal**: race-free token/grant patterns need explicit return-count signatures.

- `app/auth/providers/email.py`: `setup_tokens_repo().consume(token, now) → bool` returning `True` iff exactly one row updated; CAS via `WHERE token=? AND consumed_at IS NULL RETURNING id`. Same shape on PG (`UPDATE … RETURNING id`) and DuckDB (`UPDATE … RETURNING 1` available since DuckDB 0.9).
- `app/auth/providers/password.py`: same shape for `personal_access_tokens.last_used_at` update.

**Outcome**: 5 more hits gone, race-free behavior preserved cross-engine.

### Phase 4 — misplaced repos (move into `src/repositories/`)

**Goal**: bring the two repository-pattern-but-wrong-location files into the canonical home.

- `app/chat/persistence.py` (31 hits, ~600 LOC of dual-backend repo logic) → split into:
  - `src/repositories/chat_sessions.py` + `chat_sessions_pg.py` (PG sibling already exists)
  - `src/repositories/chat_messages.py` + `chat_messages_pg.py` (PG sibling already exists)
  - `src/repositories/chat_session_participants.py` + `_pg.py` (already exists)
  - `src/repositories/user_workdirs.py` + `_pg.py` (already exists)
  - Each registers in `_REGISTRY`; callers go through factory.
  - Cross-engine contract tests added for the 4 new repos.
- `app/secrets_vault.py` (13 hits, 3 repository classes) → split into:
  - `src/repositories/mcp_secrets.py` (`SharedSecretsRepository`)
  - `src/repositories/system_secrets.py` (`SystemSecretsRepository`)
  - `src/repositories/mcp_user_secrets.py` (`PerUserSecretsRepository`)
  - PG mirrors via existing `secrets_vault_pg.py` content.

**Outcome**: 44 more hits gone. Two largest single-file bug-class concentrations resolved.

### Phase 5 — services with no repo

**Goal**: add the missing `services/slack_bot/binding.py` repo + models.

- New models in `src/models/slack.py`: `SlackBindingCode`, `SlackBindingIssueLog`, `SlackBindingRedeemLog`.
- New repo `src/repositories/slack_bindings.py` (DuckDB) + `slack_bindings_pg.py` (PG).
- Cross-engine contract test.
- Register in `_REGISTRY`.
- Rewrite `services/slack_bot/binding.py` to call the repo.

**Outcome**: last service-layer bug-class concentration gone. App-state raw SQL count outside repos drops to ~0.

### Phase 6 — usage rollup design decision

**Goal**: decide state-vs-analytics line for `usage_*` tables.

Boundary case from the audit: `services/session_processors/usage_lib.py` rebuilds rollups via DELETE/INSERT-SELECT inside one transaction. `app/api/admin_usage.py` exports via DuckDB `COPY TO PARQUET`.

Two paths:

a. **Keep `usage_*` on DuckDB even under PG-state world.** State path runs through PG models for `users`/`groups`/etc.; `usage_*` stay in the DuckDB system DB. `Session.execute(text(...))` for the rollup INSERT-SELECT remains acceptable (analytics-shaped). The current dual-backend `usage_pg.py` mirror gets demoted to "future option", or kept as the PG escape hatch for cloud deployments that prefer all-PG.

b. **Move `usage_*` to PG fully.** Replace `COPY TO PARQUET` export with `COPY usage_events TO STDOUT (FORMAT csv/binary)` + Python parquet write. Rewrite rollup INSERT-SELECT as SA Core query. Doable but a meaningful chunk of work.

**Recommendation: (a). Defer (b) until DuckDB-Quack ships (fall 2026) and the case for all-PG state weakens further.**

Path (a) leaves ~8 raw SQL hits in `services/session_processors/usage_lib.py` and `app/api/admin_usage.py` permanently, but they're then explicit "analytical-tier" SQL and the lint allowlist covers them.

### Phase 7 — `duckdb-engine` spike + dialect dispatch

**Goal**: validate `duckdb-engine` (SA dialect for DuckDB) handles Agnes's schema + concurrency. Gate for Phase 8.

- Add `duckdb-engine` to `pyproject.toml`.
- Pick one cluster (`users` is smallest, well-tested): write `src/models/users.py` declarative model (already exists), wire `tests/db_pg/test_users_contract.py` to run against `duckdb+engine://` URL in addition to PG.
- Validate:
  - DDL roundtrips through Alembic on the DuckDB dialect.
  - JSON columns (`raw`, `params`, `doc_links`, etc.) behave correctly.
  - `RETURNING` clauses work for CAS patterns.
  - FK enforcement parity (`user_group_members.group_id → user_groups.id`).
  - Single-writer constraint: SA pool config (`NullPool` or `pool_size=1`) prevents DuckDB concurrent-write exceptions.
  - Connection lifecycle: re-open on DATA_DIR change (test fixtures swap dirs).
- Time-box: **5 days**. Sink-or-swim.

**Spike outcomes**:

| Outcome | Then |
|---|---|
| All green | Proceed to Phase 8 |
| Minor gaps (1-2 known issues with workarounds) | Proceed to Phase 8 with documented workarounds |
| Major gaps (txn semantics broken, FK enforcement missing, Alembic incompatible) | Stop. Stick with dual-repo + lint + contract tests. Revisit when DuckDB-Quack lands |

### Phase 8 — cluster-by-cluster ORM consolidation

**Goal**: collapse dual repos into single ORM-mapped repos.

Cluster order (small → large, validates pattern early):

1. `users`, `audit`, `access_tokens`, `setup_tokens`, `cli_auth_codes`, `profiles`
2. `user_groups`, `user_group_members`, `resource_grants` (RBAC core)
3. `claude_md_template`, `welcome_template`, `news_template`, `instance_templates`
4. `table_registry`, `sync_state`, `sync_history`, `sync_settings`, `view_ownership`, `column_metadata`, `bq_metadata_cache`
5. `notifications` cluster (telegram, pending_codes, script_registry)
6. `marketplace_registry`, `marketplace_plugins`
7. `store_entities`, `user_store_installs`, `user_curated_subscriptions`, `store_submissions`, `user_stack_subscriptions`
8. `data_packages`, `recipes`, `memory_domains`, `memory_domain_suggestions`
9. `personal_access_tokens`, `setup_banner`
10. `metric_definitions`, `table_profiles`
11. `session_processor_state`, `observability_views`
12. `mcp_sources`, `tool_registry`, `mcp_secrets`, `system_secrets`, `mcp_user_secrets`
13. `chat_sessions`, `chat_messages`, `chat_session_participants`, `user_workdirs`
14. `slack_bindings` (new from Phase 5)
15. **`knowledge`** (last — special-case FTS; see Phase 9)

Per cluster PR pattern:

- Drop `src/repositories/X.py` (DuckDB).
- Promote `src/repositories/X_pg.py` to `src/repositories/X.py` (drop `_pg` suffix); rewrite to use SA dialect-agnostic API (no `psycopg`-specific code).
- Update `_REGISTRY` so the cluster has only one backend entry (or use SA dialect dispatch internally).
- Add `tests/db_pg/test_<cluster>_contract.py` (or rename existing) to run against BOTH `duckdb+engine://` and `postgresql+psycopg://` URLs.
- Delete the DuckDB schema migration steps for this cluster from `src/db.py` (the Alembic ladder is now source of truth; existing instances already have the tables).

**Outcome**: 14 of 15 clusters retire the DD branch.

### Phase 9 — knowledge FTS escape hatch

**Goal**: knowledge search is the one real cross-dialect divergence.

Decision: how to deliver knowledge BM25 search on a Postgres-state instance?

a. **PG `tsvector + GIN + unaccent`.** Matches `strip_accents=1` semantics; ranking via `ts_rank_cd`. Native PG, no extension management. DuckDB instance still uses the `fts` extension. Repo method dispatches by dialect.
b. **DuckDB FTS satellite.** PG instance keeps a separate DuckDB file just for the FTS index, rebuilt from PG changes. Same SQL on both sides. Operational cost: a second moving part.
c. **External search service** (e.g. Tantivy / OpenSearch). Out of scope for this plan.

**Recommendation: (a)**. Pure DB layer, no operational ops. The `knowledge_pg.py` repo today already has CRUD; only the search query body changes per dialect.

Phase 9 ships:

- `src/models/knowledge.py` (exists) gets a `search_vector` computed column on PG (tsvector GENERATED ALWAYS AS …); DuckDB instance keeps the `fts` extension index.
- `src/repositories/knowledge.py` (the now-consolidated repo) holds two search-method branches: PG uses `tsvector @@ ts_query`, DuckDB uses `match_bm25`.
- Lint allowlist documents `src/fts.py` (DuckDB FTS extension management) as analytics-tier; the search-query SQL in the repo is the one cross-dialect raw block.
- Cross-engine contract test covers Czech-diacritics → match.

### Phase 10 — retire `src/db.py` schema ladder

**Goal**: single Alembic source of truth.

After every cluster has been ported (Phase 8 + 9 complete):

- Delete `_v1_to_v(N)` chain from `src/db.py`.
- Keep `get_analytics_db()`, BQ extension bootstrap, FTS install — those are analytics path.
- Delete `_ensure_schema` for the state tables (Alembic owns that now).
- `src/db.py` shrinks from 5565 LOC to ~1500 (analytics half + connection helpers).
- `tests/test_db_schema_version.py` integration gate updated to query Alembic head instead of `schema_version` table.

### Phase 11 — factory simplification

**Goal**: cleanup the registry once the dual-branch shape is gone.

- `_REGISTRY` collapses from `{DUCKDB: (...), PG: (...)}` per repo to `(module, class)` per repo (single backend).
- Backend dispatch lives in SQLAlchemy at the engine level (DATABASE_URL → dialect).
- `use_pg()` → `active_dialect()` returning the SA dialect name; still used by analytics paths that branch (DuckDB vs PG dialect-specific SQL like the knowledge search query).
- Migration tooling (`scripts/db_state_migrator.py`, `scripts/migrate_duckdb_to_pg/tasks.py`) re-tooled to iterate `Base.metadata.sorted_tables` and copy via SA Core.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `duckdb-engine` Alembic compatibility gap | Medium | High (blocks Phase 8) | Phase 7 spike gates; fallback to dual-repo if blocked |
| `duckdb-engine` JSON column edge cases | Medium | Medium | Spike covers JSON roundtrip explicitly |
| DuckDB single-writer + SA session pool | Low | Medium | SA pool config (`NullPool`) — already understood pattern |
| Migration cost overrun | Medium | Medium | Phase-by-phase, contract tests gate each PR |
| DuckDB-Quack lands fall 2026 and obsoletes the rewrite | Medium | Variable | Plan ships in <8 weeks (lint + Phases 1-5 land independently of duckdb-engine spike) |
| Existing instances mid-migration during PG cutover | Low | High | Existing `scripts/db_state_migrator.py` already handles per-table copy; gets re-tooled in Phase 11 |
| `knowledge` FTS Czech-diacritics regression on PG | Medium | Medium | `unaccent` extension + contract test |
| Lint rule false positives | Low | Low | Allowlist designed explicitly; iterate based on misses |

---

## Sequencing summary

| Phase | What | PRs | Effort | Depends on |
|---|---|---:|---|---|
| 0 | Lint rule + allowlist | 1 | 1d | — |
| 1 | Quick wins (~30 hits) | ~10 | 3-5d | 0 |
| 2 | Facet + aggregate routes (~25 hits) | ~10 | 5-7d | 0 |
| 3 | CAS patterns (5 hits) | ~3 | 2-3d | 0 |
| 4 | Misplaced repos (44 hits) | 2 | 5d | 0 |
| 5 | slack_bot/binding repo (new) | 1 | 2d | 0 |
| 6 | Usage rollup decision | (spike only, no code change) | 1d | — |
| 7 | duckdb-engine spike | 1 throwaway | 5d | — |
| 8 | Cluster-by-cluster ORM (~14 clusters) | ~14 | 3-4w | 7 green |
| 9 | Knowledge FTS | 1 | 5d | 8 done |
| 10 | Retire src/db.py ladder | 1 | 3d | 8, 9 done |
| 11 | Factory simplification | 1 | 2d | 10 done |

**Total**: ~7-9 engineer-weeks. Phases 0-6 (~2-3w) deliver standalone value and **lock the invariant** even if Phases 7-11 never ship.

---

## Fact-check verdict

| Claim | Reality |
|---|---|
| "No raw SQL hardcoded except in the analytical part" today | **REFUTED** — 65 bug-class spots outside repos |
| Migration is achievable | **YES** — `src/models/` already there, factory already declarative, contract tests already cover dual-backend |
| Postgres becomes mandatory | **NO** — `duckdb-engine` SA dialect carries DuckDB-only deploys |
| DuckDB stays for analytics | **YES** — explicitly fenced; ~10 files + connectors + extension paths preserved |
| Cost-effective in <8 weeks | **YES** — Phases 0-6 are independent quick wins, Phases 7-11 gated by duckdb-engine spike |
| Lint catches future drift | **YES** — Phase 0 ships standalone |

The work is incremental, gated by an explicit spike, reversible at each phase, and the first ~30% of value (Phases 0-3) ships in 2 weeks without needing any new dependency.

---

## Appendices

- **`/tmp/agnes-orm-inventory-src.md`** — file-by-file inventory of `src/` (149 files)
- **`/tmp/agnes-orm-inventory-app.md`** — file-by-file inventory of `app/` (132 files)
- **`/tmp/agnes-orm-inventory-cli-conn-svc.md`** — file-by-file inventory of `cli/`, `connectors/`, `services/`, `scripts/` (197 files)
- **`/tmp/agnes-orm-rawsql-audit.md`** — 73 numbered raw-SQL findings outside repos with verdicts
