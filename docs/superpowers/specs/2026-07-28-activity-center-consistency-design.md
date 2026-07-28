# Activity Center & observability-surface consistency

**Date:** 2026-07-28
**Status:** approved design, pre-implementation
**Scope:** `/admin/activity` (Activity Center), `/admin/sessions` browser, `/admin/usage` (telemetry), `/admin/adoption`, and the repositories backing them.

## Problem

The four observability surfaces disagree with each other and with themselves.
Verified on a production instance (7-day window, ~19.7k audit events) and
against the code; every item below is reproduced, not hypothesized.

### Confirmed defects

| # | Defect | Evidence |
|---|--------|----------|
| 1 | Activity Center KPI cards ignore every filter — `/api/admin/observability/kpis` accepts only `since_minutes`, yet the UI re-fetches it on each filter change, so the cards *look* responsive while never changing. | `app/api/observability.py` (`kpis`), `app/web/templates/activity_center.html` (`loadKpis`) |
| 2 | The Result dropdown cannot reach 35% of rows: "success only" sends `result LIKE 'success'` (no wildcard), "errors only" `LIKE 'error%'`. Rows with `result IN ('ok', NULL, 'denied', 'blocked', 'invalid_password', 'deactivated', 'skipped')` match neither option. | `src/repositories/audit.py::query` (`result_pattern`); prod: 6,916 NULL + 74 `ok` of 19,762 |
| 3 | The Source filter is client-side only — it filters the 50 loaded rows while the dropdown shows server-side whole-window counts from `/facets`. Pagination cursors ignore it entirely. | timeline endpoint has no `source` param; `classifySource()` in the template |
| 4 | The page inflates its own numbers: each read logs `activity.read` into the very `audit_log` it renders (deduped to 60s, auto-refresh is 30s). Small volume (~18/24h idle) but a permanent bias. | `app/api/activity.py::_audit_read` |
| 5 | The Reset button does not clear the Resource filter (`resource_prefix` missing from the reset `Object.assign`). | `app/web/templates/activity_center.html` (`fReset` handler) |
| 6 | Four writer sites put an **email** into `audit_log.user_id` instead of `users.id`, splitting people into two facet/dropdown entries with identical labels (6 of 19 dropdown entries on prod were duplicates). | `app/chat/audit.py` (documented as intentional — contract changed by this design), `app/api/memory.py` (4 sites), `app/api/authoring_suggestions.py` (3 sites), `services/slack_bot/binding.py` |
| 7 | The facets' scheduler fallback list names four actions that no longer exist (`run_session_collector`, …); real scheduler ticks (`run_session_processor:*`, `run_jira_sla_poll`, …) carry **no `client_kind`** because the `run_*` audit writers in `app/api/admin.py` never pass it — even though `src/audit_helpers.py::client_kind_from_user` exists and 25 other sites use it. Result: the "other" source bucket absorbs scheduler traffic (6,904 rows on prod). | `app/api/observability.py::_SCHEDULER_ACTION_FALLBACK`; `app/api/admin.py` `run_*` writers |
| 8 | The p95 KPI card reads `duration_ms`, which exactly **one** call site in the codebase writes (`app/api/sync.py`, sync trigger). The card describes a sliver of traffic while sitting next to an "Events" card that counts everything. | grep: single `duration_ms=` writer |
| 9 | The sessions browser windows on `started_at`, so a session uploaded late (queue catch-up) never appears in any recent window — on prod, 34 of 157 files uploaded in 30 days had started before the 30-day cutoff. Admins read this as data loss. | `src/repositories/usage.py::_sessions_where` |
| 10 | "Active users" means three different populations under one label: audit KPI counts the scheduler system user (84% of events, 1 of 19 "users"), telemetry counts every surface, sessions counts only transcript pushers. Error rates likewise have three different denominators (0.7% / 5.3% / 8.7% side by side). | prod cross-check |

### Explicitly verified as NOT broken

- **Ingest completeness is 100%** — every uploaded session file has a
  `usage_session_summary` row. (An earlier "2 lost sessions" reading was a
  join error: the upload audit logs the *filename*, the browser keys on the
  content-derived `session_id`, and resumed/forked sessions differ. Any
  reconciliation must join on `session_file`.)
- **sessions == adoption** (same table, same anchor) — keep it pinned by a
  contract test.
- **telemetry ⊃ sessions** is correct by design (telemetry covers pull/chat/
  MCP surfaces too).
- Duplicate *user accounts* (same person, two `users` rows under different
  domains) are an operational cleanup, not a code defect; out of scope here.

## Decisions (made with the project owner)

1. **Duration**: instrument broadly — not per call site, but via a
   request-scoped contextvar: ASGI middleware records the request start;
   `audit_repo().log()` fills `duration_ms` automatically when the caller
   didn't pass one. One change covers every HTTP-triggered audit write.
2. **Self-logging**: keep writing `activity.read` (governance trail), but the
   page excludes AC's own read actions by default, with a visible
   "include Activity Center reads" toggle honored by KPIs, facets, and the
   table together.
3. **Window anchor**: sessions browser defaults to `uploaded_at` (arrivals);
   adoption keeps `started_at` (usage-over-time). Both expose an `anchor`
   override.
4. **Result vocabulary**: read-side `result_class` + normalize writers going
   forward (`"ok"` → `"success"`); no history rewrite.

## Design

### Phase A — identity & classification at write time

- **Writers pass `users.id`.** Fix the four email-writing sites; where only an
  email is available (Slack binding), resolve via `users_repo()`; if
  unresolvable, keep the email (better than dropping the row) — the read-side
  label logic already handles both.
- **Backfill migration** (DuckDB `_vN_to_v(N+1)` + Alembic, same endpoint):
  `UPDATE audit_log SET user_id = users.id` where `user_id` contains `'@'`
  and matches exactly one `users.email`.
- **`run_*` writers stamp `client_kind`** via the existing
  `client_kind_from_user(user)` helper.
- **One scheduler rule.** Replace `_SCHEDULER_ACTION_FALLBACK` with the rule
  `last_scheduler_tick` already uses (`action LIKE 'run_%' OR action =
  'marketplace.sync_all'`), extracted to a single shared constant used by
  facets SQL, timeline classification, and the health pulse.
- **Server-computed `source`.** The timeline returns a `source` field per row
  (same CASE as facets); the template stops re-deriving it in JS.

### Phase B — Activity Center query parity

- **Shared filter surface.** `facets` and `kpis` accept the same filters as
  the timeline (`user_id`, `action_prefix`, `resource_prefix`,
  `result_pattern`/`result_class`, `q`, `source`) via one WHERE-builder per
  backend (`src/repositories/audit.py` / `audit_pg.py`), extended in the same
  PR with a cross-engine contract test.
- **`result_class`** (SQL CASE, no schema change):
  `success` = `('success','ok')`; `error` = `LIKE 'error%'`;
  `denied` = `('denied','blocked','invalid_password','deactivated')`;
  `none` = NULL; `other` = the rest. The Result dropdown lists classes with
  facet counts; `result_pattern` stays for back-compat. The KPI error count
  uses the `error` class; a vocabulary guard test asserts every literal
  `result="…"` in the codebase maps to a known class.
- **`include_self_reads`** (default false) on timeline + kpis + facets,
  excluding `action = 'activity.read'`; the UI exposes the toggle.
- **`active_users` excludes system actors** (the scheduler system user);
  the events total stays unfiltered — the card label says "people".
- **Duration contextvar** (decision 1) in `src/audit_context.py` +
  middleware registration; both repo backends read it in `log()`.
  The p95 card now reflects all audited HTTP actions; its sub-label states
  coverage (`n of m rows carry duration`).
- **UI fixes**: Reset clears `resource_prefix` + `source`; sort headers note
  they sort the loaded page (existing comment becomes a visible hint).
- **CLI parity**: `agnes admin activity` gains `--source` and
  `--result-class`; the MCP foundation tool mirrors them (ratchet).

### Phase C — sessions arrival anchor + reconciliation

- **`uploaded_at` column** on `usage_session_summary` (both ladders).
  Stamped at ingest (`now()` on first processing); backfilled from
  `audit_log` `session.upload` rows joined on `session_file`, falling back
  to `started_at`.
- **`anchor=started|uploaded`** on `/api/admin/sessions/list|kpis|facets`;
  browser defaults to `uploaded`, adoption endpoints keep `started_at`
  internally. CLI flag `--anchor`.
- **Reconciliation in the health pulse**: distinct `session.upload`
  filenames (audit, window) vs `usage_session_summary` rows matched on
  `session_file`; a nonzero gap shows as `ingest lag: N pending` (yellow).
- **Browser shows both ids** when `session_id` ≠ file stem (resume/fork),
  so audit↔browser cross-referencing stops being a trap.

### Phase D — cross-surface glossary

- KPI cards name their population and denominator:
  "Active users (people)" / "N events (incl. system)" on audit;
  "of N tool calls" under telemetry error rate; "sessions with ≥1 tool
  error" under the sessions error stat.
- Contract test pinning `sessions/kpis == adoption/kpis` for the same window
  on both backends.

## PR slicing

One PR per phase (A → D), each with: both-backend repo changes + contract
tests, migration-ladder sync where schema changes (C, and A's backfill),
CHANGELOG bullet, release-cut per the release process. Phase D may fold into
B if it stays label-only.

## Testing

- Cross-engine contract tests for every new/changed repo method
  (`tests/db_pg/…`).
- Endpoint tests asserting **KPI == table** under each filter combination
  (the regression this whole design exists to prevent).
- Vocabulary guard test (writer literals ⊆ classification map).
- Reconciliation test: upload N files, process M, health reports N−M.
- Migration tests for the backfill + `uploaded_at` on both ladders.

## Out of scope

- Duplicate `users` accounts cleanup (operational).
- Rewriting historical `result` values.
- Full-text search improvements on `params` (tracked separately).
- Multi-worker dedup cache for `_RECENT_AUDITS` (pre-existing note in
  `app/api/activity.py`).
