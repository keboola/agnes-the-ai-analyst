# Platform Telemetry Epic — 2026-05-12

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`. Each phase is one PR-ready unit. Tasks within a phase share state and ship together.
>
> **Branch:** `zs/platform-telemetry` (stacked on `zs/spec-activity-center` schema v40 + clean `/admin/activity` rebuild)
> **Schema:** target v41 (one bump for the whole telemetry foundation)
> **Goal:** Boss directive + Activity Monitoring Plan (Downloads, 2026-05-09) merged into one executable program.

---

## What this delivers

Boss directive maps to:

| Boss bullet | Where it lands here | Status before this PR |
|---|---|---|
| Platform setup, how-to guides | Phase D | scattered docs, no consolidated playbook |
| Export telemetry | Phase C — `/api/admin/usage/export` + `agnes admin usage export` | nothing |
| Admin access to telemetry (prompts, tool, usage) | Phase B (`/admin/users/<id>` Sessions) + Phase C (export + ask) | `UsageProcessor` is no-op |
| **Prompt the telemetry** (option C — CLI Text-to-SQL) | Phase C — `agnes admin ask "..."` | nothing |
| Privacy mode | **Out of scope — Minas's `agnes mark-private` (#242, merged) already covers this** | shipped |
| Flea market (anyone uploads, guardrails only, no ACL) | **Out of scope — shipped via #233 store_entities + #234 enrichment** | shipped |

Plus the Activity Monitoring Plan (Downloads) substance:

| Plan task | This epic phase |
|---|---|
| §0 Schema v38 | Phase A.1 — **renumber to v41** |
| §1 Attribution explode | Phase A.2 |
| §2 UsageProcessor real extraction | Phase A.3 |
| §3 /marketplace stats wire | Phase B.1 |
| §4 /admin/users sessions section | Phase B.2 |
| §5 Reprocess + retention | Phase C.4 |
| §6 Docs + CHANGELOG | Phase D |

---

## Architecture invariants

- **Three-source taxonomy** for invocations: `curated` (curator-managed marketplace plugins), `flea` (analyst-uploaded store_entities), `builtin` (Anthropic-shipped Bash/Read/Edit/…).
- **Per-event row + per-session summary + daily rollup** — events for forensics, summary for the user-detail page, rollups for marketplace popularity queries.
- **Attribution explode at write time** (marketplace sync, store entity write) into `usage_attribution_*` tables. Processor does single-query attribution lookup, no scan-time.
- **Privacy** = analyst's per-session `agnes mark-private` decision (Minas's #242). Sessions marked private never upload → never reach `UsageProcessor` → no telemetry. No new privacy code needed.
- **Reprocess strategy** — `USAGE_PROCESSOR_VERSION` bump triggers `agnes admin usage reprocess` which DELETEs `session_processor_state` rows for `usage` only, leaves `verification` untouched (composite PK).
- **Retention** — `USAGE_EVENTS_RETENTION_DAYS` env var, default `0` = forever. Daily prune in scheduler.
- **Admin telemetry "ask"** — CLI sends the natural-language question + a schema digest + a few sample rows to Anthropic API (Claude Haiku, cheapest model that handles SQL well), gets SQL back, executes it read-only, prints both the SQL and the results. **Audit-logged.** No data leaves the server beyond the schema digest + the question itself.

---

## Phase A — Foundation (5 tasks)

### A.1: Schema v41 migration

DDL identical to Activity Monitoring Plan §0 but bumped to v41:

```sql
-- usage_events, usage_session_summary, usage_tool_daily, usage_plugin_daily,
-- usage_attribution_skills, usage_attribution_agents, usage_attribution_commands
-- See Activity Monitoring Plan — 2026-05-09 lines 92–199 for full DDL.
```

Files:
- `src/db.py:43` — bump `SCHEMA_VERSION = 41`
- `src/db.py` — add `_v40_to_v41(conn)` function with all 7 `CREATE TABLE IF NOT EXISTS` + indices, idempotent (`ADD COLUMN IF NOT EXISTS` not relevant here — these are new tables)
- `src/db.py` — extend `_SYSTEM_SCHEMA` with the 7 new tables for fresh installs
- `src/db.py` — ladder step `if current_version < 41: _v40_to_v41(conn)`
- Test: `tests/test_schema_v41_migration.py` — 6 tests like v40 (version bump, columns/tables exist, indices exist, idempotent, v30→v41 evolved DB, v40→v41 direct)

### A.2: Attribution explode

Activity Monitoring Plan §1 task 1.1–1.6 verbatim. Files:
- Create `src/repositories/usage_attribution.py` (new repo)
- Modify `src/marketplace.py` — call explode after `MarketplacePluginsRepository.replace_for_marketplace(...)`
- Modify `app/api/store.py` — call explode after entity create/approve/soft-delete (transactional)
- Create `scripts/backfill_usage_attribution.py` — first-deploy populator (idempotent)
- Tests: `tests/test_usage_attribution.py` — curated + flea + re-sync replaces + lookup precedence

### A.3: UsageProcessor real extraction

Activity Monitoring Plan §2 task 2.1–2.10 verbatim. Files:
- Create `services/session_processors/usage_lib.py` — `iter_events`, `AttributionLookup`, `compute_active_seconds`, `compute_summary`, `rebuild_rollups`
- Create `src/repositories/usage.py` — `UsageRepository` (`upsert_events`, `upsert_summary`, `purge_for_session`, `delete_older_than`)
- Modify `services/session_processors/usage.py` — replace no-op with pipeline. Add `USAGE_PROCESSOR_VERSION = 1` constant
- Modify `app/api/admin.py:3363` — `/api/admin/run-session-processor?processor=usage` already exists; after a successful `usage` run, call `rebuild_rollups`
- Tests: `tests/test_session_processor_usage.py` — pure tool_use / mcp / skill curated / skill flea / slash / subagent / error / mixed / empty / re-grown file (10 fixtures under `tests/fixtures/sessions/usage/`)
- Tests: `tests/test_usage_rollups.py` — seed events directly, call `rebuild_rollups`, assert rollup shapes

### A.4: (originally A.5) — skip cross-link with /admin/activity timeline

**Decision: no cross-link in v1.** `/admin/activity` is server-ops timeline (audit_log). Usage events have separate semantics + surfaces (`/marketplace`, `/admin/users/<id>`). Cross-link is a Phase 2 polish if operators ask. Documented under "Parked".

---

## Phase B — Telemetry surfaces (2 tasks)

### B.1: `/marketplace` Most Popular + stats

Activity Monitoring Plan §3 task 3.1–3.8 verbatim. Net of:
- `UnifiedItem` gains `invocations_30d`, `unique_users_30d`, `trend_pct`
- `GET /api/marketplace/items?sort=most_used|trending|recent`
- Uncomment Most Popular section in `marketplace.html`, render top-8 cards per tab
- Sort dropdown in filter row
- Per-card invocation chip + trend
- Detail-page sparkline (server-rendered SVG)
- Tests `tests/test_marketplace_telemetry.py` — card response shape, sort orders, hidden when empty, existing filters still work

### B.2: `/admin/users/<id>` Sessions section

Activity Monitoring Plan §4 task 4.1–4.7 verbatim. Net of:
- `GET /api/admin/users/{user_id}/sessions` — paginated list (50 default, 200 max)
- `GET /api/admin/users/{user_id}/sessions/{session_file}/download` — single JSONL stream, path-traversal guarded
- `GET /api/admin/users/{user_id}/sessions/download-all` — chunked zip, single audit row with `file_count` + `total_bytes`
- Sessions section in `admin_user_detail.html` — table + pagination + "Download all" button
- Tests `tests/test_api_admin_user_sessions.py` — pagination cap, path-traversal rejection, zip integrity, audit row, admin-only

---

## Phase C — Admin telemetry access (4 tasks)

### C.1: Export endpoint

```
GET /api/admin/usage/export?format=csv|json|parquet&since=YYYY-MM-DD&until=…&user_id=…&source=…
```

Streamed response. CSV uses standard library. Parquet via duckdb COPY TO (already a dependency).

- Files: `app/api/admin.py` (new endpoint) or new `app/api/admin_usage.py`
- Audit-logged: `usage.export` action with `params={format, since, until, row_count}`
- Tests: each format returns valid output, filters honored, admin-only

### C.2: `agnes admin usage export` CLI

Mirrors `agnes admin activity` pattern from `zs/spec-activity-center`. Subcommand of `agnes admin usage`.

- Files: `cli/commands/admin_usage.py`, register in `cli/commands/admin.py`
- Options: `--format`, `--since`, `--until`, `--user`, `--source`, `--out FILE` (else stdout)
- Tests: CLI runner with seeded server, valid output, error paths

### C.3: `agnes admin ask "..."` — Text-to-SQL

Two-step:
1. Server endpoint `POST /api/admin/usage/ask` with body `{question: str}` returns `{sql: str, rows: list[dict], duration_ms: int}`. Server-side LLM call (Anthropic Claude Haiku via existing provider abstraction in `services/corporate_memory/`) with a system prompt that:
   - Embeds `usage_events` / `usage_session_summary` schemas
   - Lists a few sample rows for grounding
   - Demands SELECT-only SQL — refuses INSERT/UPDATE/DELETE/DROP
   - Returns the generated SQL even on guard-rail rejection so operator sees what the LLM tried
2. Server validates the SQL is SELECT-only (parse with `sqlglot` or string-prefix sanity check), executes against DuckDB read-only, returns rows
3. Server **audits the question + generated SQL + row count** to `audit_log` action `usage.ask`

CLI: `cli/commands/admin_ask.py` — `agnes admin ask "kdo nejvíc používal /compound:ce-debug minulý týden?"` — sends to server, prints SQL + table of results.

LLM availability: if no provider configured, endpoint returns 503 with hint. CLI shows the message clearly.

- Files:
  - `app/api/admin_usage.py` — new endpoint `POST /api/admin/usage/ask`
  - `src/usage_ask.py` (or similar) — LLM prompt construction, SQL validator
  - `cli/commands/admin_ask.py` — CLI command
- Tests: 
  - Unit: SQL validator rejects mutations, accepts SELECT
  - Unit: prompt builder includes schema digest
  - Integration: end-to-end with a mocked LLM provider, assert returned SQL executes, rows returned
  - Admin-only

### C.4: Reprocess + prune

Activity Monitoring Plan §5 task 5.1–5.4 verbatim. Net:
- `POST /api/admin/usage/reprocess` — admin-only, DELETEs state + events, audit-logged
- `POST /api/admin/usage/prune` — admin-only, deletes events older than `USAGE_EVENTS_RETENTION_DAYS`
- Scheduler entry — `SCHEDULER_USAGE_PRUNE_INTERVAL` daily
- Tests: reprocess clears only `usage` state, prune respects retention

---

## Phase D — Docs (3 tasks)

### D.1: `docs/PLATFORM_SETUP.md` — operator playbook

Consolidates scattered setup docs into one ordered playbook:

1. First-time bootstrap (instance.yaml, OAuth, seed admin)
2. TLS / reverse-proxy (Caddy)
3. Marketplaces (curated + private repos, PAT secrets)
4. Scheduler (env vars per processor cadence)
5. Telemetry (UsageProcessor cadence, retention, export, ask)
6. Privacy posture (per-session `agnes mark-private`, server-side audit, PostHog opt-in)
7. Operator daily routine (`/admin/activity`, `/admin/users`, `agnes admin *` commands)

Replaces the implicit knowledge in `docs/QUICKSTART.md`, `docs/ONBOARDING.md`, `docs/DEPLOYMENT.md`, `docs/HEADLESS_USAGE.md`. The old docs stay but get a "see PLATFORM_SETUP.md" pointer at the top.

### D.2: `docs/HOWTO/` index — analyst guides

5 cookbook-style guides:

1. `docs/HOWTO/01-first-query.md` — `agnes pull`, `agnes catalog`, first SQL
2. `docs/HOWTO/02-snapshots-for-remote.md` — `agnes snapshot create`, when not to
3. `docs/HOWTO/03-private-session.md` — `agnes mark-private` flow, what it does, what it doesn't
4. `docs/HOWTO/04-feedback-and-ask.md` — `agnes admin ask` (admin) + how to report a problem
5. `docs/HOWTO/05-customizing-skills.md` — install/uninstall, flea market upload, guardrails

Plus `docs/HOWTO/README.md` as an index.

### D.3: CHANGELOG + spec doc

Single `[Unreleased]` section with `### Added` / `### Changed` / `### Internal` covering everything in Phases A–C, plus Phase D as `### Documentation`.

---

## Rigor — double review + double tests

User explicitly requested. Per task:

1. **Implementation subagent** writes the failing test FIRST, then implements, then verifies test passes (TDD).
2. **Spec compliance reviewer** subagent verifies the implementation matches what the spec asked for — nothing missing, nothing extra.
3. **Code quality reviewer** subagent checks for clarity, DRY, YAGNI, security smells, naming, file responsibility.
4. **E2E behavior reviewer** subagent — NEW step beyond `superpowers:subagent-driven-development` default. Runs the actual touchpoint end-to-end against a live test server (using existing fixtures from `tests/conftest.py`), confirms behavior matches the user-visible contract.

Per phase:
- After all tasks in a phase complete, dispatch one **phase integration reviewer** that exercises the full phase surface from outside (curl + CLI + DB inspection) and confirms inter-task coordination.

Per epic (at the very end):
- **Security review** across the whole diff (SQL injection, path traversal, LLM prompt injection in `agnes admin ask`)
- **Code architecture review** across the whole diff (file responsibility, repo boundaries, no drift)
- **End-to-end behavior review** — runs every CLI command + every web endpoint on a live server, screenshots `/admin/users/<id>` and `/marketplace` Most Popular, verifies admin can answer 3 realistic questions via `agnes admin ask`.

---

## Acceptance criteria

When all phases complete:

1. `usage_events` table populates from a single seeded session within one `run-session-processor` tick.
2. `/marketplace` shows a "Most Popular — last 30 days" section with at least one curated and one flea card after the seeded data is processed.
3. `/admin/users/<id>` Sessions section lists the seeded user's sessions with single-file download + "Download all (.zip)" button. Both writes `audit_log` rows.
4. `GET /api/admin/usage/export?format=csv&since=2026-05-01` returns valid CSV stream with all seeded events.
5. `agnes admin usage export --format json --out /tmp/out.json` writes a valid JSON file with the same rows.
6. `agnes admin ask "how many times was Bash invoked yesterday"` returns a SELECT statement + the answer row.
7. `agnes admin usage reprocess` clears `usage_events` + `session_processor_state` rows for `usage` only; verification rows untouched.
8. New operator opening `docs/PLATFORM_SETUP.md` can bootstrap a fresh Agnes instance with telemetry enabled in under 30 min.
9. Per-session `agnes mark-private` (Minas's #242) prevents the session from reaching `usage_events` — verified by a regression test.
10. Full pytest suite green; double-review trail recorded in git commits per task.

---

## Phasing & PR strategy

**One PR**, `zs/platform-telemetry`, stacked on `zs/spec-activity-center`. Each phase = one or more commits; tasks within a phase share state.

Order:
1. Phase A.1 (schema) — 1 commit
2. Phase A.2 (attribution) — 1–2 commits
3. Phase A.3 (UsageProcessor) — 2–3 commits
4. Phase B.1 (marketplace stats) — 1–2 commits
5. Phase B.2 (per-user sessions) — 1–2 commits
6. Phase C.1+C.2 (export endpoint + CLI) — 2 commits
7. Phase C.3 (`agnes admin ask`) — 2–3 commits
8. Phase C.4 (reprocess + prune) — 1 commit
9. Phase D.1+D.2+D.3 (docs + CHANGELOG) — 2–3 commits

Total: ~14–19 commits. Roughly 25–35 hours of subagent work.

---

## Out of scope (parked for v2)

- `/admin/usage` drill-down dashboard (Activity Monitoring Plan §parked)
- Cross-link of `usage_events` into `/admin/activity` timeline (decided in A.4)
- LLM friction tagging on events
- Bash regex error classification
- Retry-loop detection
- Per-plugin version stats
- Per-user own-data dashboards (Resource type `OWN_USAGE`)
- Real-time push (WebSocket)
- "Users who used X also used Y" co-occurrence signal
- Flea market team directory (Boss Q3 — skip per directive)
- Anonymized telemetry mode (counts ok, content masked) — `agnes mark-private` covers the all-or-nothing case
