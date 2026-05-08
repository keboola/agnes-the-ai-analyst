# Changelog

All notable changes to Agnes AI Data Analyst.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), pre-1.0 — public surface (CLI flags, REST endpoints, `instance.yaml` schema, `extract.duckdb` contract) may shift between minor versions; breaking changes called out under **Changed** or **Removed** with the **BREAKING** marker.

CalVer image tags (`stable-YYYY.MM.N`, `dev-YYYY.MM.N`) are produced for every CI build; semver tags (`v0.X.Y`) are cut at release boundaries and reference the same commit as a `stable-*` tag from the same day.

---

## [Unreleased]

### Added

- **Session pipeline framework** under `services/session_pipeline/` — pluggable processors for the centralized `/data/user_sessions/<key>/*.jsonl` tree. Each processor implements a `SessionProcessor` Protocol (`name`, `cadence_minutes`, `process_session(...)`) and runs through its own per-processor scheduler tick + scan loop. No cross-processor coupling: a slow or failing processor cannot block any other. Pure-utility lib (`parse_jsonl`, `compute_file_hash`) is shared; orchestration is per-processor in `runner.run_processor()`. Adding a new processor is one file in `services/session_processors/<name>.py`, one entry in the registry list, one entry in the scheduler `JOBS` list. See `services/session_pipeline/contract.py` for the protocol and `services/session_processors/__init__.py` for the registry pattern.
- `services/session_processors/usage.py` — `UsageProcessor` skeleton (no-op, `cadence_minutes=10`). Reserves the registry slot + scheduler entry so the framework end-to-end exercises two processors. Extraction logic (skill / agent invocation events) and storage shape (DuckDB table vs. append-only parquet event log) are deferred to a separate brainstorm.
- `POST /api/admin/run-session-processor?processor=<name>` — parametrized admin endpoint that drives one session-pipeline processor end-to-end. Admin-gated; same audit pattern as the other `/api/admin/run-*` endpoints (one row per call with action `run_session_processor:<name>`); 400 when `processor` is unknown.
- `SessionProcessorStateRepository` in `src/repositories/session_processor_state.py` — backs the new state table.
- **PostHog snippet middleware preserves `Response.background`** on every return path so any `BackgroundTask` / `BackgroundTasks` attached to an HTML route still fires once the integration is enabled (PR #231 review by minasarustamyan). `BaseHTTPMiddleware` materialises the body and asks subclasses to return a fresh `Response`; the previous implementation dropped `background` on three paths, silently cancelling deferred audit logging / async webhooks / email sends with no log line. Also adds a `_MAX_BUFFER_BYTES` (4 MB) cap so a streamed-HTML response can't balloon RSS — bigger bodies short-circuit through with a warning instead of being buffered. Regression tests in `tests/test_posthog_inject_middleware.py` exercise the four return paths plus the streaming guard.
- **`POSTHOG_LLM_PAYLOAD_MAX_CHARS` (default 30000) clips `$ai_input` / `$ai_output_choices`** before they hit PostHog so oversized prompts don't get silently dropped at ingest. PostHog's per-event ceiling is ~32 KB and the SDK does not chunk; Agnes prompts routinely include sample rows / table schemas / analyst SQL that exceed it, and unbounded payloads landed *exactly* the calls operators wanted to inspect on the floor (PR #231 review by minasarustamyan). Truncated payloads carry an explicit `…[truncated N chars]` marker so a reader doesn't mistake them for a complete capture; metadata (provider, model, tokens, latency, error) flows regardless. Override the cap via the env var.
- **PostHog event-level user attributes** so a reviewer reading an event in PostHog sees who the user was inline, without clicking through to the person profile. Backend `capture_exception` merges `user_id` / `user_email` / `user_name` (per `POSTHOG_IDENTIFY_PII`) into the event properties; browser snippet registers the same keys as super-properties via `posthog.register({...})` so every client-side event including `posthog.captureException()` carries them.
- **`/api/debug/throw` debug-only endpoint** for verifying observability wiring end-to-end. Gated by `DEBUG=1` (404 in production), runs after `Depends(get_current_user)` so `request.state.user` is populated, then raises a configurable exception (`?kind=ValueError&msg=…`). Use to confirm PostHog receives the exception with full user context attached, not just `request_id`.
- **PostHog `environment` + `release` super-properties on every event.** Resolved at startup as `POSTHOG_ENVIRONMENT` (explicit) → `local` when `LOCAL_DEV_MODE=1` → `RELEASE_CHANNEL` → `AGNES_DEPLOYMENT_ENV` → `unknown`. Backend events get them via the SDK's `super_properties`; browser events get them via `posthog.register({...})` in the loaded callback. Filtering PostHog dashboards by `environment = production` cleanly hides traffic from developer laptops, CI, and staging deployments. `release` falls back from `AGNES_VERSION` to `RELEASE_CHANNEL`.
- **`request.state.user` populated by auth dependencies** so response-phase middleware (PostHog snippet injector, 500 handler) can identify the actor without re-running the auth dependency. Adds an `_stash_user` helper in `app/auth/dependencies.py` called from every successful resolution path (LOCAL_DEV_MODE seeded user, scheduler shared-secret, PAT/JWT). The browser `posthog.identify(user_id, {email})` call now actually fires for logged-in users.
- **Optional PostHog observability integration.** Off by default; activates only when `POSTHOG_API_KEY` is set in the environment. Covers backend exception capture (FastAPI 500s + `src/orchestrator.py` rebuild failures + `services/scheduler/` HTTP-job failures + `cli/main.py` uncaught CLI errors), LLM call tracing (`$ai_generation` events with provider, model, latency, and token counts; prompt / completion bodies stay off unless `POSTHOG_LLM_PAYLOADS=1` because LLM prompts in this product routinely include customer data), frontend errors + `$pageview` / `$pageleave`, masked session replay (`maskAllInputs: true` plus a CSS-selector mask for known data surfaces), and feature flags (server-side `is_feature_enabled` + browser `posthog.isFeatureEnabled`). Defaults to PostHog Cloud EU (`https://eu.i.posthog.com`) — override with `POSTHOG_HOST` for US Cloud or a self-hosted endpoint. Identification mode is operator-configurable (`none` / `id` / `email` / `full`); default `email` ships `user.id` + email but never name. The browser snippet is injected by an HTML-rewrite middleware (`app/middleware/posthog_inject.py`) so it reaches every `text/html` page including standalone templates that don't extend `base.html` — registered inside the GZip layer so it sees uncompressed HTML before compression. CLI entry point moved from `cli.main:app` to `cli.main:main` (Typer wrapper that captures uncaught exceptions, flushes, and re-raises). New file `src/observability/posthog_client.py` (lazy singleton, no network when disabled), `src/observability/llm_tracing.py` (`$ai_generation` context manager), `app/web/templates/_posthog.html` (browser snippet template). See `docs/observability.md` for the operator guide and `config/.env.template` for the env-var reference.
- New `/marketplace` browse page combining curated marketplaces with the community Flea Market in a single discovery + install surface. Three tabs (Curated / Flea / My Stack), per-tab category filter with inline SVG icons (Heroicons MIT, no new dependency, in `src/category_icons.py`), Flea-only type filter, search across both sources with Curated/Flea scope checkboxes, numeric pagination — all with URL state via query string. Detail pages live at `/marketplace/flea/<id>` and `/marketplace/curated/<slug>/<plugin>`. Curated detail returns 403 without the RBAC grant. Plugin detail surfaces inner skills/agents as clickable nested cards (`/marketplace/curated/<slug>/<plugin>/{skill,agent}/<name>`); commands/hooks/MCPs render as plain name lists. Guide pages at `/marketplace/guide/{curated,flea}` host the publication-flow placeholder for full copy to be authored separately.
- New REST router under `/api/marketplace` (in `app/api/marketplace.py`): `GET /items` per-tab listing, `GET /categories` per-tab counts, `GET /curated/{slug}/{plugin}` detail, `POST/DELETE /curated/{slug}/{plugin}/install` subscribe/unsubscribe, `GET /curated/{slug}/{plugin}/{skill,agent}/{name}` for inner items.
- `marketplace_plugins.created_at` column for "newest first" sorting on `/marketplace`. `MarketplacePluginsRepository.replace_for_marketplace` switched from delete-and-insert to upsert so `created_at` survives across syncs.
- Redesigned `/marketplace/curated/<slug>/<plugin>/{skill,agent}/<name>` and the flea-side `/marketplace/flea/<entity_id>` skill / agent detail pages (`app/web/templates/marketplace_item_detail.html`). Width matches the plugin detail page (1280 px), light surface hero with kind-tinted accents (skill = green, agent = purple — matching the marketplace cards), Description + Details sidebar, Docs section (flea only — curated docs deferred until per-skill / per-agent metadata YAML lands) and Files section walking the bundle on disk. Curated nested has no install button — instead a "Open parent plugin →" link with helper text noting the install happens at the parent plugin level.
- Curated skill / agent detail pages now render the "How to call it" copy-able invocation chip (previously flea-only). Curated items show `/<plugin-manifest-name>:<inner-name>` — the exact namespace Claude Code applies after install. The `manifest_name` is read from the parent plugin's own `.claude-plugin/plugin.json` via the now-public `src.marketplace_filter.resolve_manifest_name`, matching the synth `marketplace.json` Agnes serves so the chip and the post-install slash command stay in sync. Surfaced via a new `manifest_name` field on `InnerDetailResponse` (`app/api/marketplace.py`).
- Per-tab info blocks above the filter row on `/marketplace`: curated trust signal ("Each plugin here has a named curator accountable for it.", blue accent), flea open-shelf signal ("Anyone in the company can upload here.", purple accent + Tips-for-sharing link), My Stack personal-shelf orientation ("Your AI stack — everything you've added.", slate accent, no link).
- Hero illustration anchored to the right of the blue hero panel (absolute, 47% wide, behind the search row content); hidden under 900px viewport. New asset at `app/web/static/marketplace-cover.png`.
- Per-tab Heroicons next to each tab title (shield-check for Curated / building-storefront for Flea / rectangle-stack for My Stack), tinted to match each tab's accent; flips white when the tab is active.

### Changed

- **BREAKING**: Schema bump v28 → v29 renames `session_extraction_state` → `session_processor_state` with composite PK `(processor_name, session_file)` so multiple processors can track their own processed-set independently. Existing rows are copied across with `processor_name='verification'` and the old table is dropped. The `KnowledgeRepository.is_session_processed` / `mark_session_processed` helpers are removed — sessions bookkeeping now lives in `SessionProcessorStateRepository`. The session-state-aware `is_processed` check now compares `file_hash` so a session jsonl that grows (live append from an active Claude Code session) gets reprocessed on the next tick — previously the file_hash was stored but never read back.
- **BREAKING**: `POST /api/admin/run-verification-detector` is dropped in favor of `POST /api/admin/run-session-processor?processor=verification`. Audit action renames `run_verification_detector` → `run_session_processor:verification`. The scheduler `JOBS` list reflects the new endpoint; no operator action required if the only caller is the in-tree scheduler. The legacy `dry_run` flag (no real callers outside the dropped CLI shim) is gone.
- `services/scheduler/__main__.py` JOBS — `verification-detector` entry replaced by two new entries: `session-processor:verification` and `session-processor:usage`. New env var `SCHEDULER_USAGE_PROCESSOR_INTERVAL` (default 600s); `SCHEDULER_VERIFICATION_DETECTOR_INTERVAL` is retained (still drives the verification cadence AND the health-check grace window in `app/api/health.py`) for operator compatibility with existing docker-compose env files.
- `services/verification_detector/detector.py` is reduced to LLM-side helpers (`_generate_id`, `_format_turns`, `extract_verifications`); the orchestration loop moves into `VerificationProcessor` in `services/session_processors/verification.py`. The CLI (`python -m services.verification_detector`) still works — it now constructs the processor and runs the shared `run_processor` runner.
- `app/api/health.py` `_check_session_pipeline` now queries `session_processor_state WHERE processor_name='verification'` instead of `session_extraction_state` (same heuristic, scoped explicitly to the verification processor).
- `app/web/router.py` `/profile/sessions` join target updated to `session_processor_state` (verification rows). `SCHEDULER_AUDIT_ACTIONS` updated to include the new per-processor audit actions.
- Marketplace UI rebrand: `+ Install` → `+ Add to my stack`, `✓ Installed` → `✓ In your stack`, card "Installed" badge → "In stack" (amber pill), `My Subscriptions` tab → `My Stack`. Bridges the conceptual gap between "saved on the server" (what the click does) and "installed on my laptop" (what users assumed). Same vocabulary now consistent across `/marketplace`, `/store/<id>` detail, navbar link, and the post-add hint panel.
- Plugin and skill/agent detail pages now show an inline post-add hint panel after a successful "Add to my stack" click: green-bordered block under the description with a 2-step recipe ("open new Claude Code session" or run `agnes refresh-marketplace` + `/reload-plugins`), Copy button on the command, "Don't show again" dismiss persisted in `localStorage`. Removes the dead-end where users clicked Install, saw "Installed", opened Claude Code, and found nothing.
- Action-row CTA on `/marketplace`: curated tab `[How to add new content]` → `[Submit a plugin]`, flea tab `[How to add new content]` removed (the `+ Upload` button next to it already covers self-service publishing — second CTA was redundant). Empty-state CTAs aligned: curated empty state links to `Submit a plugin →`, flea empty state shows only `+ Upload`. Guide page titles updated to `Submit a plugin to Curated Marketplace` / `Upload to Flea Market`.
- Skill/agent detail page (curated nested) helper text changed from "To install, install the plugin." to "Add the bundle to your stack to use it." for terminology consistency.
- **BREAKING**: Curated marketplace plugins no longer auto-appear in a user's served marketplace on RBAC grant (Model B opt-in). Users must explicitly Install each curated plugin from `/marketplace`. `resolve_user_marketplace` composition changes from `(rbac ∖ opt_outs) ∪ store_installs` to `(rbac ∩ subscriptions) ∪ store_installs`. Existing users will see an empty served marketplace until they re-install previously-granted curated plugins; no auto-migration of prior preferences is performed.
- The `user_plugin_optouts` DB table is reused for Model B subscriptions — table and column names are kept (no DDL rename) to avoid migration churn on running operator instances. The v28 migration wipes existing rows since the semantic inverts (presence used to mean "excluded", now means "subscribed"). The Python repository is renamed `UserPluginOptoutsRepository` → `UserCuratedSubscriptionsRepository` (in `src/repositories/user_curated_subscriptions.py`) with method names flipped to `subscribe / unsubscribe / is_subscribed / subscribed_set / list_for_user / delete_for_plugin / delete_for_marketplace`.
- `/api/marketplace/items?tab=my` and `/categories?tab=my` read directly from `user_curated_subscriptions ∪ user_store_installs` (not `resolve_user_marketplace`, which bundles flea skills/agents into a single `store-bundle` synthetic entry useful for serving the Claude Code marketplace ZIP/git but wrong for browsing where each item should appear as its own card).
- `/my-ai-stack` curated toggle is now a subscribe/unsubscribe action against the renamed repository (UX — toggle on/off — unchanged; default state is now off).
- `/admin/marketplaces` DELETE cleanup now also drops `user_plugin_optouts` rows for that marketplace so a re-registered slug doesn't inherit stale subscribe state.
- Navbar: standalone "My AI Stack" relabelled "My Stack" and points at `/marketplace?tab=my`; "Store" link removed (Store flow is reachable via the Flea Market tab's `+Upload` button). The standalone `/my-ai-stack` and `/store` routes still work for old bookmarks.
- `GET /api/marketplace/curated/<slug>/<plugin>/{skill,agent}/<name>` (`InnerDetailResponse`) now also returns `marketplace_name`, `category`, `parent_author_name`, `parent_updated_at`, `bundle_size`, and `files` (recursive listing with sizes) so the redesigned detail page can render the hero badges, sidebar, and Files section without a second roundtrip.
- `GET /api/marketplace/flea/<entity_id>/detail` and `GET /api/marketplace/curated/<slug>/<plugin>` (`PluginDetailResponse`) now also return `files`, `docs`, `install_count`, and `owner_display` (friendly name resolved via `users.name → email → owner_username`, mirroring `/store/<id>`).

### Security

- `GET /api/marketplace/curated/<slug>/<plugin>/{skill,agent}/<name>` now containment-checks the resolved file path against `plugin_root` via a new `_safe_join` helper (`resolve(strict=True)` + `relative_to`). The direct URL exploit was already blocked by Starlette's `[^/]+` path-param regex, but a curator-planted symlink inside a curated marketplace's git mirror could previously dereference outside the plugin tree on read. Now centralized so `_read_inner`, the skill `files` walk, and the agent `stat` call all share the same boundary.

### Fixed (PR #232 review)

- `services/scheduler/__main__.py` tick loop is now parallel + advances `last_run` on terminal state. Pre-fix it was a synchronous `for-loop + httpx.post(timeout=900)` — a 10-minute verification run blocked every other job (`data-refresh`, `health-check`, `usage`, `corporate-memory`) for the entire window. The PR's stated isolation guarantee ("slow / failing processor cannot block any other") only held inside `services/session_pipeline/runner.py`; the scheduler dispatch layer broke it. Pre-fix `last_run` also only advanced on success, so a permanently failing job was retried every 30s tick instead of on its 15-min cadence (30× the configured request rate + LLM tokens). Replaced with `ThreadPoolExecutor.submit` per due job + per-job in-flight set so a long-running job can't be re-launched on subsequent ticks. `_run_job` extracted to a module-level helper so the bookkeeping is unit-testable.
- `SessionProcessorStateRepository.scan_unprocessed_for` had a dead `if/else` where both branches surfaced every jsonl, making the `SELECT session_file FROM session_processor_state` round-trip pointless and forcing the runner to MD5-rehash every stable session on every scheduler tick. Replaced with an mtime precheck: stable sessions (mtime <= processed_at) are filtered at scan and the runner never reads or hashes them. Files modified since the last run still surface for the runner's authoritative `file_hash` invalidation.
- `POST /api/admin/run-session-processor` now takes a per-processor advisory lock (`threading.Lock` keyed by name) before invoking the runner. Two trigger paths exist for the same processor (scheduler tick + manual admin POST); without serialization, overlapping runs would re-process the same `/data/user_sessions/*` set, double-call the LLM, and pile up duplicate `verification_evidence` rows (the dedup short-circuit only catches the create+contradiction branches, not `create_evidence`, per ADR Decision 3). Concurrent invocation returns HTTP 409 Conflict so the operator sees what happened instead of stacking behind a long-running tick. Lock releases unconditionally in `finally:` so a runner exception can't wedge the processor permanently.

### Internal

- `services/session_processors/verification.py:build_verification_processor` factory mirrors the lazy LLM-extractor construction previously inlined in `app/api/admin.run_verification_detector` and `services/verification_detector/__main__`. Single source of truth for processor instantiation.
- Schema bumped v27 → v28 (`DELETE FROM user_plugin_optouts` for the semantic flip + `marketplace_plugins.created_at` with `registered_at` backfill).
- New tests `tests/test_marketplace_api.py` (browse, categories, install/uninstall, RBAC 403, `_safe_join` containment). Existing `tests/test_marketplace_filter_store.py`, `tests/test_marketplace_server_zip.py`, `tests/test_marketplace_server_git.py`, `tests/test_store_api.py`, `tests/test_store_repositories.py` updated for Model B (explicit subscribe in fixtures).

## [0.47.4] — 2026-05-08

### Fixed

- `services/session_collector` no longer logs "Collection complete: 0 users, 0 files copied" + "Group 'data-ops' not found" every 10 minutes in the Docker layout where `/home/*/user/sessions/` doesn't exist. New env var `AGNES_SKIP_LEGACY_COLLECTOR=1` (set by default in `docker-compose.yml`) short-circuits the collector pass. The bare-VM deployment path (where /home/* IS populated by Claude Code) leaves this unset and continues to scan + log normally — including the data-ops warning, which is load-bearing for catching missing-group mis-deploys.
- `agnes diagnose` `session_pipeline` check gains a FIFO-aware lookup: in addition to the existing MAX(processed_at) comparison (catches "detector hasn't run lately"), it now flags the case where an OLD jsonl never got processed even though newer ones did (= verification-detector skipped a file). Threshold defaults to 4× the verification-detector grace (= 2h with default 30min grace) and is configurable via `SESSION_PIPELINE_STUCK_FILE_GRACE_SECONDS`. Severity intentionally starts at `info` — operators can tighten to `warning` once they have prod data on false-positive rate.

## [0.47.3] — 2026-05-07

### Fixed

- `agnes self-upgrade` (without `--force`) previously read the local 24h `update_check.json` cache to decide whether an upgrade was needed — meaning that for up to 24 hours after a server-side version bump, the explicit `agnes self-upgrade` command exited silently as a no-op even though a newer wheel was available. Cache is now always invalidated for the explicit command (the cache still gates the implicit warning loop in the root callback to avoid hammering `/cli/latest` on every `agnes <anything>` invocation). Surfaced when a server bump 0.47.1 → 0.47.2 didn't trigger client-side upgrade.

## [0.47.2] — 2026-05-07

### Fixed

- Restore #218 (real BQ error surfacing in `remote_estimate_failed`) and #219 (friendlier missing-table hint in `agnes query`) — both fixes were silently reverted by the squash merge of #217 because that branch carried stale snapshots of `app/api/query.py` and `cli/commands/query.py` from before #218 and #219 merged. Verified end-to-end against production: `agnes query --remote "SELECT FROM unit_economics WHERE bad_col=1"` now returns the BQ "Unrecognized name" diagnostic; `agnes query "DESCRIBE unit_economics"` now appends the remote-table hint.

## [0.47.1] — 2026-05-07

Keboola connector v27 — incremental, partitioned, where_filters, typed parquet.

### Added

- **`query_mode='local'` for Keboola** is back — admins can opt specific tables out of the v26 materialized default and into a per-table sync-strategy dispatcher (full_refresh / incremental / partitioned). The radio sits in the `/admin/tables` Edit modal; metadata stored in seven new `table_registry` columns (see schema v27 below).
- **Three Keboola sync strategies**:
  - `full_refresh` (default): full-table export-async, replaces the on-disk parquet atomically. Same shape as the v26 materialized default.
  - `incremental`: delta export by `incremental_column` (timestamp), merge into existing parquet keyed by primary_key. New `_convert_column` path coerces string-typed deltas to the existing parquet's typed columns; PK conversion failure now raises hard (was silent mixed-type column → broken dedup).
  - `partitioned`: per-partition export by `partition_by` (date/timestamp column), `partition_granularity` (DAY / MONTH / YEAR), with `initial_load_chunk_days` for backfill. Each partition lives in its own parquet under `data/<table>/partition_<value>.parquet`.
- **`where_filters` per table** — JSON list of column-value predicates injected as Storage API export filters; lets admins narrow a wide source table at the connector edge.
- **Typed parquet writes** — Keboola Storage API exports are CSVs with all string columns; the new pipeline reads the table schema (column types) via `get_table_info` and coerces each column to its target dtype before writing parquet. Types preserved across `agnes pull` instead of every analyst seeing strings.

### Changed

- **Schema v26 → v27.** Auto-migration adds the seven new columns to `table_registry`: `incremental_window_days`, `max_history_days`, `incremental_column`, `where_filters`, `partition_by`, `partition_granularity`, `initial_load_chunk_days`. NULL on existing rows; meaningful only when paired with the matching strategy. Pre-existing `sync_strategy` column (default `'full_refresh'`) is now load-bearing — pre-v27 it was inert catalog metadata; post-v27 the Keboola extractor dispatches off it.
- **`PUT /api/admin/registry/{id}`** changed from `{k: v for k, v in request.model_dump().items() if v is not None}` to `request.model_dump(exclude_unset=True)`. Semantic shift: previously, sending explicit `null` in the request body was silently ignored (field kept its existing value); now explicit `null` propagates as a real null update. Intentional — the v27 Edit modal needs to clear `incremental_column` etc. when an admin switches strategy from `incremental` back to `full_refresh`. Inline comment + regression test pin the new behavior.

### Fixed (Devin Review)

- **Schema docs in CLAUDE.md** updated from v25 to v27, with v25→v26 and v26→v27 migration entries describing what each version adds.
- **`update_table` exclude_unset semantic shift** documented inline; `test_api_put_clears_v26_fields_on_strategy_switch` pins the explicit-null-propagates behavior.
- **`incremental.py:_convert_column` failure on primary_key column** now raises hard (was silent mixed-type column → broken dedup downstream). Test added.

## [0.47.0] — 2026-05-07

Catalog metadata enrichment + cache discipline + automatic warmup.
Closes #155 + #156.

### Added

- **`/api/v2/catalog` returns four new optional fields per row** — `rows`,
  `size_bytes`, `partition_by`, `clustered_by` — populated by per-source-type
  metadata providers (`connectors/bigquery/metadata.py`,
  `connectors/keboola/metadata.py`). For `query_mode='remote'` BigQuery rows,
  `size_bytes` is `active_logical_bytes + long_term_logical_bytes` (a full
  scan reads both); region resolved from `data_source.bigquery.location` →
  `bq_client.get_dataset(...)` → fall back to legacy `__TABLES__`.
  Existing CLI consumers reading only `rough_size_hint` are unaffected.
- **Automatic cache warmup at startup.** FastAPI startup event schedules
  a background task that walks BQ remote rows and pre-populates
  `_metadata_cache` + `_schema_cache` with bounded concurrency (default 4,
  tunable via `AGNES_WARMUP_CONCURRENCY`). Doesn't block readiness;
  per-row failures logged + skipped. Opt-out via `AGNES_SKIP_CACHE_WARMUP=1`.
- **Three new admin endpoints under `/api/admin/cache-warmup/*`:**
  - `GET /status` — JSON snapshot of the latest run.
  - `POST /run` — manual trigger, idempotent under concurrent invocation.
  - `GET /stream` — Server-Sent Events with `start` / `row` / `complete`
    events for live UI updates.
- **`/admin/tables` cache freshness panel.** Toolbar above the per-source-type
  listings with progress bar + "Re-warm all" button + collapsible
  terminal-style log fed by SSE (polling fallback at 3 s). Per-row badge
  in the existing `col-status` column updates live (fresh / warming /
  pending / error).
- **`docs/admin/query-modes.md`** — source-agnostic admin reference for
  registering tables as `local` / `remote` / `materialized`. Decision
  tree, per-source-type IAM + setup, three worked examples. Linked from
  the `?` icon next to the `query_mode` field in the admin UI edit modal
  and from the third post-register hint in `agnes admin register-table`.
- **`agnes admin register-table` post-register hint** for `query_mode=remote`:
  points at `agnes query --remote "SELECT COUNT(*)..."` as the IAM smoke
  check so a missing `dataViewer` / `jobUser` surfaces at registration
  time, not 30 minutes later.

### Changed

- **`/api/v2/schema/{id}` cache miss now does 1 BQ job instead of 2.**
  `connectors/bigquery/access.py:fetch_bq_columns_full` collapses what
  used to be `_fetch_bq_schema` + `_fetch_bq_table_options` into a single
  `INFORMATION_SCHEMA.COLUMNS` query (same view, same predicate, just a
  combined SELECT list). The metadata provider's partition/cluster path
  shares the same helper — zero SQL duplication across the two consumers.
- **All four catalog/schema/sample/metadata caches are flushed on registry
  change.** `app/api/v2_catalog.py:invalidate_for_table` is wired into
  `POST /api/admin/register-table`, `PUT /api/admin/registry/{id}`, and
  `DELETE /api/admin/registry/{id}`. After a registry write, a single-row
  re-warm task is scheduled in the background so the admin's verification
  request hits warm caches within ~1 s instead of waiting for the next
  analyst miss. Pre-fix none of the caches were invalidated — admin
  registers a table, `agnes catalog` doesn't show the new row for up to
  5 min; admin updates a row's bucket, `agnes schema` returns the OLD
  column list for up to 1 hour.
- **`v2_schema.build_schema` split into RBAC-aware outer + RBAC-naive
  inner (`build_schema_uncached`).** Live endpoint behavior unchanged;
  warmup uses the inner entry point to populate `_schema_cache` without
  a user context.

### Internal

- New shared dataclass module `app/api/_metadata_models.py` with
  `MetadataRequest` (frozen) + `TableMetadata` for source-agnostic
  provider input/output.
- New `connectors/keboola/storage_api.py:KeboolaStorageClient.get_table_info`
  thin wrapper — keeps `_get` private to the module.
- New env vars (operator-facing tuning, no required setup change):
  - `AGNES_SKIP_CACHE_WARMUP` — opt-out of startup warmup.
  - `AGNES_WARMUP_CONCURRENCY` — default 4, max parallel BQ
    INFORMATION_SCHEMA jobs during a warmup pass.
- New runtime dependency: `sse-starlette>=2.0` (Server-Sent Events
  responses for the cache-warmup stream).
- Tests added: `test_metadata_models`, `test_v2_schema_columns_consolidation`,
  `test_v2_catalog_dispatcher`, `test_connectors_bigquery_metadata`,
  `test_connectors_keboola_metadata`, `test_v2_catalog_remote_metadata`,
  `test_v2_catalog_invalidation`, `test_cache_warmup`,
  `test_main_startup_warmup`, `test_admin_tables_warmup_ui`.

## [0.46.5] — 2026-05-07

### Fixed

- `agnes describe <table> -n 5` previously failed with `Missing argument 'TABLE_ID'` because the command was registered as a `Typer.Typer` subcommand group; the combination of positional `table_id` + short option `-n INTEGER` mis-parses in that pattern. Switched to a flat `@app.command("describe")` registration. All forms (`-n` before/after positional, `--rows=N`, default n=5) now parse correctly. Surfaced from a real analyst session following the CLAUDE.md "agent rails" discovery workflow.
- `/api/v2/sample/<id>` (called by `agnes describe`) returned HTTP 500 with `ValueError: Out of range float values are not JSON compliant: nan` when the result rows contained NaN values from the underlying DuckDB / BigQuery scan. The endpoint now sanitizes NaN/±inf to JSON `null` before serialization. Same surfaced from a real analyst session.

## [0.46.4] — 2026-05-07

### Fixed

- SessionEnd `agnes push` hook previously synchronous-ran in the foreground; Claude Code's `-p` (headless) mode terminates SessionEnd hook subprocesses after ~1 second regardless of work in progress, so the upload was killed mid-stream and most session JSONLs never reached the server. Now wrapped in `bash -c "( nohup agnes push ... & ) ; true"` so the upload child detaches from the hook subprocess and survives Claude's aggressive shutdown. Existing workspaces pick up the detached form on their next `agnes init` invocation via the existing migration path. Verified end-to-end against production: `claude -p` exited in 5s, the detached child completed the upload, and the session JSONL landed on the server within 30s.

## [0.46.3] — 2026-05-07

### Added

- `agnes init` now installs a third SessionStart hook entry (`agnes push --quiet`) so orphan session JSONLs left behind by `claude -p` headless invocations (where Claude Code does NOT fire SessionEnd) or abnormal exits get uploaded on the next interactive session start. Symmetric self-healing alongside the existing `agnes pull` SessionStart entry. Existing workspaces pick up the third entry on their next `agnes init` invocation via the existing migration path in `cli/lib/hooks.py:_OUR_COMMAND_MARKERS`.

### Fixed

- `agnes diagnose` `session_pipeline` warning previously read "uploads are not being processed", which led users to suspect their `agnes push` uploads were failing. The warning now reads "verification-detector backlog" and includes `last_processed` so operators see at a glance that uploads are fine and only the LLM extraction step is behind.

## [0.46.2] — 2026-05-07

### Fixed

- `agnes query` against a `query_mode='remote'` table previously surfaced DuckDB's misleading "did you mean <similar materialized table>" suggestion. Now appends a friendlier hint pointing users to `agnes catalog`, `agnes schema <id>`, and `agnes query --remote`. Reproduces from a real analyst session where `DESCRIBE unit_economics` (a remote table) sent the user down a 30-second wrong path.

## [0.46.1] — 2026-05-07

### Fixed

- `remote_estimate_failed` now surfaces the rewritten-SQL diagnostic (the actual BQ "Unrecognized name" / "Syntax error" message) instead of the unhelpful "Table must be qualified" from the user-original-SQL retry. Adds `underlying_original` for the second-attempt context. Hint now points users to `agnes schema <id>` first — the typical cause is a typo'd column name.

## [0.46.0] — 2026-05-07

Catalog metadata enrichment + cache discipline + automatic warmup.
Closes #155 + #156.

### Added

- **`/api/v2/catalog` returns four new optional fields per row** — `rows`,
  `size_bytes`, `partition_by`, `clustered_by` — populated by per-source-type
  metadata providers (`connectors/bigquery/metadata.py`,
  `connectors/keboola/metadata.py`). For `query_mode='remote'` BigQuery rows,
  `size_bytes` is `active_logical_bytes + long_term_logical_bytes` (a full
  scan reads both); region resolved from `data_source.bigquery.location` →
  `bq_client.get_dataset(...)` → fall back to legacy `__TABLES__`.
  Existing CLI consumers reading only `rough_size_hint` are unaffected.
- **Automatic cache warmup at startup.** FastAPI startup event schedules
  a background task that walks BQ remote rows and pre-populates
  `_metadata_cache` + `_schema_cache` with bounded concurrency (default 4,
  tunable via `AGNES_WARMUP_CONCURRENCY`). Doesn't block readiness;
  per-row failures logged + skipped. Opt-out via `AGNES_SKIP_CACHE_WARMUP=1`.
- **Three new admin endpoints under `/api/admin/cache-warmup/*`:**
  - `GET /status` — JSON snapshot of the latest run.
  - `POST /run` — manual trigger, idempotent under concurrent invocation.
  - `GET /stream` — Server-Sent Events with `start` / `row` / `complete`
    events for live UI updates.
- **`/admin/tables` cache freshness panel.** Toolbar above the per-source-type
  listings with progress bar + "Re-warm all" button + collapsible
  terminal-style log fed by SSE (polling fallback at 3 s). Per-row badge
  in the existing `col-status` column updates live (fresh / warming /
  pending / error).
- **`docs/admin/query-modes.md`** — source-agnostic admin reference for
  registering tables as `local` / `remote` / `materialized`. Decision
  tree, per-source-type IAM + setup, three worked examples. Linked from
  the `?` icon next to the `query_mode` field in the admin UI edit modal
  and from the third post-register hint in `agnes admin register-table`.
- **`agnes admin register-table` post-register hint** for `query_mode=remote`:
  points at `agnes query --remote "SELECT COUNT(*)..."` as the IAM smoke
  check so a missing `dataViewer` / `jobUser` surfaces at registration
  time, not 30 minutes later.

### Changed

- **`/api/v2/schema/{id}` cache miss now does 1 BQ job instead of 2.**
  `connectors/bigquery/access.py:fetch_bq_columns_full` collapses what
  used to be `_fetch_bq_schema` + `_fetch_bq_table_options` into a single
  `INFORMATION_SCHEMA.COLUMNS` query (same view, same predicate, just a
  combined SELECT list). The metadata provider's partition/cluster path
  shares the same helper — zero SQL duplication across the two consumers.
- **All four catalog/schema/sample/metadata caches are flushed on registry
  change.** `app/api/v2_catalog.py:invalidate_for_table` is wired into
  `POST /api/admin/register-table`, `PUT /api/admin/registry/{id}`, and
  `DELETE /api/admin/registry/{id}`. After a registry write, a single-row
  re-warm task is scheduled in the background so the admin's verification
  request hits warm caches within ~1 s instead of waiting for the next
  analyst miss. Pre-fix none of the caches were invalidated — admin
  registers a table, `agnes catalog` doesn't show the new row for up to
  5 min; admin updates a row's bucket, `agnes schema` returns the OLD
  column list for up to 1 hour.
- **`v2_schema.build_schema` split into RBAC-aware outer + RBAC-naive
  inner (`build_schema_uncached`).** Live endpoint behavior unchanged;
  warmup uses the inner entry point to populate `_schema_cache` without
  a user context.

### Internal

- New shared dataclass module `app/api/_metadata_models.py` with
  `MetadataRequest` (frozen) + `TableMetadata` for source-agnostic
  provider input/output.
- New `connectors/keboola/storage_api.py:KeboolaStorageClient.get_table_info`
  thin wrapper — keeps `_get` private to the module.
- New env vars (operator-facing tuning, no required setup change):
  - `AGNES_SKIP_CACHE_WARMUP` — opt-out of startup warmup.
  - `AGNES_WARMUP_CONCURRENCY` — default 4, max parallel BQ
    INFORMATION_SCHEMA jobs during a warmup pass.
- New runtime dependency: `sse-starlette>=2.0` (Server-Sent Events
  responses for the cache-warmup stream).
- Tests added: `test_metadata_models`, `test_v2_schema_columns_consolidation`,
  `test_v2_catalog_dispatcher`, `test_connectors_bigquery_metadata`,
  `test_connectors_keboola_metadata`, `test_v2_catalog_remote_metadata`,
  `test_v2_catalog_invalidation`, `test_cache_warmup`,
  `test_main_startup_warmup`, `test_admin_tables_warmup_ui`.

## [0.45.0] — 2026-05-07

Operator-and-analyst quality bundle: a security fix for the optional
Telegram bot, two CLI gaps closed, and three rounds of UX polish on
`agnes diagnose` and `agnes pull` so non-TTY consumers (CI runners,
Claude Code SessionStart hooks, sub-agent watchdogs) get readable,
actionable signal. Closes #84, #164, #177, #178, #203, #204.

### Security

- **Telegram bot pairing-code RNG hardened (#84).** The pairing code
  used to link a Telegram chat to an Agnes user is now generated via
  `secrets.choice` (CSPRNG) rather than `random.choices`. Pre-fix an
  attacker who scraped one issued code could recover the `random`
  module's PRNG state and predict subsequent codes issued in the same
  process — the fix neutralizes that class of attack
  (`services/telegram_bot/storage.py:_generate_code`).
- **Telegram script runner refuses out-of-shape usernames (#84).** The
  optional notification runner shells out via `sudo -u <username>`. A
  username controlled by an attacker — e.g. via tampering with
  `telegram_users.json` — could otherwise carry sudo flags
  (`-u`, `--shell=…`) or shell metacharacters. The runner now validates
  the value against a POSIX-conservative regex
  (`^[a-z_][a-z0-9._-]{0,31}$`) and returns `None` before invoking
  `subprocess.run` if it doesn't match
  (`services/telegram_bot/runner.py:_USERNAME_RE`).

### Added

- `agnes admin unregister-table <id>` — CLI wrapper for
  `DELETE /api/admin/registry/{id}` (#177). Confirms before destructive
  action; pass `--yes` to skip the prompt in scripts. The server-side
  endpoint already does the parquet/`sync_state` cleanup; the CLI is a
  thin client.
- `agnes admin update-table <id>` — CLI wrapper for
  `PUT /api/admin/registry/{id}` (#177). Only the supplied flags go in
  the body (`--name`, `--bucket`, `--source-table`, `--query-mode`,
  `--query`, `--description`, `--sync-schedule`, `--source-type`); the
  rest stay unchanged on the server. `--query` accepts `@path/to.sql`
  for files. Calling with no flags errors (`No fields supplied`)
  instead of silently no-opping.
- `agnes diagnose --include-schema` (#204). The default `agnes
  diagnose` no longer surfaces the DB schema-version check — analysts
  hitting the CLI rarely care about the integer, and it dominated the
  agent-facing output. Pass `--include-schema` (or query
  `/api/health/detailed?include=schema` directly) when verifying a
  migration.
- **`info` severity tier in `/api/health/detailed`** (#178). Sits
  between `ok` and `warning`: surfaces a non-trivial observation
  worth reading without promoting the headline status to `degraded`.
  See the module docstring at `app/api/health.py` for the full
  severity ladder. The BQ billing-equals-data check is the first
  consumer (was `warning` → now `info`).
- `AGNES_PULL_PROGRESS_INTERVAL_SECONDS` and
  `AGNES_PULL_PROGRESS_INTERVAL_BYTES` env knobs for the textual
  progress emitter (#203). Defaults are tighter than pre-fix (5 s /
  1 MiB vs the previous 30 s / 10%-of-total) so non-TTY consumers
  see continuous output and don't trip dead-process watchdogs on
  multi-GB parquets. Override either independently.

### Changed

- **`agnes pull` non-TTY progress is more chatty by default (#203).**
  Previous cadence (30 s / 10%) produced one line every several
  minutes on multi-GB parquets, long enough for Claude Code
  sub-agent watchdogs to kill the pull as a hung process. New
  defaults: emit when *any* of (10% boundary, 5 s elapsed, 1 MiB
  bytes since last emit). The 10% boundary is unchanged so small
  files still get the original visual rhythm.
- **`/api/health/detailed` no longer includes `db_schema` by default
  (#204).** Pass `?include=schema` to opt back in. The aggregator
  treats the schema check as "not asserted" when absent, so
  unrelated services can still drive the headline. Operators using
  the legacy entry should add the parameter to their probe
  configuration.
- **BQ billing-project equals data-project surfaces as `info`, not
  `warning` (#178).** Many valid single-project dev instances run
  with billing == data; the message is informational. The `detail`
  + `hint` strings are unchanged so the operator still gets the
  USER_PROJECT_DENIED context if they're hitting it. Pre-fix, the
  message alone promoted the overall headline to `degraded` even on
  intentionally collapsed setups.
- `agnes init --force` now snapshots the prior `CLAUDE.md` to
  `CLAUDE.md.bak.<ISO-timestamp>` before regenerating it (#164). Each
  re-run produces a fresh backup; the prior backup is not clobbered.
  A FS error on the backup path is logged but does not abort the
  init (the existing-workspace gate still requires `--force`).

### Internal

- New `cli.client.api_put` helper to mirror `api_get` /
  `api_post` / `api_delete` / `api_patch` for the new
  `update-table` command.
- Tests added: `tests/test_telegram_bot_runner.py`,
  `tests/test_health_schema_gate.py`, plus extensions to
  `test_telegram_storage`, `test_pull_progress`, `test_diagnose_billing`,
  `test_cli_admin`, `test_cli_init`.
- `infra/modules/customer-instance` (tag `infra-v1.8.0`):
  `startup-script.sh.tpl` no longer overwrites operator-edited
  `AGNES_TAG` / `AGNES_TEMP_DIR` in `/opt/agnes/.env` on every boot.
  Reads the existing values when present and lets them win over the
  template-computed `$IMAGE_TAG`. Pre-fix, an in-place TF action that
  stopped/started the VM (e.g. `machine_type` change) would re-run the
  startup script and clobber any manually-pinned image tag — operators
  had to re-edit the file post-restart. Fresh provisions still get the
  TF-driven values; the `.env` file's existence is the disambiguator.
  To force a TF-driven reset, `rm /opt/agnes/.env` and reboot. Folded
  in from #214, which landed on main between 0.44.1 and this cut.

## [0.44.1] — 2026-05-07

## [0.44.1] — 2026-05-07

### Fixed

- `/admin/users/{id}` — "Add to group" dropdown explains itself when empty instead of leaving the admin staring at a silent `— Pick a group —` placeholder. Three cases now surface a hint below the picker: (a) user is already in every group, (b) every remaining group is Google-Workspace-managed and Agnes can't grant manually (POST would 409 — link to `/admin/groups` to create a custom group), (c) no groups exist at all. Pre-fix on deployments where `Admin` + `Everyone` are mapped via `AGNES_GROUP_{ADMIN,EVERYONE}_EMAIL` and no custom groups exist, the picker was empty with zero indication that the operator needed to create a custom group first.
- `/admin/users/{id}` — "Add to group" dropdown's `loadAll()` race fixed: pre-fix `loadGroups()` and `loadMemberships()` ran in parallel and `refreshGroupDropdown()` (called from `loadGroups`) read the `memberships` global, which could still be `[]` if memberships hadn't returned yet — letting the dropdown show groups the user was already in. `loadMemberships()` now re-runs the dropdown refresh once it has its data, so the final render reflects both data sets regardless of which fetch completes first.

## [0.44.0] — 2026-05-07

### Added
- `agnes refresh-marketplace` — single CLI command that owns the per-user
  filtered Claude Code marketplace lifecycle. `--bootstrap` does the
  first-time setup: clones the per-user marketplace bare repo to
  `~/.agnes/marketplace`, strips the PAT from the cloned origin URL so it
  doesn't sit in plaintext at rest, registers the local path with Claude
  Code, and installs every plugin in the served manifest at
  `--scope project`. Without `--bootstrap` it does an incremental refresh:
  fetch + reset to the remote, then version-aware reconcile (install missing
  plugins, update on version diff, skip on match). Plugins removed from the
  manifest are deliberately NOT auto-uninstalled — a transient empty manifest
  from the server would otherwise wipe the user's stack.
- `agnes init` now installs a SessionStart hook that runs
  `agnes refresh-marketplace --quiet` on every Claude Code session,
  alongside the existing chained `agnes self-upgrade; agnes pull` entry.
  The marketplace refresh runs as a *separate* hook entry (not chained)
  so a failure (e.g. fresh workspace with no clone yet) doesn't suppress
  the data pull. The refresh command is wrapped in `bash -c "..."`
  because Claude Code on Windows runs hook commands directly without a
  shell, which would otherwise leave the `2>/dev/null || true` syntax
  uninterpreted.
- When `agnes refresh-marketplace` detects an actual change, it emits
  Claude Code hook JSON on stdout — `systemMessage` (transient toast)
  and `additionalContext` (model-side system reminder) — both pointing
  at `/reload-plugins` so the running session loads new plugins without
  a restart.

### Changed
- Install-prompt step 5 (in the dashboard-served setup payload) collapses
  from a 15-line inline shell sequence — `rm -rf` + `git clone` + per-plugin
  `claude plugin install` calls — to a single `agnes refresh-marketplace
  --bootstrap` invocation. The old inline form tripped Claude Code's agent
  `rm -rf` permission gate on first run.
- `scripts/dev/agnes-client-reset.sh`: now cleans
  `~/.claude/plugins/{marketplaces,cache}/agnes`, drops the uv build cache,
  and documents workspace-scoped residue that can't be enumerated from a
  user-level reset.

### Internal

- `infra/modules/customer-instance` (tag `infra-v1.7.0`): `google_compute_instance.vm` now sets `allow_stopping_for_update = true`. Without it, changing `machine_type` (or any other field GCP will only mutate on a stopped VM) caused Terraform to fall back to a destroy + recreate, churning VM-local state for what should be an in-place resize. Consumers do not need to update — the field is provider-side only — but bumping the module ref to `infra-v1.7.0` enables in-place machine-type bumps.

## [0.43.0] — 2026-05-06

### Added

- CLI auto-upgrade: `agnes self-upgrade` reinstalls the CLI from the server's currently-shipped wheel via `uv tool install --force`, falling back to `pip install --force-reinstall --no-deps` via `sys.executable` when uv is not on PATH. After install, the new binary is smoke-tested at the install-resolved path (`uv tool dir --bin` for uv, `<sys.executable parent>/agnes` for pip) — never via PATH lookup, to avoid stale-shadow false positives. Smoke failure triggers automatic rollback to the previously verified-good wheel (recorded in `~/.config/agnes/last_known_good.json`); rollback's exit code is captured and surfaced on stderr if it also fails. First-ever upgrade or unrecoverable rollback prints the canonical bootstrap recovery: `curl -fsSL <your-agnes-server>/cli/install.sh | bash`. The new command is wired into the SessionStart hook installed by `agnes init` as a chained shell entry (`agnes self-upgrade … || true; agnes pull … || true`) so an upgrade failure does not block the pull.
- Server: `/api/*` responses now carry `X-Agnes-Latest-Version` and `X-Agnes-Min-Version` headers. CLIs older than `X-Agnes-Min-Version` exit with **code 2** and a remediation message instead of failing on a wire-protocol mismatch. Day-one floor is `0.0.0` (no enforcement) — bump `MIN_COMPAT_CLI_VERSION` in `app/version.py` in the same PR that ships a deliberate wire break.
- CLI: `cli/update_check.py:check()` accepts a keyword-only `bypass_disabled=True` so explicit `agnes self-upgrade` invocations probe `/cli/latest` even when `AGNES_NO_UPDATE_CHECK=1` is set (which silences the implicit warning loop only).

## [0.42.0] — 2026-05-06

### Fixed
- `agnes query --remote`: full backtick BigQuery paths in user SQL are no
  longer corrupted by the registered-name rewriter. Previously a query
  like ``SELECT … FROM `<project>.<dataset>.<table>` WHERE …`` whose
  table name happened to be registered as a bare-name alias would have
  the alias re-substituted *inside* the backtick path, producing
  malformed SQL that BigQuery rejected with a parse error. The cap-guard
  then fell back to a filter-less `SELECT *` size estimate (often orders
  of magnitude larger than the real scan), blocking the query as
  `remote_scan_too_large`. Issue #201.

### Changed
- `agnes query --remote`: cap-guard fallback no longer estimates from
  a synthetic `SELECT *` when the rewritten SQL fails dry-run. It first
  retries the user's original SQL (handles BQ-native input cleanly), and
  only when *that* also fails returns a structured `remote_estimate_failed`
  HTTP 400 with a hint instead of silently over-estimating.
- **BREAKING (clients matching error kinds)**: failure to estimate
  remote-query scan size now returns `kind="remote_estimate_failed"`
  instead of being masked as `remote_scan_too_large` caused by
  over-estimation. Operators that grep for the old kind in dashboards
  should update.

### Security
- `agnes query --remote`: full backtick BigQuery paths are now
  registry-gated identically to `bq."<dataset>"."<table>"` syntax.
  Previously, full backtick paths bypassed Agnes RBAC entirely — only
  the configured service account scope limited what users could query.
  New `bq_path_cross_project` (when the project ≠ configured data
  project) and `bq_path_not_registered` (when path is unknown) error
  kinds. Issue #201.

## [0.41.0] — 2026-05-06

### Fixed
- **Orchestrator filesystem fallback for materialized parquets that
  couldn't register in `extract.duckdb`'s `_meta`**
  (`src/orchestrator.py:_attach_and_create_views`). The 0.40.0 fix in
  `materialize_query` opens `extract.duckdb` from a fresh DuckDB handle
  to write the `_meta` row + inner view; in production the same uvicorn
  process already holds `extract.duckdb` ATTACHed read-only as the
  source-name alias under the orchestrator's analytics connection, and
  DuckDB's single-process file-handle uniqueness rejects the second
  open with `Binder Error: Unique file handle conflict: Cannot attach
  "extract" — already attached by database "<source>"`. The 0.40.0
  helper logs WARNING and falls through; parquet stays canonical, but
  the master view never appears via the meta path.

  This release adds a second pass at the end of
  `_attach_and_create_views`: scan `<extract_dir>/data/*.parquet` and
  create a master view via `read_parquet('<path>')` for any parquet
  whose `<id>` is not already in the per-source `tables` list (i.e. the
  meta path didn't pick it up). Decoupled from `materialize_query`'s
  open-handle race; robust against any registration drift between
  materialize and rebuild. Honors the same `view_ownership` / cross-
  connector collision rules as the meta path (first-come-first-served
  via `view_repo.claim`). Tests cover: fallback fires when meta row is
  missing; fallback skips when meta path already created the view (no
  shadow); invalid identifier in parquet stem is skipped without crash;
  source without `data/` subdir doesn't crash the scan.

## [0.40.0] — 2026-05-06

### Fixed
- **Materialized BigQuery parquets now register themselves in
  `extract.duckdb` so the master view actually appears**
  (`connectors/bigquery/extractor.py:materialize_query`). Pre-fix the
  function wrote the `<id>.parquet` to disk and returned the row count,
  but **never** wrote a `_meta` row or an inner view in the connector's
  `extract.duckdb`. The orchestrator's `rebuild()` scans `_meta` to
  decide which master views to create, so materialized tables remained
  invisible: `agnes query "SELECT … FROM <id>"` returned HTTP 400
  *"registered as query_mode='materialized' but is not yet materialized
  in this instance's analytics views"* even though the parquet was
  sitting there. Symptom appeared after every container recreate (image
  upgrade) and after every `_create_meta_table` cycle in the extractor
  subprocess (which `DROP TABLE IF EXISTS _meta` + `CREATE TABLE`
  cleanly each pass — wiping any prior materialized rows). Fix: after
  the atomic `os.replace(tmp_path, parquet_path)`, open
  `extract.duckdb` and `DELETE FROM _meta WHERE table_name = ? + INSERT
  + CREATE OR REPLACE VIEW <id> AS SELECT * FROM read_parquet('<path>')`
  inside a single transaction. Idempotent, fail-soft (parquet remains
  canonical, the next sync pass recovers any registration drift).
  When `extract.duckdb` doesn't exist yet (fresh BQ-only deployment),
  the fix logs and continues — the next extractor pass creates the
  file and the master view appears on the rebuild after that.

## [0.39.0] — 2026-05-06

### Performance
- **`/api/query` (and `agnes query --remote`) now rewrites user SQL referencing
  `query_mode='remote'` BigQuery rows into a single `bigquery_query()` call
  before execute** (`app/api/query.py`). Pre-fix the master view
  (`CREATE VIEW <name> AS SELECT * FROM bigquery.<bucket>.<source_table>`) did
  not push WHERE / SELECT / LIMIT into BQ — the DuckDB BQ extension opened a
  Storage Read API session over the entire upstream table, scanning the full
  partitioned dataset before the local DuckDB filter ran. On 100M+ row
  remote-mode tables this was 50-100× slower than the equivalent direct
  `bigquery_query()` call (70-150 s vs 1.5 s) and frequently failed with
  `Response too large to return`. The rewriter (shared core with the existing
  dry-run helper) wraps the user's whole SQL in `bigquery_query('<project>',
  '<inner-sql>')` so the BQ planner receives the full query and applies
  partition pruning + projection pushdown server-side. Conservative
  fall-through: cross-source JOINs (BQ ↔ Keboola/Jira local), queries already
  containing `bigquery_query(`, and unconfigured BQ project all keep the
  original ATTACH-catalog path so behavior degrades gracefully.
- **DuckDB BigQuery-extension session pool**
  (`connectors/bigquery/access.py`). `BqAccess.duckdb_session()` now acquires
  pre-warmed connections from a bounded process-local pool instead of running
  `INSTALL bigquery; LOAD bigquery; CREATE SECRET; ATTACH …` on every request.
  Each acquire saves the ~0.5 s extension-load + secret-creation cost when
  the pool has a warm entry; auth SECRET is refreshed on acquire so a
  long-lived pooled entry doesn't keep a stale GCE metadata token past its
  TTL. Pool size is configurable via `data_source.bigquery.session_pool_size`
  (default 4; sentinel `0` disables pooling). Affects every BQ-touching path
  — `/api/query`, `/api/v2/scan`, `/api/v2/sample`, `/api/v2/schema`,
  materialize, and the orchestrator's remote-attach.
- **`agnes pull` chunked download for large parquets**: when the server
  advertises `accept-ranges: bytes` and a parquet exceeds
  `AGNES_PULL_CHUNK_THRESHOLD_BYTES` (default 50 MB), the CLI now splits
  the file into N parallel HTTP Range requests
  (`AGNES_PULL_CHUNK_PARALLELISM`, default 4, capped 1..16) and assembles
  the parts into the destination atomically. Targets the per-flow-shaped
  network (corp VPN with per-TCP-connection rate-limiting) where a single
  stream is throttled but N parallel streams over the same connection
  scale roughly linearly. Falls back to single-stream when the server
  responds 200 instead of 206 to a Range probe, when no
  `accept-ranges: bytes` is advertised, or when content is below the
  threshold — no behavior change in the small-file / non-cooperating-
  server cases.
- **Persistent HTTP/2 client across `agnes pull`**: `stream_download` now
  routes through a process-wide pooled `httpx.Client` so N parquet
  downloads share a single TLS handshake; HTTP/2 multiplexing
  (when the optional `h2` package is installed) lets all chunk Range
  requests share one TCP connection. Gracefully falls back to HTTP/1.1
  pooling when `h2` is missing — no crash, just slightly less benefit.

### Fixed
- **BigQuery `responseTooLarge` no longer surfaces as a generic 400 / 502 with
  the raw upstream message** (`connectors/bigquery/access.py`). The
  `translate_bq_error` helper now classifies "Response too large to return"
  errors via a dedicated `bq_response_too_large` kind (HTTP 400) with an
  actionable hint pointing at the WHERE / aggregation / materialized-table
  remediations. Pre-fix this failure mode fell through to the generic
  `bq_bad_request` mapping, which implied the user's SQL had a syntax error
  — wrong root cause. Affects every BQ-touching path (`/api/query`,
  `/api/v2/scan`, `/api/v2/sample`, `/api/v2/schema`, materialize) since
  they all share `translate_bq_error`.

### Added
- New optional dependency `h2>=4.1.0` (HTTP/2 transport for httpx). Pure
  performance — `agnes pull` works on HTTP/1.1 if the install skips it.
- **Textual progress fallback for non-TTY `agnes pull`**: when stderr is
  not a terminal (Claude Code SessionStart hook, CI runner, Docker log
  capture, …), `agnes pull --no-quiet` now emits a plain-text progress
  line per file at most every 10% or 30 s, plus a final completion line.
  Replaces the previous Rich-bar-on-pipe behavior that either suppressed
  output entirely or leaked ANSI escape sequences. TTY path unchanged
  (Rich progress bar with bytes / speed / ETA, aggregated per-file
  across chunked-download chunks).

## [0.38.3] — 2026-05-06

### Changed
- **Admin / Tables**: registry table now shows Source (bucket/table), Schedule, Folder, Registered by/at, and a sync-error warning icon per row. The page widens to ~1600px to accommodate.

### Fixed
- **Admin / Tables**: long table descriptions no longer push the row's Edit / Manage access / Delete buttons off-screen. The Description column is now clamped to 2 lines with the full text available on hover and in the Edit modal.
- **Admin / Tables**: descriptions stored with shell-quoting backslash-escapes (`Don\'t`, `\n`) now render correctly. The same normalization also runs at register/update time so newly-saved descriptions are never corrupted.
- **Admin / Tables**: `scripts/fix_description_escapes.py` cleans up already-corrupted descriptions in `table_registry` (run with `--dry-run` first, then `--apply`).

## [0.38.2] — 2026-05-06

### Fixed
- **`bq_query_timeout_ms` was not applied on every BigQuery ATTACH branch**
  (`src/db.py:_reattach_remote_extensions`,
  `src/orchestrator.py:_attach_remote_extensions`). Pre-fix only the
  metadata-token branch (the BqAccess contract, `token_env=''`) called
  `apply_bq_session_settings`. BigQuery sources registered with an explicit
  `token_env`, or with no auth env, ATTACH'd without ever applying the
  timeout — falling back to the extension's 90 s default. Default-config
  operators on those branches now consistently get the configured 600 s
  (or whatever `data_source.bigquery.query_timeout_ms` is set to).
- **`apply_bq_session_settings` swallowed every `Exception` silently**
  (`connectors/bigquery/access.py`). Two realistic failure modes — the
  BigQuery extension not yet loaded on the connection, or an installed
  extension version that doesn't recognise the setting — left the 90 s
  default in place with no log line explaining why. Each failure path
  now logs `WARNING` with the actionable cause; on success the applied
  value is verified via a `current_setting('bq_query_timeout_ms')`
  readback (catches the silent-ignore mode some extension versions
  exhibit) and a mismatch logs `WARNING` too.

## [0.38.1] — 2026-05-06

### Internal
- `CLAUDE.md` — `Claude Code marketplace endpoint` section now documents the
  two-step fallback (system `git clone` + local `claude plugin marketplace
  add`) for users registering manually against a private-CA Agnes instance.
  Bun-compiled `claude` ignores the OS trust store and CA env vars on the
  marketplace HTTPS path, so direct `/plugin marketplace add` over HTTPS can
  fail with TLS errors on macOS / Windows even when system tools work fine.
  The dashboard-served setup payload (`app/web/setup_instructions.py`)
  already branches between the two automatically based on platform; the
  doc snippet now matches that behavior for manual flows.

## [0.38.0] — 2026-05-06

### Added
- **`/store` page** — community marketplace where every authenticated user
  can upload skills, agents, and plugins as ZIPs. Listing has type / category /
  search filters; detail page shows metadata, file list, photo, video link,
  and an `[Install]` button. Same owner can't have two entities with the same
  `name` (any type). Plugin/skill/agent name is suffixed `-by-<owner-username>`
  (sanitized email-local-part) at upload time to avoid collisions in Claude
  Code's flat namespace.
- **`/my-ai-stack` page** — every user's per-user composition view: the
  admin-granted plugins (with an opt-out toggle each, default enabled) plus
  the entities they've installed from the Store. Toggling a curated plugin
  off writes a `user_plugin_optouts` row; admin removing the underlying
  grant drops everyone's opt-out (re-grant restarts at enabled).
- **Composed served marketplace**: the `/marketplace.zip` and
  `/marketplace.git/` endpoints now serve `(admin_granted ∖ opt_outs) ∪
  store_installs` — driven by the new
  `src/marketplace_filter.py:resolve_user_marketplace`. Same content-addressed
  ETag / git-commit-SHA contract as before; any change on either layer
  propagates to Claude Code on the next refresh.
- **Store skill+agent bundle**: skill/agent installs are merged into a single
  synthetic `agnes-store-bundle` plugin in the served marketplace (one plugin
  with N skills/agents inside), while `type='plugin'` Store entities stay
  standalone. Cuts plugin-entry count in Claude Code from O(installs) down
  to O(1) for the skill+agent path. Bundle's `version` field hashes its
  combined contents so install/uninstall flips it for auto-update detection.
- REST: `POST/PUT/DELETE/GET /api/store/entities[/{id}]`,
  `POST/DELETE /api/store/entities/{id}/install`,
  `GET /api/store/entities/{id}/photo`,
  `GET /api/store/entities/{id}/docs/{filename}`,
  `POST /api/store/entities/preview` (wizard step-1 validation),
  `GET /api/store/categories`, `GET /api/store/owners`,
  `GET /api/my-stack`,
  `PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}`.
- **CLI: `agnes store {list,show,install,uninstall,upload,update,delete,pull,info}`** and
  **`agnes my-stack {show,toggle}`** — full analyst-side coverage of the
  new Store + composition REST surface. Multipart upload helper added to
  `cli/v2_client.py` (`api_post_multipart` / `api_put_multipart` /
  `api_get_stream`) so future multipart and binary-download endpoints
  don't have to roll their own httpx wiring.
- **CLI: `agnes admin store {pull,push,info}`** — operator-flavored
  bulk Store ops. ``pull`` and ``info`` share the open
  `GET /api/store/bundle.zip` / `/entities` endpoints; ``push`` wraps
  the admin-gated `POST /api/store/import-bundle`. ``push`` accepts
  either a *.zip file or a directory containing `manifest.json` +
  `entities/` (CLI zips a directory client-side, so a backup git
  repo's working tree round-trips straight back into Agnes via a
  single command).
- **CLI: `agnes store mine`** — analyst-facing self-bundle. Same
  endpoint as `admin store pull`, scoped via `?owner=me` (server
  resolves the magic value to the caller's user_id) so authors can
  archive their own uploads without admin role.
- **REST: `GET /api/store/bundle.zip`** — deterministic ZIP of all
  (filtered) Store entities for whole-Store backup. Layout:
  `manifest.json` at the top with per-entity metadata + `owner_email`
  for cross-instance restore, then `entities/<entity_id>/{plugin,assets}/`.
  Auth: any authenticated user (Store is community-open, the same set
  is already visible via `GET /api/store/entities`). Filters mirror the
  listing endpoint (type / category / owner / search).
- **REST: `POST /api/store/import-bundle`** — admin-only restore of a
  bundle ZIP. Modes: `merge` (default — upsert by `entity_id`, replace
  when version differs), `replace` (overwrite all matching), `skip`
  (only insert new). Owner resolution by `owner_email` against
  `users.email`; missing emails get a stub disabled user
  (`active=False`, no password, id `imported-<sha256[:12]>`) so the
  historical owner stays attached and an admin can later activate or
  reassign in `/admin/users`. Audit-logged with the full counts.

### Changed
- `/admin/marketplaces` admin nav entry moved from the top-level header into
  the Admin dropdown and renamed to **Curated Marketplaces** to disambiguate
  from the new community Store.
- `app/api/access.py` `DELETE /api/admin/grants/{grant_id}` now drops every
  user's `user_plugin_optouts` row matching the deleted plugin and flushes
  the marketplace ETag cache. Audit log entry for `resource_grant.deleted`
  carries `optouts_dropped` so operators can correlate.
- `app/marketplace_server/{packager,git_backend}.py` consume
  `resolve_user_marketplace` instead of `resolve_allowed_plugins`. The
  `/marketplace/info` payload now splits its `plugins` array by `source`,
  exposing `plugins` (admin) and `store_plugins` (community).

### Fixed
- **Stored XSS via `video_url`** (`app/api/store.py`) — `video_url` accepted
  on `POST/PUT /api/store/entities` is now scheme-validated to `http(s)://`
  only. Previously a `javascript:` URI flowed through the form field into
  `store_detail.html`'s `<a href>` and would execute in any viewer's
  session on click. 400 `invalid_video_url` on bad input.
- **ZIP decompression bomb** (`app/api/store.py:_safe_zip_extract`) — the
  uncompressed-side total of an upload is now capped at 200 MB
  (`MAX_ZIP_UNCOMPRESSED`); the compressed-side cap (50 MB) alone did not
  bound the on-disk footprint. 413 `zip_too_large_uncompressed` on
  oversize.
- **Admin authz parity for Store mutations** (`app/api/store.py`,
  `app/web/router.py`, `app/web/templates/store_detail.html`) —
  `PUT /api/store/entities/{id}` now permits owner OR admin (matches
  `DELETE`); the store-detail page passes `is_admin` to the template and
  gates the Edit/Delete buttons on `is_owner OR is_admin`. Pre-fix, an
  admin could delete via the API but saw no Edit/Delete affordance in the
  UI, and could not update non-owned entities at all.
- **Scratch directory leak on ZIP validation failure** (`app/api/store.py`,
  Devin Review) — `create_entity` and `update_entity` created the `scratch`
  temp dir inside one `try/finally` block but cleaned it up in a separate
  one. When `_safe_zip_extract` raised `HTTPException` (zip-slip,
  uncompressed-too-large) or `BadZipFile` was caught and re-raised, the
  exception exited the first scope and the cleanup `finally` was never
  reached. Each failed upload leaked a temp dir. Fixed by collapsing
  scratch creation + cleanup into a single outer `try/finally` covering
  both extraction and the metadata/bake work.
- **Cross-owner suffix collision** (`app/api/store.py:create_entity`) —
  `sanitize_username` is many-to-one (`alice.smith` and `alice_smith`
  both → `alice-smith`). Two such users uploading entities with the same
  display `name` produced identical `<name>-by-<username>` suffixes,
  silently colliding in the served bundle's on-disk paths and the
  manifest catalog (Claude Code dedupes by `plugin.json`'s `name`).
  We now refuse the second upload with 409 `conflict_global_suffix`.

### Internal
- Schema **v24 → v25**: adds `store_entities`, `user_store_installs`,
  `user_plugin_optouts`. Auto-migration via `_V24_TO_V25_MIGRATIONS` ladder
  branch in `src/db.py` (existing self-heal path also creates the tables on
  same-version starts).
- New helpers in `src/store_naming.py`: `sanitize_username`, `suffixed_name`,
  `compute_entity_version` (sha256 of sorted `(relpath, content)` tuples,
  16-char hex prefix). Predefined category taxonomy in `src/store_categories.py`.
- New repositories: `src/repositories/{store_entities,user_store_installs,
  user_plugin_optouts}.py` (mirror existing `marketplace_plugins` style — dict
  returns, parameterized SQL, no ORM).
- `app/utils.py:get_store_dir()` — `${DATA_DIR}/store/`.
- `humanbytes` Jinja2 filter on Store detail page (binary KB/MB/GB).
- New CLI command modules: `cli/commands/store.py`, `cli/commands/my_stack.py`.
  Registered as Typer subapps `agnes store` and `agnes my-stack` in
  `cli/main.py`. Tests at `tests/test_cli_store.py`.
- `tests/test_store_api.py:TestStoreSecurityFixes` — regression suite for
  F1 (video_url), F2 (zip-bomb), F4 (admin authz parity), F5 (cross-owner
  suffix collision).

## [0.37.0] — 2026-05-06

Operator-side disk-layout release. Closes the 2026-05-05 shadow-mount class identified in v0.36.0's deploy notes via two independent fixes that operators can adopt separately: (#194 folds in @cvrysanek's #191 + #192). The image-side change is invisible — `STATE_DIR` defaults to the legacy nested path, so existing deployments see no behavior change unless they opt into the new flat layout. Folds in three rounds of Devin Review (3 BUGs + 1 ANALYSIS class, ANALYSIS deferred per the operator-side limitation it describes).

### Added
- **`STATE_DIR` env var + `docker-compose.flat-mount.yml` overlay** — operators can now place the writable state disk in **parallel** to the data disk (`sdb` at `/data`, `sdc` at `/data-state`) instead of nested (`sdc` at `/data/state` inside `/data`). The flat layout removes three structural fragilities of the legacy nested layout: bind-mount propagation gotchas (the 2026-05-05 shadow-mount class), two-writer collisions on a shared prefix (host's `tls-rotate.timer` as root + container app as uid 999 on the same path), and mount-order coupling on disk resize. `STATE_DIR` defaults to `${DATA_DIR}/state` so existing deployers see no behavior change; opt-in to flat layout via the new overlay + `STATE_DIR=/data-state` per the runbook in `docs/state-dir.md`. Read by `src/db.py:_get_state_dir()`, `app/secrets.py:_state_dir()`, `app/main.py` (`.env_overlay`), `app/instance_config.py` (`instance.yaml` overlay reader), `app/api/admin.py` (writers for both `/api/admin/configure` and `/api/admin/server-config` against the same overlay), `app/api/marketplaces.py` (marketplace PAT persistence into `.env_overlay`), `scripts/ops/agnes-auto-upgrade.sh` (mount-sanity + cert detection), `scripts/ops/agnes-tls-rotate.sh` (`CERT_DIR=$STATE_DIR/certs`). All read/write sites resolve via the same helper so under `STATE_DIR=/data-state` the irreplaceable tier (`system.duckdb`, secrets, `instance.yaml`, `.env_overlay`, certs) lands on sdc consistently — partial migration would silently lose secrets on container restart.

### Changed
- **`docker-compose.host-mount.yml` switched from "named volume + driver_opts" to direct service-level bind mounts** (`volumes: !override` per service). Docker named volumes have an immutability footgun: once a volume is created, its driver options are fixed for the life of the volume, and editing this file does NOT propagate the new options to existing volumes. This bit a deployer in production: the volume was created before the overlay had `bind,rbind`, kept the old `bind` (non-recursive) propagation, and containers wrote to a shadowed subdirectory of the parent disk instead of the nested child mount. DuckDB went FATAL on a root-owned WAL during a routine container recreate; sign-in broke. Direct service binds re-evaluate options every container start and default to recursive in modern Docker (20.10+) — no immutable state to migrate, no shadow-mount class. Operators on this overlay: next `docker compose up -d` starts containers with direct binds; the old `agnes_data` named volume is no longer referenced and can be removed with `docker volume rm agnes_data` (operator's choice — orphaned but harmless if left). Both `host-mount.yml` and `flat-mount.yml` `volumes: !override` blocks for `caddy` now restate every mount the base service depends on (notably `data:/srv:ro` for the v0.36.0 file_server bypass and `caddy_config:/config` for ACME state) — a Devin-caught regression where `!override` silently dropped these mounts under the new layout, defeating the parquet-download perf bypass.

## [0.36.0] — 2026-05-05

Combined performance + analyst-clarity bundle. Folds three previously-staged work streams into one PR (#188): the long-running `agnes query --remote` timeout (#181), the Caddy parquet-download bypass (#182), and Pavel's #185 Phase 1 trace findings (silent 44-min first-init, opaque CLI tracebacks, no analyst-Claude size signal). Also performs the Tier 1 event-loop unblocking — the five hottest BQ-touching endpoints were `async def` over synchronous DuckDB / BQ-extension calls, so a single heavy `agnes query --remote` froze every other request for the duration of the BQ wait. The image-side fixes ship in this release; for existing VMs, the new auto-upgrade.sh self-fetches the matching Caddyfile + compose overlays from `main` on its next 5-minute tick, so deployment requires no operator action beyond letting the cron run.

### Added
- **`data_source.bigquery.query_timeout_ms` config knob** (default 600 000 ms = 10 min). The DuckDB BigQuery extension's built-in default of 90 s was too tight for analyst-scale queries against view-backed BQ datasets — `agnes query --remote` would HTTP 400 with `Binder Error: Query execution exceeded the timeout. Job ID: …` whenever the underlying BQ job took longer than 90 s, even though the BQ job itself was healthy. The new knob is applied via `SET bq_query_timeout_ms` after every `LOAD bigquery` on every BQ-touching DuckDB session — the orchestrator's `_remote_attach` ATTACH path (`src/orchestrator.py`), the analytics-DB read-only reattach path (`src/db.py:_reattach_remote_extensions` — the primary `agnes query --remote` request path), the `BqAccess` session factory (`connectors/bigquery/access.py`), and the standalone extractor (`connectors/bigquery/extractor.py`). Sentinel `0` (or non-numeric / unparseable values) leaves the extension default in place so operators on legacy extension versions that don't recognise the setting aren't broken. Configurable via `/admin/server-config` UI. Note: BigQuery's `jobs.query` RPC caps the wait at ~200 s per call regardless of this setting; the extension polls on top so the effective ceiling is the value here but each poll is ~200 s. DuckDB emits an informational warning when the value is set above the BQ RPC cap — operators can safely ignore it.
- **Per-user parallel parquet downloads in `agnes pull`** — the download loop in `cli/lib/pull.py` now uses a `ThreadPoolExecutor` with concurrency capped by the new `AGNES_PULL_PARALLELISM` env var (default 4, set 1 to restore pre-PR serial behavior). On a registry of N tables the wall-clock time drops from `Σ stream_download_seconds(table_i)` to roughly `max × ceil(N/4)`. Works hand-in-hand with the Caddy `file_server` change below: without it parallel client-side downloads would still queue on the single uvicorn worker; with it each request is its own caddy goroutine + sendfile, so 4-way parallelism actually delivers throughput. Per-table error semantics preserved — a failure on one table no longer aborts the rest of the batch.
- **`agnes init` / `agnes pull --skip-materialize`** — opts the first sync out of materialized-mode tables (server-side scheduled-query parquets, often multi-GB). Pavel's #185 Phase 1: a single 6.3 GB `order_economics` parquet kept first init silent for 44 minutes. Materialized rows stay discoverable via `agnes catalog`; rerun without the flag once the analyst actually needs them locally.
- **`agnes pull` progress bar** — Rich-driven aggregate transfer display rendered to stderr when not `--quiet` and not `--json`. Per-file label + bytes / total / rate / ETA, aggregated across the parallel `ThreadPoolExecutor` workers introduced earlier in this PR. Replaces the prior 0-stdout silence on first init.
- **CLI clean-error wrapper** (`cli/main.py:_run_with_clean_errors`, new entry point in `pyproject.toml`) — `httpx.ReadTimeout` / `ConnectError` / `RemoteProtocolError` etc. used to dump a five-frame Python traceback to the analyst's terminal when a `agnes query --remote` against a slow BQ view timed out client-side. Now: one-line `Error: …` message + actionable hint (e.g. "narrow the WHERE on the partition column from `agnes catalog --json`, or run `agnes snapshot create --estimate`"), exit code 1. Full traceback is appended to `~/.config/agnes/last-error.log` so an operator can recover it for support without spamming the analyst's terminal. Implemented as `AgnesTransportError` raised from the `api_get` / `api_post` / `api_delete` / `api_patch` / `stream_download` helpers in `cli/client.py`; the top-level Typer wrapper renders it. Unhandled `Exception`s are caught at the same boundary, logged, and printed as "internal CLI error (see logfile)" so a Python traceback never leaks to the analyst.
- **`scripts/ops/agnes-auto-upgrade.sh` now re-fetches Caddyfile + every compose overlay** from `keboola/agnes-the-ai-analyst@main` on every tick, hashes them, and triggers a `docker compose up -d` recreation when the hash changes — same path as an image-digest change. Pre-fix the script only watched `docker images` digests, so a Caddyfile or compose change in main never reached running VMs (only fresh boots ran `startup.sh`'s file fetch). Without this, the new file_server downloads-path below would land in the image but stay inert against an old Caddyfile. The script also self-updates from the same path so the very fix that watches config files isn't itself stuck on running VMs. Fail-soft on curl errors — keeps the existing file rather than blanking it.
- **Caddy `file_server` for parquet downloads** — `GET /api/data/{table_id}/download` is now intercepted at the Caddy layer (TLS profile only) and served directly via sendfile/zero-copy from the data volume mounted read-only at `/srv` inside the caddy container. Caddy authorises every request via a new lightweight RBAC probe `GET /api/data/{table_id}/check-access` (returns 204 when the caller has read access on the table, 403 otherwise) using the `forward_auth` directive — the bulk byte transfer never touches uvicorn workers. Resolves a real production failure mode where a single multi-GB analyst pull held the app's only uvicorn worker for the duration of the stream and starved the UI / `/api/health` / every other API endpoint, eventually flipping the container to `unhealthy`. Path discovery uses Caddy's `try_files` over the known `extract.duckdb` v2 source subdirs (`bigquery/data/<id>.parquet`, `keboola/data/<id>.parquet`, `jira/data/<id>.parquet`); a parquet not at any of those paths transparently falls through to the existing app handler so legacy `src_data/parquet` layouts and future connectors keep working with no Caddyfile change. Non-Caddy deployments (dev `docker compose up` without `--profile tls`) continue to use the app handler unchanged.
- **Workspace prompt: decision tree, common-mistakes callout, failure-mode dictionary** in `config/claude_md_template.txt` (the template `agnes init` writes to `<workspace>/CLAUDE.md`). Surfaces every catalog-row field analyst Claude should read before deciding which command to use (`query_mode`, `sql_flavor`, `where_examples`, `fetch_via`, `rough_size_hint`); explicitly binds `--estimate` to `agnes snapshot create` ONLY (was the most-failed first-try misuse — fails with `No such option: --estimate` on `agnes query`); calls out the `agnes fetch` → `agnes snapshot create` rename so stale-doc analysts don't run a non-command; documents the BQ permission model (server SA, not personal Google identity) and a 6-row failure-mode table mapping each common error wording to its cause + the right next step.
- **`rough_size_hint` populated for `local` + `materialized` catalog rows** in `GET /api/v2/catalog` (was hardcoded `null` with a "Task 8" TODO). Reads the parquet file size at `${DATA_DIR}/extracts/<source_type>/data/<table_id>.parquet` and buckets into `small` (≤100 MiB), `medium` (≤1 GiB), `large` (≤10 GiB), `very_large` (>10 GiB). `remote` rows stay `null` for now (size requires a BQ INFORMATION_SCHEMA call; tracked separately). Lets analyst Claude pick `agnes snapshot create` over `agnes query --remote` by inspecting `agnes catalog --json` rather than discovering size empirically via a failed `--remote` round-trip.

### Changed
- **Tier 1 event-loop unblocking** — the five hottest BQ-touching endpoints (`POST /api/query`, `POST /api/v2/scan`, `POST /api/v2/scan/estimate`, `GET /api/v2/sample/{id}`, `GET /api/v2/schema/{id}`) were declared `async def` but invoked synchronous DuckDB / BQ-extension calls inside the body. Under uvicorn's single event loop that meant a single heavy `agnes query --remote` (waiting up to ~200 s for BQ's `jobs.query` to return) **froze every other request** — `/api/health`, the dashboard, auth, even another query — for the full duration of the BQ wait. Operators saw "VM idle, app frozen" symptoms during this work. Converted all five to plain `def` so FastAPI auto-offloads the blocking body to the anyio thread pool; the event loop stays free for non-BQ requests. Verified via 0-await audit (no `await` statements in the converted handlers, so the rename is safe). Tests: `tests/test_v2_*.py` were rewritten to call the handlers directly instead of `asyncio.run(...)` (which now fails on a non-coroutine return). Pairs with the thread-pool capacity bump below.
- **`AGNES_THREADPOOL_SIZE` env var** (default 200, was anyio's stock 40) controls the FastAPI / Starlette thread pool capacity used by every plain-`def` route handler. Set in `app/main.py:lifespan` via `anyio.to_thread.current_default_thread_limiter().total_tokens`. 200 leaves comfortable headroom over the BQ extension's connection budget while keeping the per-process thread cost bounded — for the workload of <50 concurrent analysts this is well over what's needed; bump for higher concurrency.
- **CLI update-banner now says `agnes` instead of `da`** (`cli/update_check.py:format_outdated_notice`). The string `[update] da X is out of date` had survived the `da` → `agnes` CLI rename and was the most-visible stale identifier in the analyst-facing surface — every CLI command printed it on stderr when a newer wheel was available.

### Fixed

- **CLI ReadTimeout message reports the actual httpx timeout** (was hardcoded to `QUERY_TIMEOUT_S` = 300s). On a 30s-default call (`agnes catalog`, `agnes auth`, …) the analyst saw "didn't respond within the read timeout (300s)" while the call had actually given up after 30s — confusing and unactionable. The translator now takes the real timeout from the calling helper and renders it; the long-running-BQ advisory only appears for calls where the timeout was set ≥ 60s. Devin Review on PR #188.
- Keboola sync now falls back to the legacy Storage-API client when the DuckDB Keboola extension's per-table scan fails, not just when the initial `ATTACH` fails. Two changes:
  - `kbcstorage>=0.9.0` is promoted from optional to core dependency. The legacy fallback path in `connectors/keboola/extractor.py:_extract_via_legacy` has been there since the extension landed, but until now the bare `from kbcstorage.client import Client` would crash any default install with `ModuleNotFoundError`.
  - `connectors/keboola/extractor.py:run` now wraps `_extract_via_extension` in a per-table try/except — on any per-table scan failure it retries via the legacy client. Previously, when `ATTACH` succeeded but the table-level `COPY (SELECT * FROM kbc."<bucket>"."<table>")` failed, the table was just marked failed with no retry.
  Together these unblock deployments where the extension's bucket-schema scans return `Schema '..."in.c-..."' does not exist or not authorized` (keboola/duckdb-extension#17) while the upstream extension fix is in flight.

## [0.35.1] — 2026-05-05

### Fixed

- `agnes query --remote` no longer dies after 30s on long-running BigQuery SELECTs. The CLI HTTP client now defaults to a 300s timeout for `/api/query` and exposes `AGNES_QUERY_TIMEOUT` (seconds, float) for operators who need to extend it further. Other CLI calls keep the 30s default. (`cli/client.py`, `cli/commands/query.py`)

## [0.35.0] — 2026-05-05

Five-defect fix for the silently-broken session pipeline on default Compose deploys (#176). Sessions uploaded by `agnes push` landed on `/data/user_sessions/<user>/*.jsonl`, but on a stock `docker compose up` deploy nothing ever processed them — `/corporate-memory` stayed empty even when sessions and `CLAUDE.local.md` were uploaded. The root cause was a stack of compounding defects: LLM SDKs were dev-only deps so the scheduler container boot-looped on `ModuleNotFoundError`, the side-car services were profile-gated and ran as tight `restart: unless-stopped` boot loops anyway, the `verification_detector` had no scheduler entry at all, the first-time setup never seeded an `ai:` block, and the `/corporate-memory` page silently filtered out the pending review queue. This release wires the LLM pipeline into the existing scheduler-v2 model (one HTTP-driven cron tick per service) and adds a health-check that warns when uploaded jsonls aren't being processed.

### Changed

- **BREAKING** `docker-compose.yml` and `docker-compose.prod.yml` no longer ship the `corporate-memory` and `session-collector` services. The scheduler container drives both jobs through admin HTTP endpoints (see Added below) on offset cadences (10 min / 17 min). Operators previously running `COMPOSE_PROFILES=full` or maintaining custom Compose overrides need to drop those service stanzas — leaving them in produces a double-driver footgun (the standalone container loop races the scheduler-v2 cron tick on `/data/user_sessions` and `knowledge_items` writes). The Python entry points (`services/{corporate_memory, session_collector, verification_detector}/__main__.py`) remain — they're still callable from the CLI for one-shot manual runs and from the new admin endpoints.

### Added

- New admin endpoints in `app/api/admin.py` that wrap the LLM pipeline jobs so the scheduler can drive them over HTTP (matching the existing `/api/marketplaces/sync-all` pattern):
  - `POST /api/admin/run-session-collector` — copies Claude Code session jsonls from user homes to `/data/user_sessions/<user>/`.
  - `POST /api/admin/run-verification-detector` — extracts verified knowledge from session transcripts via the LLM, writes pending items to `knowledge_items`.
  - `POST /api/admin/run-corporate-memory` — refreshes the catalog from team `CLAUDE.local.md` files.
  All three are admin-gated, sync-def (FastAPI thread pool), and emit one audit row per invocation.
- Three new entries in `services/scheduler/__main__.py:JOBS` with deliberately offset cadences (10 m / 15 m / 17 m, all coprime modulo the 30 s tick) so the LLM-backed jobs don't fire on the same tick and stack their API + DB load:
  - `session-collector` — every 10 min → `POST /api/admin/run-session-collector`.
  - `verification-detector` — every 15 min → `POST /api/admin/run-verification-detector`.
  - `corporate-memory` — every 17 min → `POST /api/admin/run-corporate-memory`.
- `connectors.llm.factory.create_extractor_from_env_or_config(ai_config)` — falls back to `ANTHROPIC_API_KEY` / `LLM_API_KEY` env vars when the `ai:` block is empty, raises a clear `ValueError` when neither is available. `services/corporate_memory` and `services/verification_detector` switch to the new helper so a missing `ai:` section is no longer a silent skip.
- `POST /api/admin/configure` now seeds a default `ai:` block into the writable `instance.yaml` overlay when the overlay has no `ai:` yet AND `ANTHROPIC_API_KEY` (or `LLM_API_KEY`) is present in the environment. The block stores the env-var reference (`${ANTHROPIC_API_KEY}`), never the raw secret. Existing operator config is preserved verbatim.
- `/corporate-memory` page renders an admin-only banner (`N pending items awaiting review — review them at /corporate-memory/admin`) when the pending review queue is non-empty. Non-admins see no change — the route zeroes the count server-side before the template renders. Closes the silent-failure UX gap that hid the review queue from operators with `approval_mode='review_queue'` (the default).
- `GET /api/health/detailed` now returns a `session_pipeline` service entry that warns when uploaded session jsonls aren't being processed. Heuristic: `max(mtime of /data/user_sessions/**/*.jsonl) <= max(processed_at in session_extraction_state) + grace_seconds`, where `grace_seconds = 2 ×` the verification-detector cadence (default 30 min, configurable via `SCHEDULER_VERIFICATION_DETECTOR_INTERVAL`). Surfaces as `status='warning'` (never `error`) with an actionable `detail` pointing at the verification-detector job. A warning bubbles up to the existing `overall='degraded'` aggregation so `agnes diagnose system` flags it.

### Fixed

- **Defect 4 — LLM provider SDKs in dev-only deps caused scheduler container boot loops.** `anthropic>=0.30.0` and `openai>=1.30.0` are now in `[project].dependencies`, not `[project.optional-dependencies].dev`. The Dockerfile's `uv pip install --system --no-cache .` picks them up automatically, no Dockerfile change required. `tests/test_packaging.py` locks the contract.
- **Defect 5 — first-time setup never wrote an `ai:` block.** Two paths to a working LLM pipeline now actually work end-to-end (#179 review): (a) a default `ai:` block seeded by `POST /api/admin/configure` into the writable overlay at `${DATA_DIR}/state/instance.yaml` when env keys are present (Added above), or (b) env-var fallback at service start time. The seeded overlay path was dead code on the initial 0.35.0 cut — the three LLM consumers (`services/corporate_memory/collector.py`, `services/verification_detector/__main__.py`, `app/api/admin.py:run_verification_detector`) imported `load_instance_config` from `config.loader` (which only reads the static config dir), and even if they had read the overlay, `app/instance_config.py` ran `yaml.safe_load` on it without resolving `${ENV_VAR}` references so the seeded `${ANTHROPIC_API_KEY}` placeholder would have stayed literal. Both fixes shipped: consumers switched to the overlay-aware `app.instance_config.load_instance_config`, and the overlay is now passed through `config.loader._resolve_env_refs` before deep-merge with the static base. `collect_all` no longer swallows the factory's `ValueError` into `stats["errors"]` — fail-fast propagates so the scheduler / admin endpoint surface the actionable misconfiguration message.
- **#179 review — scheduler ignored its own LLM cadence env vars.** `app/api/health.py` already read `SCHEDULER_VERIFICATION_DETECTOR_INTERVAL` to compute the staleness grace window, but the scheduler cadence was hardcoded to `every 15m`, so an operator throttling the detector via the env was silently ignored on the schedule side while the health grace silently widened. All three LLM-pipeline cadences are now env-driven through the same `_read_positive_int` pattern as `data-refresh` / `health-check` / `script-runner`: `SCHEDULER_SESSION_COLLECTOR_INTERVAL` (default 600s = 10m), `SCHEDULER_VERIFICATION_DETECTOR_INTERVAL` (default 900s = 15m), and `SCHEDULER_CORPORATE_MEMORY_INTERVAL` (default 1020s = 17m). Defaults preserve the 10/15/17m coprime offset so the three jobs don't fire on the same tick. The verification-detector env var remains the single source of truth for the health-check grace (still `2 ×` the cadence).
- **Defect 3 — `verification_detector` had no scheduler entry.** Now in `JOBS` with a 15 min cadence, hitting the new `/api/admin/run-verification-detector` endpoint.
- **Defect 2 — side-car services gated by `profiles: [full]` were silently skipped on default deploys.** Both stanzas dropped (Changed above); the scheduler-v2 cron is the sole driver.
- **Defect 1 — `/corporate-memory` filtered `status IN ('approved','mandatory')` with no hint that pending items existed.** Admin banner added (Added above).
- **#179 review — `/api/admin/run-session-collector` would SystemExit the worker.** The endpoint called `collector.main()`, whose `argparse.parse_args()` parsed uvicorn's `sys.argv` (`['app.main:app', '--host', …]`) and called `sys.exit(2)` on the unrecognised flags. `SystemExit` inherits from `BaseException`, escapes FastAPI's exception machinery, and propagates through the thread pool — every scheduler tick that fired the endpoint either 500-ed or risked killing the uvicorn worker. Fix: `services/session_collector/collector.py` now exposes an argv-free `run(dry_run, verbose) -> (rc, stats)` helper; `main()` is a thin CLI shim around it and the admin endpoint calls `run()` directly. Audit log now carries the per-run stats (`users_processed`, `files_copied`, `files_skipped`) instead of just the rc. Regression tests in `tests/test_session_collector.py::TestRunHelper`.
- **#179 review — `python -m services.corporate_memory` crashed on missing LLM config instead of exiting cleanly.** The PR's fail-fast change made `collect_all()` raise `ValueError` when neither an `ai:` block nor `ANTHROPIC_API_KEY`/`LLM_API_KEY` was available. The `verification_detector` CLI was updated to catch it; the corporate-memory CLI was missed. Now also wrapped — operators get a one-line `Corporate Memory cannot run: <factory message>` on stderr and rc=1 instead of a raw traceback. Regression test in `tests/test_llm_connector.py::TestCorporateMemoryCollector::test_main_returns_1_on_no_ai_config_instead_of_traceback`.
- **E2E test — Anthropic API rejected every extraction request.** The structured-output API now requires `additionalProperties: false` on every `{"type": "object"}` node in the json_schema; without it the API returns 400 `invalid_request_error` ("output_config.format.schema: For 'object' type, 'additionalProperties' must be explicitly set to false"). Surfaced on a real BQ-backed deploy: every uploaded session jsonl failed verification-extraction in a tight retry loop. Fix: `connectors/llm/anthropic_provider.py` now wraps the caller-supplied schema through a recursive `_strict_json_schema()` walker that adds the field where missing (preserving any explicit operator override), then passes the strict variant to the API. Six unit tests in `tests/test_llm_connector.py::TestStrictJsonSchema` pin the recursion across nested objects, array items, and the no-mutation invariant.
- **#179 review — `/api/admin/run-verification-detector` skipped audit on unhandled exceptions.** If `detector.run()` threw anything other than the already-translated `ValueError` (DuckDB lock, network blip, unexpected SDK error), the audit_log row was never written — the operator's only signal was `docker logs agnes-scheduler-1`. The endpoint now wraps `detector.run` in try/except, records the exception in `audit_params["unhandled_error"]`, then re-raises as 500 after audit. The `/admin/scheduler-runs` page surfaces the failure row with the error type and message.
- **#179 review — `SCHEDULER_AUDIT_ACTIONS` listed action strings that don't actually appear in `audit_log`.** The list at `app/web/router.py:952` had `"marketplaces_sync_all"` (wrong — actual is `"marketplace.sync_all"`) plus `"data_refresh"` and `"scripts_run_due"` (which `app/api/sync.py` and `app/api/scripts.py` don't write). Corrected to the four actually-logged strings, with a comment pointing at the missing audit calls in sync/scripts as a follow-up.
- **#179 review — `/api/admin/run-corporate-memory` skipped audit on unhandled exceptions** (same gap as `run_verification_detector` from the previous round). Mirrored the same try/except + `unhandled_error` audit pattern, so a DuckDB lock or unexpected SDK error from `collect_all()` now produces an audit row with the error type+message before re-raising as 500. Regression test in `tests/test_admin_run_endpoints.py::TestRunCorporateMemory::test_unhandled_exception_still_audits`.
- **#179 review — `/api/admin/run-session-collector` skipped audit on unhandled exceptions** (third occurrence of the same pattern, completes the trilogy of LLM-pipeline endpoints). Mirrored the same try/except + `unhandled_error` audit pattern from the other two endpoints, so a `PermissionError` walking `/home`, an `OSError` on `/data/user_sessions` mkdir, or any other unhandled exception from `collector.run()` now produces an audit row before re-raising as 500. Regression test in `tests/test_admin_run_endpoints.py::TestRunSessionCollector::test_unhandled_exception_still_audits`.
- **#179 review — `/profile/sessions` 500-ed on transient `stat()` failure.** The previous implementation used `sorted(glob, key=lambda p: p.stat().st_mtime)`; if any single jsonl file's stat call raised (race with delete, EACCES from a remount, etc.), the whole sort raised and the page returned 500 instead of just dropping that one row. Reworked the gather: stat each path under try/except into a `(path, stat)` list, then sort the already-statted entries. Bad files are silently dropped from the listing. Regression test in `tests/test_web_ui.py::TestAdminRoleGuards::test_profile_sessions_page_tolerates_stat_failures`.

### Added

- `/admin/scheduler-runs` — read-only admin page showing the last 200 audit-log entries from scheduler-driven actions (`run_session_collector`, `run_verification_detector`, `run_corporate_memory`, `marketplace.sync_all`). New `AuditRepository.query_actions(actions, limit)` query helper, new admin nav entry under the Admin dropdown. `data-refresh` (`POST /api/sync/trigger`) and `script-runner` (`POST /api/scripts/run-due`) are scheduler jobs but don't write to `audit_log` today, so they can't appear here yet. Failed scheduler ticks (HTTP 401, network errors) don't reach the audit_log either — those still live only in `docker logs agnes-scheduler-1`; the page calls that out with a hint to set `SCHEDULER_API_TOKEN` if no rows show up.
- `/profile/sessions` — self-service user page in the user menu, showing all session jsonls the caller uploaded via `agnes push` joined against `session_extraction_state`. Each row shows uploaded_at, file size, status badge (`pending` / `processed` / `extracted`), processed_at, `items_extracted`, and a per-row Download button. The page docstring explicitly calls out that `items_extracted = 0` means the verification detector ran successfully but the LLM found no claims worth tracking — that's the documented "no items" outcome, not a broken pipeline. Closes the gap surfaced during the e2e test of #176 where a user could see their sessions on disk and process them through the LLM but had no UI to inspect what happened.
- `GET /profile/sessions/<filename>` — owner-only download of a single jsonl. Auth via `get_current_user`; path safety locks the served file under `${DATA_DIR}/user_sessions/<caller.id>/` and rejects path-traversal / nested-component / non-`.jsonl` / dotfile filenames with 404 (never 403, so existence of files belonging to other users is not leaked). `Content-Disposition: attachment` returns the file as a download.

### Internal

- `tests/test_packaging.py` — guards against `anthropic`/`openai` slipping back into dev extras.
- `tests/test_setup_ai_block.py` — overlay seeding contract for `POST /api/admin/configure`.
- `tests/test_llm_provider_env_fallback.py` — env fallback + fail-fast for `create_extractor_from_env_or_config`.
- `tests/test_admin_run_endpoints.py` — admin gating + scheduler registration + endpoint contract for the three new run-* endpoints.
- `tests/test_docker_compose.py` — pins the compose contract: the two side-car services must not reappear under either Compose file.
- `tests/test_corporate_memory_page.py` — pending-banner contract (admin sees, non-admin doesn't).
- `tests/test_health_session_pipeline.py` — session-pipeline staleness check across cold-start + ok + warning + never-processed cases.
- `tests/test_instance_config_overlay.py` — pins overlay env-ref resolution + the three LLM consumers reading from `app.instance_config` (#179 review).
- `tests/test_scheduler.py` — `TestLLMPipelineCadenceEnvVars` + `TestVerificationDetectorGraceFollowsCadence` pin the new env-var-driven cadences and the single-source-of-truth contract between scheduler and health-check grace (#179 review).
- `docs/architecture.md` — Services table updated to reflect the scheduler-v2 cadence map.

## [0.34.0] — 2026-05-04

End-to-end clean-analyst-bootstrap rewrite. The web `/setup` page now produces a single unified paste prompt that, dropped into Claude Code in an empty folder, fully bootstraps a workspace — installs the CLI, authenticates, fetches `CLAUDE.md`, installs SessionStart/End hooks, runs the first data refresh, and writes a human-readable workspace docs file (`AGNES_WORKSPACE.md`). The admin-vs-analyst layout split (introduced as `?role=` mid-cycle) was collapsed before merge: every caller sees the same flow, with the marketplace + plugins block emitted iff the caller has plugin grants. 26 implementation tasks across 6 phases plus a 10-task unification follow-up.

### Changed
- **BREAKING** CLI binary renamed from `da` to `agnes`. No backward-compat alias is shipped. Update shell aliases, hook commands in any pre-existing `.claude/settings.json`, scripts, and cron jobs. Reinstall via `uv tool install <wheel>`; the wheel now ships an `agnes` entry point.
- **BREAKING** Environment variables and config dir renamed: `DA_CONFIG_DIR/DA_SERVER/DA_NO_UPDATE_CHECK/DA_LOCAL_DIR/DA_TOKEN/DA_STREAM_RETRIES` → `AGNES_*`; `~/.config/da/` → `~/.config/agnes/`. Hard cutover, no fallback. Existing analysts re-authenticate via `agnes auth import-token`.
- **BREAKING** Analyst bootstrap rewritten end-to-end. `da analyst setup` is removed; replaced by `agnes init` (non-interactive, requires `--server-url` and `--token`). `da sync` is split into `agnes pull` (refresh) and `agnes push` (upload). `da fetch` is folded into `agnes snapshot create`. `da metrics list/show` is folded into `agnes catalog --metrics`; `da metrics import/export/validate` move to `agnes admin metrics {import,export,validate}`. The `da analyst` namespace is removed; the workspace status command is now `agnes status`. The previous `da status` (server-health overview) becomes `agnes diagnose system`.
- **BREAKING** Workspace layout simplified. Removed: `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Canonical paths: `server/parquet/` (synced parquets), `user/duckdb/analytics.duckdb` (DuckDB views), `user/snapshots/` (ad-hoc snapshots), `user/sessions/` (recorded sessions). Lazy-mkdir contract — no empty pre-allocated directories.
- **BREAKING** `/setup` is now a single unified flow regardless of caller's role. The `?role=` query parameter (introduced earlier in this Unreleased cycle but never released) is removed before merge — no migration needed. The admin tile is gone. PAT scope is uniform: every install-page mint uses `scope=general` with `expires_in_days=90`, calling the existing `POST /auth/tokens` endpoint. The `bootstrap-analyst` 1 h-clamped scope is no longer used from `/setup` (still defined in code for future reuse, see open issue for redesign). The marketplace + plugins block is emitted iff the caller has plugin grants in `resource_grants`. `agnes init` is now part of every setup flow (admin and analyst alike) — it's the workspace-rails delivery mechanism. `/install` continues to 302 to `/setup`.
- `CLAUDE.md` server-side template + repo-root `CLAUDE.md` updated to reference the new CLI verbs and workspace paths. The admin UI for the `claude_md_template` DB override (`/admin/workspace-prompt`) renders a yellow banner when the saved override contains legacy strings (`data/parquet/`, `da sync`, `da fetch`, `da analyst setup`, `da metrics list/show`); admins re-author and save to clear it. Migration is manual.

### Added
- `agnes init <opts>` — non-interactive workspace bootstrap orchestrator. 8 steps: detect existing workspace, verify PAT (`GET /api/catalog/tables`), save config + token globally, fetch `CLAUDE.md` from `/api/welcome`, install SessionStart/End hooks via `cli/lib/hooks.py:install_claude_hooks`, write `CLAUDE.local.md` stub (preserved on `--force`), run first `agnes pull`, write `AGNES_WORKSPACE.md`. Errors render via `cli/error_render.py:render_error()` with typed kinds (`auth_failed`, `server_unreachable`, `partial_state`, `manifest_unauthorized`).
- `agnes pull` / `agnes push` — split from the old `da sync` / `da sync --upload-only`. `--quiet` / `--json` / `--dry-run` flags. SessionStart hook runs `agnes pull --quiet`; SessionEnd hook runs `agnes push --quiet`.
- `agnes snapshot create <table>` — folded from `da fetch`. Adds `if not local_db.exists()` guard so `agnes snapshot create` no longer silently materializes an empty DuckDB file when run before any `agnes pull`.
- `agnes catalog --metrics` (replaces `da metrics list`) and `agnes catalog --metrics --show <id>` (replaces `da metrics show`).
- `agnes admin metrics {import,export,validate}` — write paths relocated from the deleted `da metrics` namespace.
- `agnes diagnose system` — server-side health check (was the old `da status`).
- `AGNES_WORKSPACE.md` — human-readable workspace docs file generated by `agnes init` in the workspace root. Documents global install, workspace layout, hooks, cheat sheet, uninstall recipe.
- PAT request body now accepts `scope: str = "general"` and `ttl_seconds: int | None = None` fields. PATs minted with `scope="bootstrap-analyst"` are TTL-clamped to ≤ 1 h server-side. Existing `expires_in_days` field continues to work; `ttl_seconds` wins when both are set. `ttl_seconds` upper bound is 315_360_000 (matches `expires_in_days <= 3650` cap). JWT carries the `scope` claim via new `extra_claims` parameter on `create_access_token`; reserved keys (`sub`/`email`/`typ`/`iat`/`jti`/`exp`) cannot be overridden via `extra_claims`. Audit log includes the scope.
- `cli/lib/` shared-library tree with `cli/lib/pull.py:run_pull` (data-refresh primitive callable from both the Typer wrapper and `agnes init`) and `cli/lib/hooks.py:install_claude_hooks` (workspace-scoped, idempotent Claude Code hook installer).
- `_scan_legacy_strings` helper + `legacy_strings_detected` field on `GET /api/admin/workspace-prompt-template` — server scans saved CLAUDE.md overrides for stale CLI verbs / paths; the admin UI banner consumes the field.
- `/setup` pre-flight check (step 4, gated on the marketplace block being present) now verifies `claude --version` in addition to `git --version`. Both binaries are needed by `claude plugin marketplace add` and the git-clone fallback — checking them together surfaces a clear "install X" message instead of a confusing downstream error. Install hints: `npm i -g @anthropic-ai/claude-code` for Linux/WSL plus a doc URL (`https://docs.claude.com/claude-code`) for macOS / Windows native installers.

### Fixed
- `agnes pull` (formerly `da sync`) no longer creates `.claude/rules/` when the corporate-memory bundle is empty.
- `agnes pull` no longer creates `server/parquet/` when the manifest is empty (mkdir is lazy — only on first per-table write).
- `agnes snapshot create` (formerly `da fetch`) no longer materializes an empty `user/duckdb/analytics.duckdb` when run before any `agnes pull`. Friendly hint redirects to `agnes pull`.
- Workspace `agnes status` reads from the canonical `server/parquet/` and `user/duckdb/analytics.duckdb` paths (was reading legacy `data/parquet/`, `data/metadata/last_sync.json`).
- `agnes init` and `agnes pull` errors now use the `cli/error_render.py` typed-error renderer (added in 0.32.0), so analyst-facing error UX matches the structured shape `agnes query --remote` already produces.
- **Schema v24 migration retry path is no longer dead** (Devin Review on `db.py:1757`, escalated from advisory to critical on rescan). Pre-fix: when `_v23_to_v24_finalize` had materialized BQ rows to migrate but `data_source.bigquery.project` was not configured, it logged a warning per row and returned normally. The schema_version then bumped to 24 unconditionally, the `if current < 24:` gate in `_ensure_schema` skipped the function on every subsequent startup, and the affected rows kept their DuckDB-flavor `bq."ds"."tbl"` source_query forever — which the new `_wrap_admin_sql_for_jobs_api` wrapping path rejects as unparseable BQ SQL with no automatic recovery. The "set the project and restart to retry" log hint pointed at a code path that no longer ran. Fix: the migration now raises `RuntimeError` BEFORE the schema_version bump when it has rows to migrate but no project_id, blocking startup with a clear actionable error pointing at `data_source.bigquery.project`. Operator configures the project, restarts, and the migration completes (schema_version is still at 23, so the `if current < 24:` gate fires). Side effect: a BQ-using deployment that hasn't set the project blocks startup until they do — that's the right call for a config error that would otherwise silently break materialized tables. Two regression tests in `test_schema_v24_source_query_rewrite.py`: `test_v24_raises_when_project_not_configured_and_rows_need_migration` (raise + version-stays-at-23) and `test_v24_skips_clean_when_no_rows_match_even_without_project` (no-rows-no-block invariant).
- **`agnes admin register-table` UX**: three real-world feedback items addressed.
  - **`--query-mode materialized` now requires `--bucket`** (client-side validation; exits with a clear error before hitting the server). The previous help docstring claimed `--bucket` was *ignored* for materialized rows, but the value is actually load-bearing — `agnes schema <name>` builds the BQ identifier as `bq.<bucket>.<source_table>`, so an empty bucket registered the row but broke subsequent schema/describe with HTTP 400 "unsafe BQ identifier in registry". Docstring rewritten to reflect reality.
  - **Post-success hints**: after a successful registration the CLI now points operators at the two follow-ups they routinely miss: (a) `agnes setup first-sync` to materialize the parquet (registration alone doesn't trigger a build; `agnes pull` reports "Updated 0 tables" until the scheduler tick), and (b) `agnes admin grant create <group> table <name>` to make the row visible in `agnes catalog` for non-admin users (catalog is RBAC-filtered).
  - Test coverage: `tests/test_cli_admin_materialized.py::test_register_materialized_without_bucket_fails_with_clear_error` and `test_register_table_emits_first_sync_and_grant_hints`.
- **`agnes query --remote` SQL rewriter no longer corrupts output when the GCP project ID contains a registered table name as a hyphen-delimited word** (Devin Review on `query.py:464`). The previous iterative rewrite (one `re.sub(\b<name>\b, ...)` per registered name) was vulnerable to cross-contamination: e.g. project `my-ue-project` + registered `orders` + registered `ue` → iter 1 rewrites `orders` to `\`my-ue-project.fin.orders\``, iter 2's `\bue\b` then matches the `ue` INSIDE `my-ue-project` and corrupts the iter-1 path. Fix: replaced the iteration with a SINGLE `re.sub` whose alternation regex (sorted longest-first) handles every name in one pass, so freshly-inserted backticked text isn't re-scanned. The fallback at `query.py:576` (per-table SELECT * on BQ parse error) caught the corrupted output as `bq_bad_request` so impact was over-estimation rather than fail-open, but the partition-pruning benefit of #171 is now preserved for projects whose IDs share a hyphen-segment with a registered table name. Regression test in `tests/test_api_query_guardrail.py::test_rewrite_helper_does_not_corrupt_when_project_id_contains_registered_name`.
- **BigQuery materialize TTL reclaim is no longer dead code** (Devin Review on `extractor.py:166`). `_try_acquire_file_lock` used to call `open(lock_path, mode="w")` BEFORE checking the lock-file mtime, which truncated the file and refreshed mtime to *now* on every invocation. The subsequent `time.time() - lock_path.stat().st_mtime` always saw age ~0, so `age > TTL` never fired, and `materialize.lock_ttl_seconds` was a silently no-op config knob. Fix: stat the lock path BEFORE any `open()` to read the real pre-probe mtime; if older than TTL, unlink (forcing a fresh inode for the next `open + flock`); only then probe. Two regression tests added: `test_stale_held_lock_is_reclaimed_despite_live_holder` exercises the full reclaim path with a still-living fcntl holder, `test_failed_probe_does_not_self_refresh_lock_mtime` pins that a failed acquisition doesn't pathologically loop. Residual cross-process risk (a genuinely overrunning materialize past TTL races a fresh attempt) is documented in the helper docstring; in-process `threading.Lock` keyed on `table_id` blocks the single-process race.
- **`agnes init --token X` now correctly uses the explicit token in the verify call**, even when `~/.config/agnes/token.json` already holds a stale token from a prior install. Pre-fix `cli.config.get_token()` read the on-disk file first and only fell back to env vars, so step 2 (PAT-verify) ran with the stale token and failed with a confusing 401 — even though the `--token` arg was valid (Devin Review on `init.py:99`). Fix: a `ContextVar`-based override in `cli.config` short-circuits `get_token()` before the file read; `_override_server_env` (used by both `agnes init` and `agnes pull`'s `run_pull`) sets it for the duration of the call. Async-safe (each task sees its own override) and leak-proof (resets on context exit).
- **`agnes status` sessions counter now reads the same source as `agnes push`** — `~/.claude/projects/<encoded-cwd>/` (Claude Code's actual write path) with the legacy `<workspace>/user/sessions/` as a fallback, via `cli.lib.claude_sessions.list_session_files()`. Pre-fix the counter only checked the legacy dir and always reported 0 in workspaces bootstrapped with `agnes init` (since Claude Code never writes there).
- **BigQuery materialize lock-reclaim docstring** at `connectors/bigquery/extractor.py:_try_acquire_file_lock` corrected: a still-running holder's `fcntl.flock` does NOT block the post-unlink reacquisition (new file = new inode = independent lock). The in-process `threading.Lock` keyed on `table_id` is the actual concurrency guard; cross-process protection (two schedulers on one workspace) relies on operators not running multiple concurrent schedulers AND on the TTL being well above the longest plausible COPY (24 h default). Documenting the residual risk so it isn't masked by a misleading "we're safe" comment (Devin Review on extractor.py:111).
- **`agnes pull` now re-downloads parquets when the local file is missing, even if the recorded hash matches the server.** Pre-fix the download set was computed from `sync_state.json` hash equality alone — if the parquet had been deleted (manual `rm`, disk cleanup, a different workspace sharing the same global `~/.config/agnes/sync_state.json` writing one workspace's parquets while another reads sync_state and assumes "I already have these"), the hash-equal check would short-circuit the download and the next DuckDB view rebuild would fail on a missing file. Now the existence check on `<workspace>/server/parquet/<tid>.parquet` runs alongside the hash compare; missing file → forced re-download regardless of hash.
- **`agnes query --remote` no longer over-rejects narrow queries on partitioned/clustered BigQuery tables.** Closes #171. Pre-fix the `/api/query` cost guardrail dry-ran a synthetic `SELECT * FROM <table>` per registered remote-BQ row referenced by the user SQL, which forced BQ to estimate "full table scan" — column projection, predicate pushdown, and partition pruning were all ignored, producing scan-byte estimates up to ~30,000× larger than the actual query would scan. Narrow queries on big partitioned tables (the documented happy-path use case) were rejected with 400 `remote_scan_too_large` even when BQ's own dry-run reported single-digit MB. Now the guardrail rewrites the user SQL from DuckDB-flavor (bare registered names + `bq."<ds>"."<tbl>"`) to BQ-native (`` `<project>.<ds>.<tbl>` ``) and runs ONE dry-run on the EXACT user SQL — partition pruning, column projection, and predicate pushdown all engage. Cap check uses the real estimate. Fallback: if BQ rejects the rewritten SQL with `bq_bad_request` (DuckDB-only syntax that doesn't translate, e.g. `::INT` casts), the guardrail falls back to the pre-fix per-table SELECT * estimate so a non-portable query still gets bounded; non-parse errors (forbidden / upstream) propagate as 502. Helpers exported as `_rewrite_user_sql_for_bq_dry_run` (test seam).
- **Windows: `agnes` CLI no longer crashes on cs-CZ / non-UTF-8 consoles.** Two failure modes addressed (originally reported in #172 against the pre-rename `da` CLI; ported and broadened here): (1) `agnes pull` and any other Rich-progress-bar codepath crashed with `UnicodeEncodeError` because cp1250 / cp1252 cannot encode Rich's Braille spinner glyphs — `cli/main.py` now reconfigures `sys.stdout` / `sys.stderr` to UTF-8 with `errors="replace"` at import time when `sys.platform == "win32"`. (2) `agnes skills list` and `agnes skills show` crashed with `UnicodeDecodeError` reading skill markdown that contains em-dashes / accents — every `Path.read_text()` / `Path.write_text()` / `open()` call site in `cli/` (including ones not touched by #172, since several files were renamed in the bootstrap rewrite) now passes `encoding="utf-8"` explicitly. Defensive: also covers JSON / YAML config files that were ASCII-only in practice but were one non-ASCII value away from the same failure mode.
- `agnes snapshot create … --estimate` in a pre-init directory no longer leaks an httpx `ConnectError` traceback to stderr. The estimate-guard fix (3d587681) let `--estimate` reach `api_post_json`, but the existing `except V2ClientError` clause didn't catch transport-layer errors when no server was configured (defaulted to `http://localhost:8000`). Now also catches `httpx.HTTPError` and renders the friendly hint `Run \`agnes init …\` first`.
- `agnes push` now reads Claude Code session jsonls from `~/.claude/projects/<encoded-cwd>/` (where Claude Code actually writes them), instead of `<workspace>/user/sessions/` (which the SessionEnd hook never populated — the previous code uploaded an empty list every time). Encoding logic in `cli/lib/claude_sessions.py` probes both Claude Code variants — older `/`→`-` and newer all-non-alphanumeric→`-` — and unions the result, so users who have upgraded Claude Code mid-project see sessions from both encoded dirs. Falls back to `<workspace>/user/sessions/` for back-compat.

### Removed
- `da analyst setup`, `da analyst status`, `da sync`, `da fetch`, `da metrics`. See **Changed** for replacements.
- `da metrics` namespace as a top-level group (subcommands moved to `agnes catalog --metrics` for read-only views and `agnes admin metrics …` for write operations).
- Legacy workspace directories `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Existing analyst workspaces should be reinitialized with `agnes init --server-url ... --token ... --force` (a fresh empty folder is recommended).
- `_resolve_analyst_lines`, `_analyst_init_lines`, `_analyst_finale_lines` helpers in `app/web/setup_instructions.py` — the analyst-vs-admin layout split is gone. `role` parameter on `compute_default_agent_prompt`, `resolve_lines`, and `render_setup_instructions`. `?role=` query parameter on `/setup`. Admin tile (`<nav class="role-tiles">`) and `ROLE` JS const + role-aware PAT-mint ternary in `install.html`.

### Internal
- `cli/lib/__init__.py` (empty) makes `cli/lib/` a proper package picked up by Hatchling for wheel inclusion. `.gitignore` allowlists `cli/lib/` from the generic `lib/` rule.
- `tests/fixtures/analyst_bootstrap.py` — reusable test fixtures (`fastapi_test_server`, `web_session`, `test_pat`, `test_pat_no_grants`, `zero_grants_workspace`, `NONEXISTENT_TABLE`) for clean-install verification.
- `tests/test_reader_smoke_matrix.py` — load-bearing parametrized test: every reader CLI command runs on a freshly-bootstrapped zero-grants workspace without a Python traceback.
- `tests/test_clean_install_integration.py` — end-to-end happy-path tests (minimal grants, zero grants, force preserves CLAUDE.local.md, readers in pre-init dir).
- `docs/RELEASE_CHECKLIST.md` — manual clean-install protocol mandated for any PR touching the bootstrap path.
- Audited and replaced stale `da` verbs left over from prior merges in admin UI text, audit-log messages, code comments, operator runbooks, analyst-facing skill docs, and test docstrings (welcome template renderer/API tests now assert exact emitted markers — `agnes init` for analyst flow, `agnes auth` for admin flow — with explicit absence checks on legacy verbs). Vendor-specific `/opt/data-analyst/` install paths in jira backfill/consistency scripts and operator docs replaced with `<install-dir>/` and an `AGNES_ENV_FILE` env-var override. Intentional stale-marker tuples (`_LEGACY_STRINGS` in `app/api/claude_md.py`, `_OUR_COMMAND_MARKERS` in `cli/lib/hooks.py`) and tests that seed legacy hook content (`tests/test_lib_hooks.py`, `tests/test_legacy_strings_scan.py`) are preserved by design.

## [0.33.0] — 2026-05-04

Closes #162. Headline fix: `query_mode='materialized'` BigQuery rows now
materialize correctly for views and materialized views, with per-table
concurrency control preventing parquet corruption on overlapping scheduler
ticks. Plus a source_query server-generation convenience, a
`materialize.lock_ttl_seconds` config knob, and a schema v24 migration that
converts existing DuckDB-flavor source_query values to BQ-native SQL.

### Fixed

- BigQuery materialize now works for views and materialized views. Pre-fix,
  `materialize_query` ran admin's `source_query` as `COPY (sql) TO parquet`
  through the DuckDB BigQuery extension session, which routed through the BQ
  Storage Read API for `bq."<ds>"."<tbl>"` references. Storage Read API
  rejects non-base entities (`Binder Error: Error while creating read session:
  ... non-table entities cannot be read with the storage API`). Fixed by
  always wrapping admin SQL into `bigquery_query('<billing-project>',
  '<inner-sql>')` so COPY uses the BQ jobs API uniformly for tables, views,
  and materialized views.
- `materialize_query` no longer corrupts its parquet under concurrent
  invocations for the same `table_id`. Pre-fix, two overlapping
  `_run_materialized_pass` calls (e.g. a long-running COPY + the next
  scheduler tick) both hit the unconditional `if tmp_path.exists():
  tmp_path.unlink()` at function entry and started parallel COPYs against the
  same path, interleaving bytes and producing a parquet file with no valid
  footer. Now each call acquires a per-table_id `threading.Lock` plus an
  advisory `fcntl.flock` on `<id>.parquet.lock`; the second caller raises
  `MaterializeInFlightError` and the scheduler treats it as
  `skipped, in_flight` — never as an error.
- Cost guardrail dry-run now engages for materialized rows. Pre-fix, the
  BigQuery Python client returned 400 (`Table-valued function not found:
  bigquery_query`) on the wrapped SQL and the dry-run silently fail-opened.
  The dry-run now operates on the inner BQ-native SQL (admin's `source_query`
  directly), which the client parses cleanly.

### Changed

- **BREAKING** `query_mode='materialized'` rows MUST register `source_query`
  as BigQuery-native SQL (backticks for dashed identifiers, native
  joins/CTEs). DuckDB-flavor (`bq."<ds>"."<tbl>"`) is no longer accepted on
  register/PUT. The schema v24 migration converts existing rows automatically;
  operators with custom-written `source_query` should review the migrated form
  on first deploy. The validator's prior backtick-rejection rule is now scoped
  to `query_mode IN ('remote', 'local')` only.
- `_run_materialized_pass` summary `skipped` field changes from `list[str]`
  to `list[dict]` with shape
  `{"table": str, "reason": Literal["due_check", "in_flight"]}`. Downstream
  consumers that asserted the old string form must update.

### Added

- `POST /api/admin/register-table` for `query_mode='materialized'` rows with
  `bucket`+`source_table` but no `source_query` now server-generates
  `` SELECT * FROM `<project>.<bucket>.<source_table>` `` from the configured
  BigQuery project. The same fallback fires on `PUT /api/admin/registry/{id}`
  when flipping to materialized. Operators only need to know
  `bigquery_query()` semantics for non-trivial queries.
- New top-level `materialize` config section in `instance.yaml`. Single field
  — `materialize.lock_ttl_seconds` (default `86400`, 24 h) — controls how
  long a stale `<id>.parquet.lock` file lives before a sibling materialize
  attempt reclaims it. Editable via `/admin/server-config` API and UI.

### Internal

- Schema v24 migration: rewrites `table_registry.source_query` for
  materialized BigQuery rows from DuckDB-flavor (`bq."<ds>"."<tbl>"`) to
  BQ-native (`` `<project>.<ds>.<tbl>` ``) using the configured BQ project.
  Idempotent on already-converted rows; logs a warning and skips when the
  project isn't configured (operator can configure + restart for retry).
  Wrapped in `BEGIN TRANSACTION` / `COMMIT` to match the project's
  transactional-finalizer pattern.
- `connectors/bigquery/extractor.py` exports `MaterializeInFlightError` and
  the `_get_table_lock` / `_get_lock_ttl_seconds` /
  `_wrap_admin_sql_for_jobs_api` / `_escape_sql_string_literal` helpers as
  test seams. Underscore-prefixed; not part of the public API.
- `tests/conftest.py` lifts `bq_instance` and `stub_bq_extractor` fixtures
  from `tests/test_api_admin_materialized.py` so subsequent test modules in
  this PR can resolve them via pytest's auto-discovery.
- `app/api/sync.py:is_table_due` hoisted to module-level import (was deferred
  inside `_run_materialized_pass`) so monkeypatching `app.api.sync.is_table_due`
  actually intercepts the call — the deferred form made test patches a no-op.

## [0.32.0] — 2026-05-04

Closes #160. Headline fix: `da query --remote` now resolves
`query_mode='remote'` BigQuery rows whose underlying entity is a `VIEW`
or `MATERIALIZED_VIEW`. Plus four reinforcing fixes that surfaced during
the work — server-side cost guardrail, registry-gating of direct `bq.*`
paths, function-call backdoor closed, structured CLI error rendering —
and one operator-side admin convenience (BQ test-connection endpoint +
billing_project placeholder UI). 14 issues caught + fixed across 6
iterations of Devin Review.

### Added
- **`/admin/server-config` BQ test connection**: admin-only `POST
  /api/admin/bigquery/test-connection` runs a 10s-timeout `SELECT 1`
  against BigQuery via the **process-cached** `BqAccess`
  (`@functools.cache` on `get_bq_access`) and returns typed structured
  feedback (`200 ok` / `400 not_configured` / `502 cross_project_forbidden`
  / `504 timeout`). Tests the config active in the running process —
  after a `data_source.bigquery` save the response shape includes
  `restart_required: True`; click "Test connection" AFTER restart to
  validate the freshly-saved values. The
  /admin/server-config UI gets a "Test BigQuery connection" button next
  to the data_source Save button; on failure the inline result uses the
  same structured shape as the CLI renderer so operators see the same
  hint format admins do.
- **`data_source.bigquery.bq_max_scan_bytes` server-config knob** (default 5 GiB):
  caps the BigQuery scan that `da query --remote` will issue against
  `query_mode='remote'` BQ rows. Exceeded queries are rejected with a
  structured `400 remote_scan_too_large` detail naming the bytes,
  tables, and a `da fetch` suggestion. Quota usage is recorded against
  the same daily byte cap as `/api/v2/scan`.
- **`data_source.bigquery.billing_project` placeholder UI**: the admin
  form now shows `(defaults to <project>)` greyed under an empty
  billing_project input, surfacing the access.py:339-340 fallback rule
  directly in the UI.

### Fixed
- **`da query --remote` against `query_mode='remote'` BigQuery rows
  whose underlying entity is a `VIEW` or `MATERIALIZED_VIEW`** now
  resolves correctly (issue #160). The BQ extractor creates a master
  view via the catalog path (`bq."<dataset>"."<source_table>"`) for
  `BASE TABLE` (Storage Read API; predicate pushdown) and via
  `bigquery_query()` for `VIEW`/`MATERIALIZED_VIEW` (jobs API). Other
  BQ entity types (`EXTERNAL`, `SNAPSHOT`, `CLONE`) are logged + skipped
  at extraction with no `_meta` row, so the orchestrator doesn't strand
  a registered name with a non-existent inner view.
- **Direct `bq."<dataset>"."<source_table>"` references in `/api/query`
  are now registry-gated**: unregistered paths return 403
  `bq_path_not_registered`; registered paths are subject to the same
  per-name grant check as registered names. Closes a pre-existing RBAC
  bypass where direct catalog-path syntax skipped the master-view
  forbidden-table check entirely. Quoted catalog tokens
  (`"bq"."ds"."tbl"`) are caught by the same regex.
- **`bigquery_query()` direct calls in user SQL are now blocked** by the
  `/api/query` keyword blocklist. Closes a pre-existing function-call
  bypass that ran arbitrary BQ jobs API calls against any reachable
  dataset, ignoring the registry. Wrap views internal to the BQ
  extractor still use `bigquery_query()` inside their `CREATE VIEW`
  body — those run via DuckDB's view resolution at query time, never
  via user-submitted SQL, so the blocklist doesn't break them.
- **CLI commands (`da query --remote`, `da query --register-bq`,
  `da fetch`, `da schema`, etc.) pretty-print structured BigQuery
  errors** — `cross_project_forbidden`, `bq_forbidden`, `auth_failed`,
  `not_configured`, `remote_scan_too_large`, `bq_path_not_registered`,
  etc. — instead of dumping the truncated JSON body. The hint that
  explains how to fix `USER_PROJECT_DENIED` (set
  `data_source.bigquery.billing_project` in /admin/server-config) is
  now actually visible to the operator.
- **`/api/query/hybrid` now returns dict `detail`** for typed errors
  (was flattening to `f"BQ '{alias}': {error_type}: {message}"`), so
  the new CLI renderer surfaces the structured shape consistently
  across both endpoints.

### Changed
- **BREAKING (config-only): `data_source.bigquery.legacy_wrap_views`
  removed**. The flag was opt-in for the wrap-view behavior that is now
  the default. Keys still present in operator overlays are silently
  ignored — no action required. Operators who previously set
  `legacy_wrap_views: false` (the prior default) get the new behavior
  for VIEW / MATERIALIZED_VIEW rows: a master view is created (via the
  BQ jobs API), and `da query --remote` works against the registered
  name. The cost concern that motivated the prior default is now
  addressed by the server-side guardrail (see Added).
- **Quota tracker relocated**: `_build_quota_tracker` and
  `_quota_singleton` moved from `app/api/v2_scan.py` to
  `app/api/v2_quota.py` (their natural home). `v2_scan.py` re-exports
  the function for backwards compat; existing test sites that call
  `v2_scan._build_quota_tracker()` keep working.

## [0.31.0] — 2026-05-04

### Added

- **Agent Workspace Prompt** — admin-editable Jinja2 markdown template for the analyst's `CLAUDE.md`, surfaced in their workspace by `da analyst setup`. Default = rich briefing with RBAC-filtered tables/metrics/marketplaces context. Edit at `/admin/workspace-prompt`. Endpoints: `GET /api/welcome` (analyst-facing, auth required), `GET/PUT/DELETE /api/admin/workspace-prompt-template`, `POST /api/admin/workspace-prompt-template/preview`. CLI: `da analyst setup` writes `CLAUDE.md` by default; new `--no-claude-md` flag opts out. See `docs/agent-workspace-prompt.md`.
- **Agent Setup Prompt** — customizable bash setup script shown on `/setup` and copied by the dashboard clipboard CTA. Default = the live `setup_instructions.resolve_lines()` output (TLS trust bootstrap, CLI install, login, marketplace, skills). Admin override at `/admin/agent-prompt` — full replacement of the default, not a banner added on top. Override flows to both the `/setup` page display and the dashboard clipboard payload. Jinja2 is available for `{{ instance.name }}` etc.; `{server_url}` and `{token}` are JS-substituted at clipboard-copy time and survive Jinja2 rendering unchanged. REST API: `GET /api/admin/welcome-template` returns `{content, default, updated_at, updated_by}` (`content` is `null` when no override is set; `default` is always the live computed script); `PUT` to set an override; `DELETE` to clear; `POST /api/admin/welcome-template/preview` for live preview without persisting. Available Jinja2 placeholders: `instance.{name,subtitle}`, `server.{url,hostname}`, `user` (may be `null` for anonymous visitors), `now`, `today`. Override content is HTML-sanitized post-render (script/iframe/event-handler strip). See `docs/agent-setup-prompt.md`.
- DuckDB schema v21: `welcome_template` singleton table backing the Agent Setup Prompt override. Auto-migration v20→v21 on first start.
- DuckDB schema v22: `setup_banner` table reserved (no consumers; retained for forward compatibility with already-migrated instances).
- DuckDB schema v23: `claude_md_template` singleton table backing the Agent Workspace Prompt override. Auto-migration v22→v23.

### Changed

- `da analyst setup` writes `CLAUDE.md` to the analyst workspace from the server-rendered template (fetched via `GET /api/welcome`). Use `--no-claude-md` to opt out. Analysts who ran setup while CLAUDE.md generation was temporarily absent will have their file written on the next `da analyst setup` run.
- `/install` page renamed to `/setup` ("Setup local agent" nav label) with 302 redirect from `/install`.
- Dashboard "What Claude Code will receive" inline preview replaced with a link to `/setup` for the canonical view.

### Fixed

- `da analyst setup` summary now accurately reflects whether `CLAUDE.md` was written, skipped (`--no-claude-md`), or skipped due to a server error — previously it always claimed "written from server template" even when the fetch failed (404, 401/403, network), contradicting its own stderr warning.

## [0.30.1] — 2026-05-02

### Security
- **auth**: per-IP rate limiting now applied across every credential-bearing
  auth endpoint. Defaults:
    - **10/minute** — `POST /auth/token`, `POST /auth/password/login`,
      `POST /auth/password/login/web` (login brute-force throttle).
    - **10/minute** — `POST/GET /auth/email/verify`,
      `POST /auth/password/reset/confirm`, `POST /auth/password/setup/confirm`,
      `POST /auth/password/setup` (JSON variant — without it, the form
      `/setup/confirm` throttle is bypassable by switching to the JSON
      path) (token brute-force throttle: the 32-byte URL-safe tokens are
      high entropy but partial leaks via logs / proxy referer have
      surfaced before, and there's no reason to allow unbounded guessing).
    - **5/minute** — `POST /auth/email/send-link`,
      `POST /auth/password/reset`, `POST /auth/password/setup/request`
      (email-bombing throttle: same shape on all three — attacker rotates
      random recipient addresses from a single IP to burn SMTP/SendGrid
      quota and spam real users; anti-enumeration responses mask which
      addresses landed).
    - **3/minute** — `POST /auth/bootstrap` (one-shot in normal use).
  Returns `429` with `Retry-After: 60` once exceeded. Per-IP key uses the
  leftmost `X-Forwarded-For` hop — same trust model as
  `app.auth.dependencies._client_ip` (Caddy strips client-supplied XFF in
  front of the app). Set `AGNES_AUTH_RATELIMIT_ENABLED=0` in env and
  bounce the container to disable (no image rebuild required; the value
  is read at process start, matching every other Agnes env knob). New
  dependency: `slowapi>=0.1.9`. Closes #45.
- **admin API**: `DELETE /api/admin/users/{id}/memberships/{group_id}` and
  `DELETE /api/admin/groups/{group_id}/members/{user_id}` now refuse to
  remove **anyone** from the seeded `Admin` group when they are the only
  remaining active admin — previously the guard only fired on self-removal,
  leaving a path where an admin could demote the only other admin and then
  rely on the partial guard to (correctly) block self-removal, but a
  scheduler / bootstrap path that bypasses normal admin checks could still
  reduce active admins to zero. Recovery from zero admins requires direct
  DB access, so the guard generalizes to mirror the existing
  `count_admins(active_only=True) <= 1` check on `DELETE /api/admin/users/{id}`
  and `PATCH /api/admin/users/{id}` (active=false). Closes #151.

### Fixed
- **admin API**: `POST /api/admin/register-table` and `PUT /api/admin/registry/{id}`
  now reject `source_query` containing BigQuery-native backtick identifiers
  (e.g. `` `prj.ds.t` ``) with HTTP 422 and a message pointing operators at
  the DuckDB-flavor equivalent (`bq."dataset"."table"`). Backtick SQL would
  silently no-op at the next materialize tick — the BQ extension's COPY runs
  through DuckDB's parser, which doesn't recognize backticks, so the query
  either parse-errored or matched zero rows and no parquet ever landed at
  `/data/extracts/<source>/data/<id>.parquet`. Fix catches the bad SQL at
  registration time so the row never lands in the registry.
- **admin API**: `DELETE /api/admin/registry/{id}` now removes the canonical
  materialized parquet (`${DATA_DIR}/extracts/<source_type>/data/<name>.parquet`
  plus any stale `.parquet.tmp`) AND clears the matching `sync_state` /
  `sync_history` rows. Pre-fix the registry row was dropped but the parquet
  + sync_state row stayed, so `GET /api/sync/manifest` kept advertising the
  dropped table to `da sync` and analysts kept downloading it. Defensive
  failure handling — file-removal errors are logged but don't fail the
  DELETE.

### Added
- **admin API**: `GET /api/admin/registry` enriches each table row with
  `last_sync_error` (string or null) sourced from `sync_state.error`. The
  scheduler's `_run_materialized_pass` now writes per-row failures via
  `SyncStateRepository.set_error` so cap-exceeded / auth-failure / bad-SQL
  errors surface to the admin UI and `da admin status` instead of vanishing
  into scheduler stderr. A row that recovers on the next tick clears the
  error automatically (the success path of `update_sync` resets
  `status='ok'` / `error=NULL` on the upsert).
- **admin API**: `POST /api/admin/register-table` now refuses requests whose
  `source_type` isn't actually configured on the instance — pre-fix, an
  admin could register `source_type='keboola'` on a BQ-only instance and
  the row would land in the registry but never sync (no Keboola URL/token
  to ATTACH against). Returns 422 with a message naming the configured
  primary source and pointing at `/admin/server-config` for enabling a
  secondary source. `jira` / `local` are exempt — they don't sit under
  `data_source.*`. Omitted source_type still tolerated for legacy CLI
  callers. Stays permissive when primary is `'local'` (bootstrap workflow
  — instance not yet pointed at a real source).
- **query API**: `POST /api/query` now returns a materialize-aware error
  when the failed SQL references a table id that's registered with
  `query_mode='materialized'` but doesn't yet exist as a master view in
  this instance's `analytics.duckdb` (e.g. fresh instance, no scheduler
  tick yet). The hint names the table, points at `da sync` /
  `POST /api/sync/trigger`, and — when the registry row carries a
  bucket+source_table — surfaces the equivalent direct-source query
  (`bq."dataset"."table"` or `kbc."bucket"."table"`) so the operator has a
  concrete next step. Falls back to DuckDB's raw error for non-materialized
  unknowns.

### Internal
- **tests**: refresh `docker-e2e` health asserts to match the current
  `/api/health` shape (auth-free, returns `status` + `db_schema` only).
  `version` moved to `/api/version` in 0.10-era refactor; richer
  `services.duckdb_state` lives in `/api/health/detailed` (auth-gated).
  Tests had drifted and broke nightly e2e on main.

## [0.30.0] — 2026-05-01

### Added
- **admin UI**: each row in `/admin/tables` listings now has a per-row
  **Manage access** icon button (between Edit and Delete) that deep-links
  to `/admin/access#table:<table_id>`. The grant editor reads the hash on
  load and pre-fills the resource filter so the operator lands on the
  picked table once they select a group — shortcut for the common
  "I just registered table X, who should see it?" workflow without
  manual navigation through the resource tree.
- **docs**: `config/instance.yaml.example` documents every field newly
  exposed by `/admin/server-config` — `data_source.bigquery.billing_project`
  (with the USER_PROJECT_DENIED hint), `data_source.bigquery.legacy_wrap_views`,
  `data_source.bigquery.max_bytes_per_materialize`, `ai.base_url`,
  `openmetadata.*`, `desktop.*`, and the full `corporate_memory.*` block.
  Each cross-references the admin UI so operators discover the editor exists.
- **diagnostics**: `/api/health/detailed` (and therefore `da diagnose`) now
  surfaces a `bq_config` service entry on BigQuery instances. Reports
  `status="warning"` when `data_source.bigquery.billing_project` resolves
  equal to `data_source.bigquery.project` — the configuration where a
  service account with `roles/bigquery.dataViewer` on the data project but
  no `serviceusage.services.use` 403s every BQ call with
  USER_PROJECT_DENIED. The warning includes a hint pointing at the
  `instance.yaml` field and the `/admin/server-config` UI.
- **admin UI**: `/admin/server-config` exposes the full **corporate_memory
  governance schema** in the editor — `distribution_mode`, `approval_mode`,
  `review_period_months`, `notify_on_new_items`, the `sources` /
  `extraction` / `confidence` / `contradiction_detection` /
  `entity_resolution` nested objects, plus the `domain_owners` /
  `domains` lists. The whole section is optional (omitted = legacy
  democratic-wiki mode); admins can opt in via the UI without hand-editing
  YAML. Schema mirrors `config/instance.yaml.example` lines 224-317.
  `confidence.modifiers` (map<string, map<string, float>>) currently
  renders as a JSON-textarea fallback with the schema explained inline —
  full structured editor is a TODO.
- **admin UI**: server-config renderer learned three new shapes —
  `kind="array"` with a scalar `item_kind` renders as a vertical stack
  of typed inputs with +/- row controls; `kind="map"` with scalar
  `value_kind` renders as key:value rows with +/- controls;
  `value_kind="array"` inside a map renders the value column as a
  comma-separated list (pragmatic compromise over a full nested-array
  UI inside each map row). Leaf inputs now carry `data-path` (JSON-encoded
  segment array) so map keys with embedded dots —
  e.g. `confidence.base["user_verification.correction"]` — survive
  round-trip without being mistaken for nested-path separators.
- **admin UI**: `/admin/server-config` renders registry-declared nested
  fields (`kind="object"` with explicit `fields`) as a fully-editable
  structured form — every leaf is its own input with a dotted-path
  `data-key`, and the collector rebuilds a nested patch on save. Replaces
  the previous read-only preview that forced operators to edit a parent
  JSON textarea. YAML-only keys outside the registry survive via an
  "Other (YAML-only) keys" expander per nested layer. Recursion handles
  arbitrary depth, ready for the upcoming corporate_memory + admins
  registry entries.
- **admin UI**: `/admin/server-config` now ships a known-fields registry
  (`_KNOWN_FIELDS` in `app/api/admin.py`, exposed on the GET response as
  `known_fields`). The renderer shows registry-declared knobs as dashed
  placeholders alongside populated values, with a one-line hint per
  field, so operators discover optional config (e.g.
  `data_source.bigquery.billing_project`) directly in the UI instead of
  having to read docs or hit a runtime error first. Subagents 2-4 will
  populate the bodies; the smoke fixture covers `bigquery.billing_project`.
- **admin UI**: `/admin/server-config` now exposes three previously
  YAML-only BigQuery knobs in the editor — `data_source.bigquery.billing_project`,
  `legacy_wrap_views`, and `max_bytes_per_materialize`. The GET response
  always includes them under `data_source.bigquery` (with documented
  defaults when YAML omits them) so the JSON-textarea UI shows them as
  editable keys. The section help text describes each. Operators no
  longer need to SSH to the VM, edit YAML, restart to flip these.
- **admin UI**: `/admin/tables` is now a per-connector tab interface
  (BigQuery / Keboola / Jira). Each tab has its own Register modal +
  listing scoped to its source_type. Active tab persists in
  `window.location.hash` so refresh keeps the operator in place.
- **Keboola materialized SQL**: `query_mode='materialized'` now works
  for `source_type='keboola'` — admin registers a SELECT against
  `kbc."bucket"."table"` and the scheduler writes the result to
  `/data/extracts/keboola/data/<id>.parquet`. Same flow as BigQuery
  materialized; same `da sync` distribution; same RBAC. Cost guardrail
  (BQ-style dry-run) intentionally omitted — Keboola extension has no
  dry-run analog and Storage API cost is download-byte-shaped, not
  scan-byte-shaped. A future PR can add a configurable byte cap if
  operators ask for it.
- **Keboola Sync Schedule**: per-table cron input added to the Keboola
  tab Register and Edit modals. The scheduler has always honored
  per-table `sync_schedule` for every source via `is_table_due()`,
  but the Keboola UI had no surface for it — operators had to use the
  `/api/admin/registry/{id}` PUT endpoint or `da admin` CLI. Now they
  can type `every 6h` / `daily 03:00` directly.
- **BigQuery `query_mode='materialized'`** — admin registers a SQL query
  via `da admin register-table --query-mode materialized --query @file.sql
  --sync-schedule "every 6h"`; the sync trigger pass runs it through the
  DuckDB BigQuery extension via the `BqAccess` facade on each tick that's
  due (per-table `sync_schedule` honored via `is_table_due()`) and writes
  the result to `/data/extracts/bigquery/data/<name>.parquet`. The
  manifest endpoint exposes the row to `da sync`, which distributes the
  parquet to analysts; analysts query it through their **local** DuckDB
  view. The server-side orchestrator does **not** create a master view
  for materialized tables — they are intentionally local-only for
  analyst distribution, mirroring the v2 fetch primitives' "queryable
  via `da fetch` not via remote" contract. Per-user RBAC filtering is
  unchanged: a materialized table is just another row in
  `table_registry` with `resource_grants` controlling which groups see it.
- **Schema v20** adds `source_query TEXT` column to `table_registry` to
  back the materialized mode. NULL for existing rows. The
  `materialize_query()` function in the BigQuery extractor performs the
  COPY atomically (`<id>.parquet.tmp` → `os.replace`) so a failed query
  never leaves a half-written parquet.
- BigQuery cost guardrail for `query_mode='materialized'` tables: before
  each COPY the scheduler runs a BQ dry-run (reusing
  `app.api.v2_scan._bq_dry_run_bytes` so cost-estimate logic lives in
  exactly one place) and raises `MaterializeBudgetError` (skips the row)
  when the estimate exceeds `data_source.bigquery.max_bytes_per_materialize`.
  Default 10 GiB; explicit `0` disables (YAML `null` falls through to
  the default — documented in `config/instance.yaml.example`).
  Fail-open when the dry-run itself errors (library missing, DuckDB
  three-part syntax the native BQ client can't parse, transient API
  failure) — logs a warning instead of blocking the COPY.
- Admin API: `POST /api/admin/register-table` and
  `PUT /api/admin/registry/{id}` accept `source_query` field. Validator
  enforces that `query_mode='materialized'` requires `source_query` and
  `query_mode in ('local', 'remote')` forbids it. PUT also rejects
  `source_query` set without `query_mode` in the same request body and
  clears the stale `source_query` when switching the merged record away
  from materialized mode.
- CLI: `da admin register-table --query <SQL>` accepts inline SQL or
  `@path/to.sql` shorthand for reading from disk. Reuses the existing
  `--sync-schedule` flag for the cron string.
- `da sync --quiet` flag suppresses Rich progress + multi-line summary,
  intended for use from Claude Code SessionStart/SessionEnd hooks and
  cron jobs. Errors still surface on stderr; the no-op case is silent.
  The terse summary line in `--quiet` mode (`sync: N tables, M errors`)
  lands on stderr so stdout stays clean for hook callers.
- `da analyst setup` now installs `SessionStart` (pull) and `SessionEnd`
  (upload) hooks into `<workspace>/.claude/settings.json`, idempotently,
  preserving any existing user-owned hooks. Workspace-level (not
  user-home) so the hooks fire only when Claude Code is opened in the
  analyst workspace, not in unrelated sessions on the same machine.
  Hooks assume `da` is on `PATH`. If the CLI is not installed system-wide
  (e.g. via `pipx` or `pip install -e .`), the hooks no-op silently —
  expected graceful degradation, never blocks a session.
- `docs/setup/claude_settings.json` ships the same two hooks so operators
  bootstrapping a fresh Claude Code workspace get auto-sync out of the box.

### Changed
- **admin UI**: Keboola Register and Edit modals adopt the same
  two-question radio model as BigQuery — *What to sync?* (Whole table
  / Custom SQL). Whole-table mode synthesizes a `SELECT *` and writes
  it through the materialized path; Custom mode lets the admin filter
  / aggregate / project. The legacy `query_mode='local'` extractor
  path remains supported for back-compat but is no longer the default
  for new Keboola registrations — Whole mode is functionally
  equivalent and follows the unified materialized pipeline.
- **admin UI**: `Sync Strategy` dropdown removed from the Keboola form
  (Register and Edit). Two independent agent reviews (2026-05-01) found
  the field's hint claimed it controlled extraction but no extractor
  reads it; only `profiler.is_partitioned()` consumes it for parquet-
  layout detection. Field stays in the DB and Pydantic model for
  back-compat (marked `Field(deprecated=True)`); just hidden from the
  primary form.
- **admin UI**: `Primary Key` input moved under `<details>Advanced` in
  both Keboola Register and Edit modals, with a clarifying hint that
  it's catalog metadata only — Agnes always does full-overwrite sync;
  no upsert / dedup. Auto-fill from Keboola discovery still works.
- **admin UI**: Registry listing column "Strategy" replaced with "Mode"
  (showing `query_mode` instead of decorative `sync_strategy`). The
  `.col-strategy` / `.strategy-badge` CSS rules removed.
- BigQuery `init_extract` no longer creates remote views for rows with
  `query_mode='materialized'`; those live as parquets and surface via
  the orchestrator's standard local-parquet discovery. Skipped rows do
  not appear in `_meta` so cross-source view-name collisions remain
  impossible.

### Deprecated
- `RegisterTableRequest.sync_strategy` — catalog/profiler metadata only;
  no extractor reads it. Marked `Field(deprecated=True)`. External API
  consumers see the signal in OpenAPI; back-compat preserved.
- `RegisterTableRequest.profile_after_sync` — runtime never read this
  flag (Agent 1 finding 2026-05-01); profiler runs unconditionally on
  every synced table. Marked `Field(deprecated=True)` and made inert
  (the BQ register endpoint no longer force-sets it to `False`).
  Back-compat preserved — external clients sending the field get no
  error, no warning, no effect.

### Fixed
- **admin API**: `update_table` PUT preserves `sync_strategy` and
  `primary_key` when the Edit modal omits them from the payload (this
  invariant always held via `request.model_dump()` + `if v is not None`,
  but Phase I now has an explicit regression-guard test).
- `docs/setup/claude_settings.json` no longer references the deleted
  `server/scripts/collect_session.py` — the dead `SessionEnd` hook had
  silently failed in every Claude Code session since the v1→v2 server
  purge. Replaced with `da sync --upload-only --quiet`.

### Internal
- README mode-first source table; new "Local sync & auto-update" section
  covering `da sync`, hooks, and admin RBAC for auto-sync membership.
- `CLAUDE.md` schema chain extended through v20 with the `source_query`
  description; four source modes documented in Connector Pattern (added
  Materialized SQL); new "Local sync & Claude Code hooks" subsection
  under Development.
- `cli/skills/connectors.md` — "BigQuery: pick a mode" decision table
  with cost / guardrail / registration example.
- `docs/architecture.md` — new "BigQuery — Materialized SQL" subsection
  describing the COPY pipeline, BqAccess integration, and cost guardrail.
- BQ cost guardrail dry-run is performed via the native
  `google-cloud-bigquery` client (through `BqAccess.client()`), which
  does not parse DuckDB three-part identifiers (`bq."ds"."t"`). Queries
  written in DuckDB syntax fall through fail-open and log a warning
  instead of engaging the cap. Operators who need the cap to be
  enforceable must register the materialized SQL using native BQ
  identifiers (`\`project.ds.t\``).
- Hardenings landed during devil's-advocate review of PR #145:
  - `materialize_query` computes the parquet MD5 inline (after COPY,
    before `os.replace`) instead of re-reading the file in
    `_run_materialized_pass` — saves a full sequential read on the
    request thread for multi-GB parquets.
  - 0-row materializations log a `WARNING` so an empty result set
    can't masquerade as "the SQL is fine, today there's nothing".
  - The ATTACH-tolerated `except duckdb.Error: pass` is narrowed to
    the "alias already attached" case; real errors (cross-project
    permission, malformed project_id) propagate so the per-row
    aggregator records them correctly instead of surfacing a
    confusing downstream "bq is not attached".

### Known limitations
Operators should be aware of these production-only behaviours; tests
cannot exercise them and they will be revisited in follow-up PRs:

- **GCE metadata token expiry mid-COPY (catastrophic for very long
  scans).** The DuckDB BQ extension caches the token in a session
  SECRET created at session-open. A `materialize_query` call that
  takes longer than the token's remaining lifetime (~1h) will see
  silent 401s downstream and may produce a truncated parquet. No
  current mitigation; if your materialized SQL scans more than ~30
  GiB on a single COPY, run it via the BQ console / Storage Read
  API offline and `da fetch` the result instead until token refresh
  is wired into the BQ extension's session.
- **DuckDB `bigquery` community extension is unpinned** —
  `INSTALL bigquery FROM community; LOAD bigquery;` picks up the
  latest published version on every cold start. A breaking change
  upstream surfaces as a production failure with no test signal.
- **Schema drift after a SQL edit silently breaks analyst queries.**
  Editing `source_query` to drop a column writes a new parquet with
  the new shape; analysts' queries that referenced the dropped
  column 500 on the next sync without warning. No diff or version
  field surfaces this. Workaround: announce changes in the team
  channel before editing materialized SQL.
- **`materialize_query` is not concurrency-locked.** Two concurrent
  `/api/sync/trigger` calls for the same materialized row race on
  `<id>.parquet.tmp`. `init_extract` has `_INIT_EXTRACT_LOCK` for
  the remote-attach path, but the materialized path does not yet.
  In practice: the cron scheduler is single-threaded and manual
  triggers are rare, so the race window is small.

## [0.29.0] — 2026-05-01

### Fixed
- **`scripts/ops/agnes-tls-rotate.sh` self-signed fallback cert now sets `basicConstraints=critical,CA:FALSE` on the leaf.** OpenSSL's default `[v3_ca]` config marks `CA:TRUE` on `req -x509`, which causes strict TLS stacks (rustls / `webpki`, used by `uv`, `cargo`, and future versions of `pip`) to reject the cert with `invalid peer certificate: CaUsedAsEndEntity` per RFC 5280 §4.2.1.9. Browsers, curl, and OpenSSL-based clients tolerated the violation, hiding the bug until a `uv` user hit it. Affects every VM running on the self-signed fallback while the corp PKI hasn't published the real chain yet — the fix lands on the next `agnes-tls-rotate.timer` tick (or `systemctl start agnes-tls-rotate.service` for an immediate refresh). Existing CSR / real-cert paths unaffected; only the bring-up fallback regenerates.

## [0.28.0] — 2026-05-01

### Fixed

- **Analyst CLAUDE.md template now documents BigQuery remote-query capability.** `config/claude_md_template.txt` (used by `da analyst setup`) had **zero mention** of `query_mode: "remote"`, `da fetch`, `da query --remote`, or `--register-bq` — the AI analyst running in a freshly-bootstrapped workspace had no idea remote tables existed. Added a `## Remote Queries (BigQuery)` section covering: discovery via `da catalog` (now called out as canonical, with `data/metadata/schema.json` flagged as local-only); the three query patterns (`da fetch` preferred, `da query --remote` for one-shots, `da query --register-bq` for hybrid joins); permission boundary (BQ access via the agnes server's GCE service account, not personal creds — escalate permission errors to admin); cost awareness (every query bills the SA's project for bytes scanned, `--select`/`--where`/`--estimate` discipline); `da fetch` estimate-first rules; BigQuery SQL flavor reminder; snapshot freshness ritual (`da snapshot drop` + re-fetch when source data updates); concrete hybrid-query example with `--register-bq` joining local + ad-hoc BQ; the unknown-table case (ad-hoc `--register-bq` or ask admin to register); and a cross-reference to `da skills show agnes-data-querying` for deeper guidance. Also clarifies that **personal customizations belong in `.claude/CLAUDE.local.md`**, not CLAUDE.md (which is regenerated by `da analyst setup --force` and would lose edits). Closes #153.

### Removed

- **Legacy `docs/setup/claude_md_template.txt` deleted.** 359-line stale template that documented the deprecated SSH-heredoc remote-query protocol (`ssh data-analyst 'bash ~/server/scripts/remote_query.sh --stdin' < query.json`). The active template lives at `config/claude_md_template.txt`; the docs/ copy was confusing references and at risk of being pulled into a workspace by a future refactor. No code references the deleted file (verified).

## [0.27.0] — 2026-04-30

### Removed

- **BREAKING** Table access fully migrated to per-group `resource_grants` (`ResourceType.TABLE`). Existing `dataset_permissions` rows are dropped on upgrade — admins must re-grant via `/admin/access`. Wildcard bucket grants (`bucket.*`) no longer supported and not replaced: every table needs an explicit grant (or admin override). Per-table bulk action in `/admin/access` covers a whole bucket at once.
- **BREAKING** `table_registry.is_public` column dropped. The bypass shortcut had no API/UI/CLI surface to set it (only direct DB UPDATE worked) so the legacy data-RBAC layer was de-facto inactive — every table was implicitly public. Post-upgrade non-admin users see **zero tables** until admin grants explicit access. Migrate by minting the relevant `resource_grants(group, "table", id)` rows in `/admin/access` before deploy or immediately after.
- **BREAKING** Self-service `access_requests` flow removed (table, repository, `/api/access-requests/*` endpoints, "Request Access" catalog modal). Users contact admin out-of-band; admin grants via `/admin/access`.
- **BREAKING** Legacy `users.role` column dropped (NULL artifact since v13). API contracts: `CreateUserRequest.role`, `UpdateUserRequest.role` removed; `UserResponse.role` becomes a derived `"admin"|"user"` label. CLI: `da admin set-role` removed (hard-fail with a replacement command), `--role` flag removed from `da admin add-user` and `da auth import-token`. JWT `role` claim removed from new tokens (existing tokens keep the claim, ignored on read).
- **BREAKING** `/api/admin/permissions` endpoints removed (POST/DELETE/GET). Replaced by `/api/admin/grants`. Half-shipped `/admin/permissions` admin UI page removed (template, route).
- `AGNES_ENABLE_TABLE_GRANTS` env-gate removed from `app/resource_types.py` — `ResourceType.TABLE` is now unconditionally enabled (the gate existed only because runtime enforcement still flowed through legacy `dataset_permissions`).
- `tests/test_permissions.py`, `tests/test_permissions_api.py`, `tests/test_access_requests_api.py` deleted (covered functionality removed).

### Added

- Schema **v19**: drops `dataset_permissions`, `access_requests` tables and `users.role`, `table_registry.is_public` columns. Implementation in `src/db.py:_v18_to_v19_finalize` uses the table-rebuild idiom (rename → create new → INSERT … SELECT → drop old) to work around DuckDB's `ALTER TABLE DROP COLUMN` limitations on tables that have ever held FK constraints. The INSERT picks the intersection of the legacy and v19 column sets so test fixtures with hand-crafted minimal pre-v19 schemas migrate cleanly.

## [0.26.0] — 2026-04-30

### Changed

- **BREAKING** **All host-side artifacts (compose files, `Caddyfile`, host bash scripts) now ship in the docker image, not curled from `main` at boot.** The Dockerfile bakes them at `/opt/agnes-host/` and the customer-instance startup template extracts the whole directory via `docker create` + `docker cp` from the same `image_tag` the operator already pinned. Removes 5 `curl`s against `raw.githubusercontent.com` from the customer template (`docker-compose.yml`, `docker-compose.prod.yml`, `docker-compose.host-mount.yml`, `docker-compose.tls.yml`, `Caddyfile`) plus the `agnes-auto-upgrade.sh` curl shipped in 0.25.0. The image also now ships `agnes-tls-rotate.sh` + `tls-fetch.sh` at `/opt/agnes-host/` so consumer-side deploy templates can adopt the same pattern. Replaces the curl-from-main pattern that decoupled host-side artifacts from the pinned image (split-brain — image at `stable-2026.04.516`, host artifacts floating on whatever `main` was when the VM last booted) and gave no rollback knob other than reverting upstream PRs globally. With everything baked in, host artifacts and app code are released together from one commit; `image_tag` controls all; rollback is one tag bump; egress simplifies to "private registry" only (no public-internet dependency on every boot). Drift prevention is preserved by construction — image and host artifacts CANNOT drift because they ship together. **Operator action**: `image_tag` MUST point to a tag from this release or later; older tags lack `/opt/agnes-host/` and the startup `docker cp` will fail-loud at first boot. Existing VMs are unaffected because the module sets `lifecycle { ignore_changes = [metadata_startup_script] }` — only newly-created VMs run the new script.
- `compose_ref` variable on the customer-instance terraform module is **deprecated** — no longer used (compose files come from `image_tag` now). Variable retained for one release cycle to avoid breaking existing `terraform plan`s; will be removed in a future major bump. Pin `image_tag` instead.

## [0.25.0] — 2026-04-30

### Fixed
- `scripts/ops/agnes-auto-upgrade.sh`: fail-fast guard before any `docker
  compose` action — when the VM has a config disk attached
  (`/dev/disk/by-id/google-config-disk` exists), `/data/state` MUST be backed
  by it. Three retry attempts with backoff, then exit non-zero. Prevents the
  silent regression where docker host-mount propagation unmounts the config
  disk and the app writes user state (DuckDB, marketplaces, session secret)
  onto `/data` (sdb) — wiped on the next container recreate. Re-applies
  `mount --make-rprivate /data /data/state` on every run to defend against
  propagation regressions.
- `infra/modules/customer-instance/startup-script.sh.tpl`: replaced the
  inline heredoc copy of the auto-upgrade script with a `curl` from
  `raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/scripts/ops/agnes-auto-upgrade.sh`
  — single source of truth eliminates drift (the inline copy had fallen
  behind on TLS overlay detection, array-form compose files, and the new
  config-disk guard). VMs re-fetch on every boot, so script-only fixes
  propagate without an infra recreate. Also: `docker-compose.tls.yml` is
  now fetched unconditionally (not only when `tls_mode=caddy`), because
  the canonical auto-upgrade script detects TLS at runtime via cert files
  on disk — certs can appear after boot via `agnes-tls-rotate.sh` or
  manual provisioning, and the cron job would otherwise fail every 5 min
  until the file was placed. Same reasoning extends to `Caddyfile`:
  fetched unconditionally now, plus `agnes-auto-upgrade.sh` skips the
  tls overlay when `Caddyfile` is missing/empty (defensive — without
  it the caddy service crash-loops while the overlay closes `:8000`,
  net effect "app unreachable").

## [0.24.0] — 2026-04-30

### Changed

- **Effective-access readout no longer short-circuits for admin users on `/admin/users/{id}` and `/profile`.** Both `GET /api/admin/users/{id}/effective-access` and `GET /api/me/effective-access` previously returned `is_admin=true, items=[]` when the target was in the Admin group, and the UI rendered a flat "Full access via Admin" gold pill — which hid the underlying grant graph. Now both endpoints always run the JOIN, return the explicit per-resource breakdown, and surface `is_admin` only as informational metadata on the response. The UI drops the special pill on both surfaces and renders the same per-resource table everyone else sees. Authorization at runtime still gives Admin god-mode regardless of this list (see `app.auth.access.is_user_admin`); this is purely an audit/debug surface for admins to see *which* Admin-group grants exist via *which* sibling groups.

- **`/profile` group memberships use the same color-coded chip vocabulary as the rest of the admin surface.** Each membership renders as a colored `.group-chip` (Admin yellow, Everyone gray, google_sync green, custom purple) with the same name-shortening rule (`grp_acme_legal@workspace.example.com` → `Legal`, full email on hover via `title`). The Status row in the Account card was removed — same admin signal already appears as the Admin chip in Group memberships, so the pill was redundant. Server-side: the `/profile` route now projects `origin` and `display_name` per membership (computed via the shared `_derive_origin` helper + the `AGNES_GOOGLE_GROUP_PREFIX` strip), so the Jinja template stays env-lookup-free.

- **`/admin/users/{id}` polish: header `Admin` pill removed, "Add to group" dropdown filters out google-managed groups, whole user-cell on the list page is one anchor.** Header pill was redundant — the Group memberships section already shows the Admin chip with the canonical yellow color. The dropdown now skips `is_google_managed` rows (both `created_by='system:google-sync'` and the env-mapped Admin/Everyone) so admins don't see options the API would 409 on anyway. On `/admin/users` the avatar + name + email block became a single `<a class="user-cell">` linking to `/admin/users/{id}` so the entire info area lights up on hover, not just one line; the dedicated `Detail` action button stays for explicit affordance.

- **`/admin/users/{id}` Group memberships table renders chips with the same color + name-shortening rules as the user list.** The Group cell is now a `<a class="group-chip">` colored by `is-admin` (yellow) / `is-everyone` (gray) / `is-google_sync` (green) / `is-custom` (purple) and links through to `/admin/groups/{group_id}`. Google-sync chip text shortens via `deriveDisplayName` (e.g. `grp_acme_legal@workspace.example.com` → `Legal`); raw email lives on the chip's `title` attribute. Powered by a new `origin` field on `UserMembershipResponse` (`GET /api/admin/users/{id}/memberships`), computed via the same `_derive_origin` helper the rest of the surface uses.

- **`/admin/users` membership chips are color-coded by group origin and shorten Workspace-email names to a friendly form, so a row tells the same story as `/admin/groups` at a glance.** Colors: Admin → yellow, Everyone → gray, other google-synced groups → green, admin-created custom groups → purple. Name match (`Admin` / `Everyone`) takes precedence over origin so an env-mapped Admin/Everyone row (whose API origin is `google_sync`) keeps its canonical color. The chip text for google_sync groups runs through the same `deriveDisplayName` helper used on `/admin/groups`: `grp_acme_legal@workspace.example.com` renders as `Legal` (prefix stripped via `AGNES_GOOGLE_GROUP_PREFIX`, capitalized), and the raw Workspace email goes into the chip's `title` attribute for hover reveal. Custom / Admin / Everyone chip text stays raw — `deriveDisplayName` would over-capitalize names like `data-team`. To support this, `GroupBrief` on `GET /api/users` now carries the same `origin` field as `/api/admin/groups`, computed via the shared `_derive_origin` helper. Replaces the v12-era 2-color layout (yellow Admin, gray for any other system row, blue for everything else, full email always shown) which gave no signal about whether a chip came from Workspace or a manual admin grant and overflowed the cell on long Workspace emails.

- **`/admin/access` sidebar + right-pane title now use the same group display rules as `/admin/groups`.** Each sidebar row renders a multi-color origin pill (`google_sync` / `system` / `custom`) instead of the legacy yellow inline `system` tag, and a monospace subtitle below the name showing the Workspace email when the row is wired to one (`mapped_email` for env-mapped Admin/Everyone, the raw `name` for user-created google-sync groups). The right-pane card head adopts the same treatment when a group is selected. To support this, `GET /api/admin/access-overview` now includes `origin`, `mapped_email`, `is_google_managed`, and `created_by` per group — single source of truth shared with the `GET /api/admin/groups` endpoint via the same helpers (`_derive_origin`, `_mapped_email`, `_is_google_managed`).

- **`GET /api/admin/groups` and `GET /api/admin/access-overview` rename the `origin` value `"admin"` → `"custom"`.** The label is named after the row's *origin* (admin-created via UI/CLI), not the creator's role, so the pill doesn't visually clash with the seeded `Admin` system group's name. CSS class `.origin-admin` → `.origin-custom`; same purple swatch. No external consumers (CLI never reads the field). Pydantic default and JS fallbacks updated in lock-step. The previous workaround — a frontend `originLabel()` helper that mapped `admin → Custom` at render time — is gone now that the API value already reads correctly.

- **`/admin/groups` switches the seeded Admin / Everyone rows to a `google_sync` chip and shows the Workspace email as a subtitle when env-mapped.** Previously the mapped Admin row showed `Admin` as the big title with `Admin` repeated as the subtitle (the `deriveDisplayName` strip-and-capitalize chain produced no useful output for a literal canonical name) and a yellow `system` chip — which buried the fact that membership is actually owned by Workspace. Now: when `AGNES_GROUP_ADMIN_EMAIL` / `AGNES_GROUP_EVERYONE_EMAIL` is configured, `GET /api/admin/groups` reports `origin='google_sync'` for the matching seeded row (the system badge is suppressed; Workspace is the authoritative source of membership) and the new `mapped_email` field carries the configured Workspace email. The list view shows the canonical name as the big title with the Workspace email as a monospace subtitle (`Admin / admins@workspace.test`) and a green `google_sync` chip. The `/admin/groups/{id}` detail header mirrors the same — name as `<h1>`, `mapped_email` as the `gd-title-email` subtitle. Unmapped Admin / Everyone rows stay `origin='system'` with no subtitle. Regular google_sync rows (whose `name` is already the Workspace email) keep the existing `deriveDisplayName` rewrite behavior with `mapped_email=null`.

- **SSO-managed accounts are read-only for password / delete operations, both in UI and at the API layer.** Detection is in `app.api.users._is_sso_user`: a user counts as SSO-managed if they belong to any group whose `created_by = 'system:google-sync'`, OR they belong to the seeded `Admin` system group while `AGNES_GROUP_ADMIN_EMAIL` is set, OR the seeded `Everyone` system group while `AGNES_GROUP_EVERYONE_EMAIL` is set. Users with no groups, or only admin-created custom groups, are unaffected. The flag surfaces as `is_sso_user: bool` on every `/api/users` and `/api/users/{id}` response. UI: the `/admin/users` row actions and the `/admin/users/{id}` Account section suppress the Reset / Set pwd / Delete buttons for those rows. Server: `POST /api/users/{id}/reset-password`, `POST /api/users/{id}/set-password`, and `DELETE /api/users/{id}` now return **409** with `detail: "User is managed by an external SSO provider; …"` for SSO targets — so a curl-savvy admin who bypasses the UI guard still cannot reset / set / wipe a Google Workspace account locally. Deactivate stays available so admins can gate access locally even when the upstream account is managed elsewhere. Name is provider-neutral so a future provider (Cloudflare Access, Okta, …) plugs into the same flag without churning the API.

### Fixed
- **`scripts/ops/agnes-tls-rotate.sh` now chowns `/data/state/certs/` to UID 999 (the `agnes` user inside the app image) on every run.** Previously the script only `mkdir -p`'d and `chmod 700`'d the directory, leaving ownership to whoever happened to create it first — root when systemd fired the timer before docker-compose-up, or UID 999 when the container's volume init touched it first. Race-dependent. When root won, the resulting `drwx------ root:root` directory was unreadable by the UID-999 container, `_read_agnes_ca_pem()` returned `None`, and the `/install` setup prompt silently dropped the cross-platform TLS trust block (Step 0 from #137) — operators on those VMs ended up with no client-side cert bootstrap and a broken `claude plugin marketplace add` against the self-signed host. The chown is unconditional + idempotent (`|| true` for hosts where the numeric GID can't be set), so re-running the timer self-heals existing VMs without manual `chown` on the operator's part. Files inside the directory keep their existing modes — `fullchain.pem` is `0644` (world-readable, so root- or 999-owned both work for the agnes container) and `privkey.pem` is `0600` (only Caddy reads it, and Caddy's container runs as root).
- **`_is_sso_user` no longer treats `system_seed` / `admin` memberships in env-mapped Admin/Everyone as SSO (Devin BUG_0002 on PR #142).** Without checking `user_group_members.source`, the v13 migration's blanket Everyone backfill (`source='system_seed'`) flipped every existing local user to `is_sso_user=True` the moment an operator set `AGNES_GROUP_EVERYONE_EMAIL` — locking the admin out of password reset / set / delete on accounts the IdP doesn't actually own (the admin couldn't even un-flag them via "remove from Everyone" because `_guard_google_managed` blocks manual removal once env-mapped). The system-group branches (Admin / Everyone) now additionally require `source='google_sync'`. The created_by branch (`system:google-sync` groups) is unchanged because those groups only exist because of Google sync — every membership in them is IdP-owned regardless of `source`. The v18 migration in this PR also retroactively cleans up the offending `system_seed` rows in env-mapped Admin/Everyone groups; the source-check fix is the runtime guard that keeps future writes safe.
- **`POST /api/admin/users/{id}/memberships` now returns the correct `origin` for the new membership (Devin review round 1 on PR #142).** The handler constructed `UserMembershipResponse` without setting `origin`, so the model default `"custom"` was returned regardless of the target group — while the matching GET endpoint computes `origin` via the shared `_derive_origin` helper. Adding a user to a system group (Admin / Everyone) over POST now reports `origin="system"` (or `"google_sync"` when env-mapped), matching GET. The UI re-fetches after add so visible impact was zero, but any non-UI API consumer got the wrong value.

- **Schema migration v18: drop stranded non-google memberships in google-managed groups (Devin review round 1 on PR #142, partial response).** v13's `_v12_to_v13_finalize` unconditionally backfilled every existing user into Everyone with `source='system_seed'` under the original "Everyone = all users" semantics. The platform design has since shifted: when `AGNES_GROUP_EVERYONE_EMAIL` / `AGNES_GROUP_ADMIN_EMAIL` is configured, those system rows mirror a Workspace group exclusively, and only Google sync should write into them. The leftover `system_seed` rows (a) misrepresent the membership model and (b) cause `_is_sso_user` to flag local users as SSO-managed, blocking password-reset / set / delete via `_reject_if_sso`. v18 deletes: (1) non-google memberships in auto-created `created_by='system:google-sync'` groups (unconditional — those groups only exist because Workspace materialized them), (2) `system_seed` rows in Everyone **only when `AGNES_GROUP_EVERYONE_EMAIL` is set**, (3) `system_seed` rows in Admin **only when `AGNES_GROUP_ADMIN_EMAIL` is set** AND `added_by NOT IN ('app.main:seed_admin', 'auth.bootstrap')` so the bootstrap admin always survives. Env-conditional branches mean a non-Google deployment keeps its local Admin / Everyone semantics intact (system_seed rows there are legitimate, not cruft). Runtime safeguards against future writes from the legacy `users.role` apparatus are tracked in #144.

### Removed

- **`GET /api/admin/group-suggestions` endpoint and the "Suggested from your Google account" picker on the `/admin/groups` create modal.** The picker fetched the calling admin's Workspace groups (via Cloud Identity), filtered out ones already registered as `user_groups` rows, and offered them as one-click name pre-fills. Replaced by the OAuth callback's automatic `google_sync` group materialization (every Workspace group the user belongs to that matches `AGNES_GOOGLE_GROUP_PREFIX` is auto-created on login) — the manual picker became redundant. Cloud Identity calls in the request path are gone with it.

## [0.23.0] — 2026-04-30

### Added
- **Single-item Edit button on every memory item card** in `/corporate-memory/admin`. Surfaces the per-item `PATCH /api/memory/admin/{id}` endpoint added in #126 — until now it was only reachable via the CLI (`da admin memory edit <id>`) or by selecting one item in the bulk batch bar. The modal pre-fills from the item's current title / content / category / domain (dropdown matching `VALID_DOMAINS` + `(unset)`) / audience / tags (comma-separated). Authorisation: same `require_admin` gate as the rest of the memory admin surface.
- **`ai` section editable in `/admin/server-config`**. The `ai:` block in `instance.yaml` (provider / api_key / model / base_url / structured_output for the corporate-memory extractor) was missing from `_EDITABLE_SECTIONS` and `SECTION_META`, so admins had no UI path to view or set the LLM token without editing `instance.yaml` directly. `api_key` is auto-masked via the existing `_SECRET_KEY_PATTERNS` (substring matches "api_key"), so the input renders as a password field and audit-log diffs redact the value.
- **`MEMORY_DOMAIN` RBAC resource type** for corporate-memory items. Admins use `/admin/access` to grant `user_groups` access to specific domains (one of `finance` / `engineering` / `product` / `data` / `operations` / `infrastructure`). Members of granted groups see all `knowledge_items` in that domain regardless of the existing `audience` string filter. The two filters compose with OR semantics, so the existing `audience='group:X'` convention keeps working unchanged for ad-hoc per-item targeting; pre-grant deployments behave identically (when no MEMORY_DOMAIN grants exist, the OR clause collapses to a no-op). Wired in `KnowledgeRepository.list_items` / `search` / `count_items` / `count_by_tag` / `count_by_audience` and in the inline SQL of `GET /api/memory/stats` via a new `granted_domains` parameter resolved from `resource_grants` by `_caller_granted_memory_domains`. **Note**: a MEMORY_DOMAIN grant is a parallel visibility path that pierces the `audience` field — an item with `audience='group:admins-only'` and `domain='finance'` becomes visible to anyone with a `MEMORY_DOMAIN/finance` grant. Operators who relied on `audience` as a hard access boundary should be aware (Devin ANALYSIS_0003 on PR #141).

### Fixed
- **Edit modal NULL→empty-string preservation** in `/corporate-memory/admin`. `submitEditItem` was sending `audience=""` for items whose stored audience was NULL, which silently broke visibility (the audience filter checks `audience IS NULL OR audience = 'all'`, neither of which matches empty string). Now empty form values for `audience`/`category`/`domain`/`content` are sent as JSON `null` so the backend stores NULL. (Devin BUG_0001 on PR #141 5f649a4 review.)

## [0.22.0] — 2026-04-30

### Fixed

- **`/api/v2/sample/{table_id}`, `/api/v2/schema/{table_id}`, `/api/v2/scan/estimate`, and `/api/v2/scan` now return structured 502/400 instead of bare 500 when BigQuery raises `Forbidden`/`BadRequest`.** Issue #134. Previously, `_fetch_bq_sample`, `_fetch_bq_schema`, `_bq_dry_run_bytes`, and `_run_bq_scan` had no `try/except`, so a cross-project SA without `serviceusage.services.use` on the data project surfaced as an empty HTTP 500 — operators got no diagnostic. All four call sites now translate `google.api_core.exceptions.Forbidden` to HTTP 502 with `error: "cross_project_forbidden"` (when the message mentions `serviceusage`) plus a `details.hint` pointing at `data_source.bigquery.billing_project` in `instance.yaml`, or `error: "bq_forbidden"` for non-serviceusage ACL denials. `BadRequest` translates to HTTP 400 (`bq_bad_request`) on `/scan/estimate` and `/scan` since their SQL is user-derived (built from `req.select`/`where`/`order_by`), and to HTTP 502 (`bq_upstream_error`) on `/sample` and `/schema` where SQL is server-constructed (server-built `SELECT * … LIMIT n` and `INFORMATION_SCHEMA.COLUMNS` queries respectively). The strict `_fetch_bq_schema` path is wrapped; the best-effort `_fetch_bq_table_options` path retains its existing `try/except → return {}` so `/schema` still returns 200 with empty partition info if BQ metadata is unreachable. `/api/v2/sample` additionally falls back to `data_source.bigquery.billing_project` (with `data_source.bigquery.project` as the default) — `/scan/estimate` and `/scan` already had this fallback.

### Changed

- **BREAKING for deployments using `BIGQUERY_PROJECT` env var alongside `data_source.bigquery.project` in `instance.yaml`.** Issue #134. The env var now sets BOTH billing and data project (used as both the FROM-clause project AND the billing/quota target), overriding `data_source.bigquery.project` for FROM-clause construction in `v2_scan` / `v2_sample` / `v2_schema`. Previously `BIGQUERY_PROJECT` only affected `RemoteQueryEngine` billing and was ignored by the v2 endpoints (which read `instance.yaml` directly). Migrate by clearing `BIGQUERY_PROJECT` and setting `data_source.bigquery.billing_project` + `data_source.bigquery.project` in `instance.yaml` instead — the env var remains as a legacy override only.

### Internal

- **New shared module `connectors/bigquery/access.py` — `BqAccess` facade.** Issue #134. Unifies BigQuery project resolution (`BIGQUERY_PROJECT` env → `instance.yaml billing_project` → `instance.yaml project`), `bigquery.Client` construction, DuckDB-extension session setup (`INSTALL/LOAD/SECRET` from `get_metadata_token()`), and Google-API error translation (`translate_bq_error()` mapping `Forbidden`/`BadRequest`/`GoogleAPICallError` to typed `BqAccessError` with `kind` → HTTP-status mapping). Replaces four near-identical inline blocks across `v2_scan`, `v2_sample`, `v2_schema`, and `RemoteQueryEngine`. FastAPI endpoints inject via `Depends(get_bq_access)` (process-cached; `instance_config.reset_cache()` invalidates this cache too, so admin server-config saves hot-reload BQ project IDs without container restart); `RemoteQueryEngine` injects via `bq_access=BqAccess(...)` constructor kwarg with lazy resolution (DuckDB-only paths never trigger BQ config lookup). When BigQuery isn't configured, `get_bq_access()` returns a sentinel `BqAccess` whose `client()` / `duckdb_session()` raise `BqAccessError(not_configured)` only when actually called — non-BQ instances (Keboola-only, CSV-only) get clean `Depends()` resolution and 200s on local-source v2 requests. Two known-duplicate sites (`connectors/bigquery/extractor.py`, `scripts/duckdb_manager.register_bq_table`) explicitly out of scope; tracked as follow-up.
- **Internal API change in `RemoteQueryEngine`**: `__init__` no longer accepts `_bq_client_factory` (test-only injection point, prefix `_`). Tests migrate to `RemoteQueryEngine(..., bq_access=BqAccess(projects, client_factory=...))`. The `BqAccessError` raised internally by `BqAccess` is translated to the existing `RemoteQueryError(error_type="bq_error")` shape in `_get_bq_client`, preserving the public contract — CLI (`cli/commands/query.py`) and `/api/query/hybrid` callers see no change. Removed the stale docstring at `src/remote_query.py` referencing `scripts.duckdb_manager._create_bq_client` as the default factory (it never was).
- **Side-effect behavior change for unusual cross-project setups in `/api/v2/sample`.** Issue #134. The FROM-clause project for `/sample` is now `data_source.bigquery.project` (the data project) rather than the conflated `billing_project` value — the Phase 1 fix passed `billing_project` (when set) as both the billing target AND the FROM-clause project. Deployments where `billing_project ≠ project` AND the queried table physically lives in `billing_project` (an unusual setup contradicting the documented config semantics) must move the table to the data project or unset `billing_project`. No effect on the standard cross-project setup (table in data project, jobs billed to billing project).
- `scripts/smoke-test.sh`: assertion 8 now hits `/api/admin/registry` (the current admin tables endpoint). The old `/api/admin/tables` URL was renamed long ago and the smoke test was returning 404 on every run — it only surfaced as a deploy failure when the full release pipeline first triggered the rollback path on the post-#137 deploy (run 25151878647). Same stale URL was also fixed in `CLAUDE.md`, `README.md`, and `dev_docs/server.md` — the routes now correctly point at `POST /api/admin/register-table` (create) and `PUT /api/admin/registry/{id}` (update).
- `.github/workflows/release.yml` smoke-test job: added `Log in to GHCR` step. The auto-rollback's `docker push :stable` was hitting `unauthenticated: User cannot be authenticated with the token provided` because the smoke-test job had no GHCR login of its own. Result: a failed deploy left `:stable` pointing at the broken image. The rollback step also got an explicit `GH_TOKEN` env, and the workflow's top-level `permissions` block gained `issues: write`, so its `gh issue create` call actually creates the alert issue (was silently swallowed by the `|| echo` fallback because of both the missing env var AND the missing scope).
## [0.21.0] — 2026-04-30

### Internal

- `scripts/dev/agnes-client-reset.sh` — destructive cleanup of an Agnes *client* install on a developer workstation, mirror image of `app/web/setup_instructions.py` so an onboarding-from-scratch test is reproducible. Removes the `da` CLI (`uv tool uninstall`), `~/.config/da` / `~/.agnes` / `~/.claude/skills/agnes`, the Claude Code `agnes` marketplace + its plugins, the Agnes CA from the OS trust store (Windows `certutil -delstore`, macOS `security delete-certificate -Z`, Linux `update-ca-certificates`/`update-ca-trust`), the `AGNES_CA_PEM_TRUST` block from the user's shell rc (with `.agnes-reset.bak` backup), and `/tmp/agnes*.whl` matches. Cross-platform (Git Bash on Windows / macOS / Linux); `--yes` skips the confirm prompt, `--dry-run` prints actions without executing.

### Changed

- **Trust block heredoc trimmed to 8 lines so reset script's `skip = 8` matches exactly (Devin review round 3 on PR #137).** The `_tls_trust_block` heredoc was emitting 9 lines into the user's shell rc (a leading empty line + the `AGNES_CA_PEM_TRUST` marker + 7 export/comment lines), but `scripts/dev/agnes-client-reset.sh` awk strips exactly 8 lines starting at the marker — leaving the leading empty line behind. On repeated install/reset cycles, those stray empty lines accumulated in `~/.zshrc` / `~/.bashrc`. Removed the leading empty string from the heredoc body in `_tls_trust_block` so the heredoc now writes exactly 8 lines, matching the awk count. Added two regression tests that pin the invariant — one asserts the heredoc body length, the other parses `skip = N` out of the reset script via regex and cross-checks it against the heredoc body line count, so future drift on either side fails loudly.

- **Marketplace block re-detects `$PLATFORM` so Linux actually gets the direct-HTTPS attempt (Devin review round 2 on PR #137).** `$PLATFORM` is set in step 0(a) but the prompt itself warns that env vars don't persist across separate Bash invocations (step 0(e) IMPORTANT note). The marketplace step's `case "$PLATFORM" in` ran in a later Bash call where `$PLATFORM=""`, falling through to the `*)` catch-all which hard-codes `MARKETPLACE_VIA=clone` — defeating the Linux-only direct-HTTPS attempt that node-based `claude` would have honored via `NODE_EXTRA_CA_CERTS`. Marketplace block now re-detects `$PLATFORM` via the same `uname -s` switch from step 0(a) before its case statement, making the block self-contained. Same fix not applied to step 0(c)'s `$PLATFORM` use because step 0 is meant to run as a single Bash block (a→b→c→d→e in sequence) where the variable is still in scope.

- **Setup prompt no longer references steps that may not have been emitted (Devin review on PR #137).** Three places hard-coded references to optional steps regardless of whether those steps were actually rendered. (1) Confirm step's summary bullets unconditionally listed "Which CA bundle source got picked in step 0(d)" and "Whether the marketplace add went via direct HTTPS or via the git-clone fallback" — both phantom in the default no-CA, no-plugins flow, and an LLM following the prompt would either ask the user about non-existent steps or hallucinate. `_FINALE_LINES_TEMPLATE` constant replaced with `_finale_lines(has_ca, has_marketplace)` that conditionally appends each bullet. (2) Preamble's "The fallback chain inside step 0(d) is documented and OK to use" pointed at a non-existent step when `ca_pem` was unset. `_preamble_lines(has_ca)` now drops that line in the no-trust-block path; the "don't disable TLS verification" guidance stays unconditional (valid generic advice). (3) Trust block step 0(c) said "without this, step 7's marketplace add fails" — stale after the layout reordering moved marketplace to step 5 (and made it optional). Reworded to describe the consequence without naming a step number.

- **Marketplace step now uses the git-clone fallback on macOS too — not only Windows — and strips the PAT from the cloned repo's `.git/config` after clone.** First fix: `claude` on macOS arm64 ships as a Mach-O binary with a `__BUN` segment (single-file `bun build --compile`); reverse engineering its `strings` table shows it recognizes `NODE_EXTRA_CA_CERTS` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` (including a "NODE_EXTRA_CA_CERTS detected" log line) but in practice none of them — nor the macOS login keychain — is honored for the marketplace HTTPS request, leaving the direct-add path failing with `unable to verify the first certificate` even after step 0(c) registered the cert. So the marketplace `case` now matches `linux)` for the direct-then-fallback path and `*)` (= Windows + macOS, both Bun-compiled) for straight-to-clone. Second fix: `git clone https://x:<PAT>@host/...` writes the URL verbatim into `~/.agnes/marketplace/.git/config`, so the PAT then sits in plaintext at a path that gets read by cloud-sync agents (iCloud, OneDrive) and antivirus scanners on default home setups; after clone we now run `git remote set-url origin "https://<host>/marketplace.git/"` to drop the token, plus a best-effort `chmod 700`/`chmod 600` (wrapped in `|| true` so it's a no-op on Windows NTFS via MSYS / Git Bash). Marketplace registration uses the local FS path, not the remote URL, so removing the token doesn't break anything — refreshes go via re-running setup with a fresh PAT from the dashboard. Third fix: each shell-out (`git clone`, `claude plugin marketplace add`, `claude plugin install <name>@agnes`) is now wrapped in `|| { echo "ERROR..." >&2; exit 1; }` so a failure halts the prompt loudly instead of falling through to a confusing downstream error (e.g. failed clone → `marketplace 'agnes' not found` from the next `plugin install`). Fourth fix: the diagnose step now calls out that `db_schema: unknown` is also normal for non-admin roles (e.g. `analyst`) on populated instances, not just on fresh installs — analyst lacks grants on the system schema, so the field stays `unknown` forever and was previously misread as a yellow check.

- **Setup prompt step ordering reshuffled so all installation work runs before the human-loop skills question.** Old order interleaved the human question (skills, step 5) between install (step 1) and marketplace/plugins (step 7), which led the assistant to either block on the user mid-install or "do the rest in parallel" while waiting. New order: install → login → verify → git check → marketplace + plugins → diagnose → **skills (last interactive step before Confirm)** → Confirm. With marketplace plugins to install, that's steps 1-2-3-4-5-6-7-8; without plugins, 4-5 (git/marketplace) collapse out and diagnose/skills/confirm renumber to 4-5-6. The skills step now explicitly tells the assistant to *wait* for the user's answer before moving to Confirm — the old "you can continue in parallel" hint is gone because there's no longer anything to do in parallel. `da diagnose` running late doubles as a final smoke test after plugins are in place.

- **Setup prompt's TLS trust block rewritten to be cross-platform and to dodge three TLS pitfalls observed across real workstation setups.** The previous block exported `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO` all pointing at the single Agnes CA; this caused (1) every Python tool in the same shell to lose its system trust store (PyPI immediately broke with `UnknownIssuer` because `SSL_CERT_FILE` is a *replace*, not an append), (2) `uv tool install <https-url>` against the Agnes wheel endpoint to fail with rustls' `CaUsedAsEndEntity` because the Agnes leaf cert is its own CA — `--native-tls` doesn't help (the rejection happens during chain validation, not trust lookup), and (3) `claude plugin marketplace add` to fail on Windows because `claude.exe` ignores both the OS trust store and `NODE_EXTRA_CA_CERTS` for marketplace HTTPS. The new step 0 (a) detects platform via `uname` + `$SHELL` and picks the correct shell rc file (zsh→`.zshrc`, bash on macOS→`.bash_profile`, else→`.bashrc`), (b) writes the cert PEM via single-quoted heredoc, (c) registers the cert in the OS trust store (Windows `certutil -user -addstore Root`, macOS `security add-trusted-cert`, Linux `update-ca-certificates`/`update-ca-trust`) — no admin rights needed, idempotent on re-run — so native binaries that bypass our env vars still trust the host, (d) builds a *combined* CA bundle at `~/.agnes/ca-bundle.pem` (system roots + Agnes CA) using a fallback chain for the system roots source (system `python3 -c 'import certifi'` → distro/curl bundle paths → `uv run --with certifi` as last resort), (e) persists `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`GIT_SSL_CAINFO` pointing at the *combined* bundle while keeping `NODE_EXTRA_CA_CERTS` on just `ca.pem` (Node's append semantics). When the trust block is emitted, step 1 also switches to a curl-then-local-install pattern (`curl --cacert` to download the wheel, `uv tool install --native-tls --force <local-file>` to install) so rustls never sees the Agnes host. Step 7 (marketplace) goes platform-aware: Windows skips the direct HTTPS attempt and uses a system `git clone` fallback (system git honors `GIT_SSL_CAINFO`), macOS/Linux try direct first. Step 4 (diagnose) calls out that `db_schema: unknown` and `data: 0 tables` are normal on fresh installs. Step 5 (skills) makes clear the assistant can continue with steps 6-7 while waiting for the user's skills answer. Step 12 (marketplace) calls out the harmless `git: 'credential-manager-core' is not a git command` warning so the operator doesn't chase it. The legacy `git config sslVerify=false` downgrade path stays as a fallback for instances without a `fullchain.pem` on disk (so existing `AGNES_DEBUG_AUTH` setups keep working).

### Added

- **"Set up a new Claude Code" prompt now bootstraps the marketplace and plugins.** The clipboard payload generated by the dashboard CTA appends a git pre-flight check (`git --version`, with `brew install git` / `winget install --id Git.Git` install commands for macOS / Windows) followed by a marketplace-registration step that runs `claude plugin marketplace add "https://x:<PAT>@<host>/marketplace.git/"` and one `claude plugin install <plugin>@agnes --scope project` per RBAC-allowed plugin (resolved via `marketplace_filter.resolve_allowed_plugins`). When the user has no plugin grants, the original 6-step layout is preserved. When `AGNES_DEBUG_AUTH` is enabled on the server (dev/self-signed-cert instances), a host-scoped `git config --global http."<server>/".sslVerify false` line is also included so the marketplace clone works against the self-signed endpoint. Plugins load on the next `claude` start.
- **Setup prompt inlines the server's TLS cert as a step-0 trust block on instances with a private CA / self-signed chain.** `app.web.router._read_agnes_ca_pem` reads `/data/state/certs/fullchain.pem` (path overridable via `AGNES_TLS_FULLCHAIN_PATH`; the file is bind-mounted into the app container by `docker-compose.host-mount.yml` from the same location `agnes-tls-rotate.sh` writes). Self-signed leaves and CA-signed leaves whose issuer isn't in the server-side `certifi` trust store are inlined into the prompt; publicly-trusted chains (Let's Encrypt etc.) are skipped so users don't unnecessarily narrow their default Python TLS trust. The inlined block writes the PEM to `~/.agnes/ca.pem` via single-quoted heredoc (so `$`/backtick chars in the cert never shell-expand) and exports `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO` for the current shell + persists them to `~/.bashrc`/`~/.zshrc` (idempotent via a marker grep guard) so `da` keeps trusting the host across new terminal sessions. When the trust block is emitted, the legacy `git config sslVerify=false` downgrade is suppressed — full TLS validation re-enabled, just against the inlined cert. Cross-platform (macOS bash/zsh + Windows Git Bash) — same env vars, same heredoc syntax. Replaces the `git config sslVerify=false`-only path that broke `claude plugin marketplace add` (Node has its own HTTPS client and ignores `git config`) and `uv tool install` (rustls, no insecure flag) on self-signed instances.

## [0.20.0] — 2026-04-29

### Added
- Dev debug toolbar gated by `DEBUG=1`. Mounts `fastapi-debug-toolbar` with panels
  for headers, routes, settings, versions, timer, logging, and a custom DuckDB
  panel that captures every `con.execute()` from `src/db.py` (tagged by
  `system` / `analytics` / `analytics_ro`). See `docs/development.md`.
- `X-Request-ID` request header / response header on every FastAPI response, plus
  a `request_id` field in JSON logs for cross-process correlation.
- Request-ID surfaced end-to-end on error responses: `Reference: <rid>` block on
  the rendered `error.html` page (with `user-select: all` for one-click copy)
  and a `"request_id": "<rid>"` field in the JSON 5xx body. The same id appears
  in the `x-request-id` response header, so a support ticket can be traced from
  a single value the user sees on the page.
- Dev log lines now carry the request id via `_RequestIdFilter` — `RichHandler`
  format is `[<rid>] [<logger>] <msg>` (or `[-]` outside of a request scope).
  JSON formatter already included `request_id`; this closes the gap for
  `DEBUG=1` development.
- Centralized `app.logging_config.setup_logging()` — replaces 23 scattered
  `logging.basicConfig(...)` calls. Uses `rich.logging.RichHandler` in dev
  (`DEBUG=1`) and JSON to stderr in prod.

### Changed
- All service entrypoints (`services/scheduler/__main__.py`, `ws_gateway`,
  `telegram_bot`, `corporate_memory`, `session_collector`, `verification_detector`)
  and CLI scripts under `scripts/` and `connectors/jira/scripts/` now call
  `setup_logging(__name__)` instead of inline `basicConfig`. Library modules no
  longer configure root logger at import time.
- **BREAKING** Telegram bot no longer writes to `/data/notifications/bot.log`.
  All bot logs go to stdout, captured by Docker. Use
  `docker compose logs -f notify-bot` to read them. Operators tail-ing the file
  must update their runbooks; see `dev_docs/telegram_bot.md` for the new
  procedure (including `journalctl` fallback for non-Docker hosts).
- Toolbar middleware is mounted INSIDE the GZip middleware (innermost on
  response) so the toolbar can decode HTML before compression. RequestIdMiddleware
  remains outermost; production behavior (DEBUG unset) is byte-identical to
  before.

### Fixed
- Removed rogue module-level `logging.basicConfig` from `app/api/sync.py` that
  was reconfiguring root logger every time the api module was imported.
- `RequestIdMiddleware` rewritten as a pure ASGI middleware (was
  `BaseHTTPMiddleware`). Removes the early `request_id_var.reset` in `finally`
  that fired BEFORE BackgroundTasks ran, causing them to lose the id. Also
  side-steps the known `BaseHTTPMiddleware` ContextVar-cross-task issue.
- Incoming `X-Request-ID` headers are now sanitized (alnum + `-` / `_`,
  truncated to 64 chars; falls back to a fresh uuid if nothing legal remains).
  Closes a CRLF log-forging vector when log handlers don't escape newlines.
- `_wants_html` no longer treats `Accept: */*` (curl default) or empty Accept
  as "wants HTML". Operators who curl non-API paths get JSON `{"detail": "..."}`
  as before — only real browsers (with `Accept: text/html,...`) get HTML error
  pages. (Devin ANALYSIS_0003 on PR #136 review.)
- Subprocess extractor in `app/api/sync.py` re-installs `logging.basicConfig`
  so INFO-level extraction progress from `connectors.keboola.extractor.run()`
  reaches stderr again (was silently dropped by Python's `lastResort` handler
  after the import-time `basicConfig` cleanup). (Devin BUG_0002.)
- `.env.template` comment for `DEBUG=1` no longer claims to enable
  `FastAPI debug=True` — that flag is intentionally NOT toggled (Starlette's
  `ServerErrorMiddleware` would intercept unhandled exceptions before the
  custom error handler runs). (Devin BUG_0001.)
- **Security**: HTML error page (500) no longer leaks `str(exc)` in
  production. The JSON branch already guarded that string behind `debug_on`
  but the HTML branch did not — browser users could see raw exception
  messages containing DB paths, SQL fragments, internal hostnames, or
  credentials embedded in connection strings. The HTML branch now mirrors
  the JSON branch's `debug_on` check; production users see only
  `"Internal server error"` plus the request id. (Devin BUG on b1c6ee9 review.)

### Internal
- `pyproject.toml`: added `fastapi-debug-toolbar>=0.6.3` to dev optional deps.
- `services/telegram_bot/config.py`: removed unused `BOT_LOG_FILE` constant.
- `tests/conftest.py`: removed stale comment about bot.py FileHandler.

## [0.19.0] — 2026-04-29

### Added
- `table_registry.sync_schedule` is now honored at runtime. `POST /api/sync/trigger` (called by the scheduler sidecar every 15 min by default) drops local tables whose schedule says they are not due. Tables without a schedule continue to sync on every tick (opt-in feature). Manual `POST /api/sync/trigger {"tables":[...]}` bypasses the schedule filter — operator override always wins. (#79)
- `script_registry.schedule` is now honored at runtime via the new endpoint `POST /api/scripts/run-due` (admin-only). The scheduler sidecar fires this every 60 s by default. Each due script is claimed atomically (`last_status='running'`), executed in a BackgroundTask, and the outcome written to `last_run` / `last_status`. Scripts already in `running` state are skipped — no concurrent runs of the same script. (#78)
- Four new env vars on the scheduler sidecar: `SCHEDULER_DATA_REFRESH_INTERVAL`, `SCHEDULER_HEALTH_CHECK_INTERVAL`, `SCHEDULER_SCRIPT_RUN_INTERVAL`, `SCHEDULER_TICK_SECONDS`. All accept positive integers (seconds); tick must be ≤ smallest job interval. Documented in `docs/DEPLOYMENT.md` → Scheduler tuning. (#77)
- `RegisterTableRequest.sync_schedule`, `UpdateTableRequest.sync_schedule`, and `DeployScriptRequest.schedule` now reject malformed strings with a Pydantic 422 (e.g. `"hourly"`, `"daily 25:00"`). The accepted forms are unchanged: `every Nm`, `every Nh`, `daily HH:MM[,HH:MM,...]`. **Note**: cron expressions (`"0 8 * * MON"` etc.) were never honoured by the runtime evaluator — they used to round-trip through the API as a silent no-op, and now they get a loud 422 at register/deploy time. Operators using cron strings must convert to one of the supported forms. (#78, #79)
- New `verify_ssl` knob in the `openmetadata:` section of `instance.yaml` (default `true`). Operators on internal CAs / self-signed catalogs must set it explicitly. (#89)

### Changed
- **BREAKING** `POST /api/scripts/deploy` now validates the source against the safety blocklist BEFORE persisting (previously safety checks ran only at execution time). Scripts containing blocked imports / patterns return 400 from `/deploy` instead of being stored and failing every scheduler tick. Closes the claim-fail-retry loop where the new `/api/scripts/run-due` endpoint would re-claim and re-fail an unrunnable deployed script every minute. (#78)
- **BREAKING** `OpenMetadataClient` now defaults to `verify=True` for TLS. The previous version hardcoded `verify=False` and suppressed urllib3's "Unverified HTTPS request" warning at import time (which leaked to every other httpx client in the process). Existing deployments on self-signed certificates without an explicit opt-out will start failing TLS verification — set `verify_ssl: false` in the `openmetadata:` block of `instance.yaml`, or supply a CA bundle path, before upgrading. Both production call sites (`connectors/openmetadata/enricher.py`, `src/catalog_export.py`) read the new `verify_ssl` config knob and pass it through. (#89)
- **BREAKING** `GET /marketplace/info` (admin-only debug endpoint) `name` field now returns the plugin's authoritative name from its `plugin.json` (e.g. `plug-x`) instead of the slug-prefixed form (`<slug>-<plug-x>`). The slug-prefixed form moved to a new `prefixed_name` field next to it; `original_name` is unchanged. Side-effect of the `/plugin` UI fix below — the synth marketplace.json's `name` field had to switch over for Claude Code's catalog lookup to work, and `/marketplace/info` mirrors that surface for consistency. Any downstream tooling that parsed the `name` field expecting the slug-prefixed format must now read `prefixed_name`. (#133)

### Fixed
- **`/plugin` UI in Claude Code rendered "Plugin <X> not found in marketplace" in the Components panel** for every plugin Agnes served, even though agents/skills/commands loaded correctly under the plugin's own namespace. Root cause: the synthetic `.claude-plugin/marketplace.json` listed each plugin under a slug-prefixed `name` (`<slug>-<plugin>`) while the plugin's authoritative `.claude-plugin/plugin.json` kept the original name. Claude Code resolves the loaded plugin back to its catalog entry by `plugin.json` name, so the lookup missed every entry. The synth manifest now reads the plugin's authoritative name from `<plugin_dir>/.claude-plugin/plugin.json` (falling back to the upstream marketplace.json's `name` when the plugin manifest is absent or unreadable). The directory layout under `plugins/<slug>-<plugin>/...` keeps the prefix so two upstream marketplaces that ship a same-named plugin still get distinct on-disk paths in the ZIP / git tree — their catalog entries will then collide under the same `name`, which is the correct surface (admin RBAC decides which upstream wins, same as if a user added both upstream marketplaces directly to Claude Code). `/marketplace/info` now exposes `prefixed_name` alongside `name` so operators can still disambiguate cross-marketplace shadowing. (#133)

### Internal
- `src/scheduler.py` now exports `is_valid_schedule(s)` and `filter_due_tables(configs, sync_state_repo)` for reuse across the sync filter, the script runner, and Pydantic validators.
- `ScriptRepository` gains `claim_for_run(script_id)` and `record_run_result(script_id, status)` — the atomic primitives for the scheduled-script execution path. `claim_for_run` uses `UPDATE … WHERE last_status IS DISTINCT FROM 'running' RETURNING id` for race-free claim.
- `services/scheduler/__main__.py` JOBS list refactored to a `build_jobs()` factory that reads + validates env at startup.

### Known limitations
- **Stuck `last_status='running'`**: a scheduled script whose BackgroundTask crashes mid-run (process killed, OOM, gateway timeout) stays claimed forever. Recovery: `UPDATE script_registry SET last_status = NULL WHERE id = ?` from a DuckDB shell. Auto-recovery via max-runtime detection is intentionally out of scope for v0.19.0; revisit if it bites in practice.
- **Schedule quantization rounds up**: `SCHEDULER_*_INTERVAL` accepts seconds but the underlying schedule grammar is minute-grained. Non-multiples of 60 round UP to the next minute (90 s → `every 2m`, never `every 1m`) so a job never fires more often than the operator configured. Sub-minute values clamp to `every 1m`. Documented in `docs/DEPLOYMENT.md` → Scheduler tuning.

## [0.18.0] — 2026-04-29

### Added

- **Corporate-memory tree view + cross-axis filtering** on `/corporate-memory` and `/corporate-memory/admin`. Operators choose a grouping axis (domain / category / tag / audience) and combine it with chip filters (status, source_type, audience, has-duplicate-hint, search). Tree uses native `<details>`; localStorage persists open/closed state per axis; no new dependencies. Issue #62.
- **Corporate-memory duplicate-candidate hints** — admin sees a "Duplicate Candidates" tab with likely-duplicate item pairs detected by entity overlap (Jaccard score, ≥2 shared entities, same domain). Resolution actions: `duplicate` / `different` / `dismissed`. Auto-merge intentionally not included. Issue #62.
- **Bulk-edit endpoints**: `PATCH /api/memory/admin/{id}` (now accepts category/domain/tags/audience/title/content, was title+content only); `POST /api/memory/admin/bulk-update` for multi-id mutations with per-item audit rows; new "Move to category / domain / audience" + "Add/Remove tag" actions in the admin batch bar. Issue #62.
- **`GET /api/memory/stats`** now includes `by_tag` (DuckDB `json_each` over tags) and `by_audience` aggregations to power chip-filter pickers. Issue #62.
- **`GET /api/memory/tree?axis=...&...`** — server-side grouping endpoint that returns `{groups: [{key, label, count, items: [...]}]}`, RBAC-filtered, with chip filters (`status_filter`, `source_type`, `audience`, `q`, `has_duplicate`). Issue #62.
- **`da admin memory {tree,edit,bulk-edit,stats,duplicates list,duplicates resolve}`** — full CLI parity for the new admin endpoints. Issue #62.
- **Schema v17**: new `knowledge_item_relations` table for duplicate-candidate hints. PK `(item_a_id, item_b_id, relation_type)` with canonical `(min, max)` pair ordering at the repository layer; auto-migration v16→v17 idempotent. Issue #62.
- **BigQuery table registration via admin UI + CLI (issue #108 — Milestone 1).** Operators on a BigQuery instance can now register a BQ table or view as a remote DuckDB master view from `/admin/tables` or `da admin register-table`, without hand-editing `table_registry` or running the extractor by hand. The register modal branches on `data_source.type` server-side: BQ instances see Dataset / Source Table / View Name / Description / Folder / Sync Schedule; Keboola instances keep the discovery-driven flow. Submit runs `/api/admin/register-table/precheck` first (round-trips `bigquery.Client.get_table` to confirm the table exists and the SA can see it; surfaces row count + size + column count in the modal), then commits. The server validates BQ-specific shape (dataset / source_table / DuckDB-safe identifiers / GCP project_id grammar), forces `query_mode=remote` + `profile_after_sync=false`, and synchronously rebuilds `extract.duckdb` + master views with a 5s wall-clock budget — on overrun, the rebuild continues in a `BackgroundTask` and the API returns 202 with `{"status": "accepted", "view_name": ...}` instead of 200. View-name collisions (distinct from id collisions) return 409 to stop two callers from registering the same DuckDB view via different display names. `sync_schedule` is accepted and stored but not yet evaluated by the scheduler — see issue #79; addressed in Milestone 3 of #108. See `docs/DATA_SOURCES.md`.
- `POST /api/admin/register-table/precheck` — validation-only sibling of register-table. Returns `{"ok": true, "table": {rows, size_bytes, columns, …}}` for BQ rows after round-tripping `get_table`; surfaces NotFound → 404, Forbidden → 403, anything else → 400 with the GCP error verbatim. Also runs Pydantic validation for non-BQ source types so the CLI / UI gets a single endpoint shape.
- `--dry-run` flag on `da admin register-table` — calls `/precheck` and pretty-prints rows / bytes / columns; exits 0 on `ok`, 1 on validation or source-side error.
- Audit-log entries on every `register_table` / `update_table` / `unregister_table` mutation — closes the asymmetry where instance-config saves audited but registry mutations didn't (Decision 4 in #108). Secret-named fields in the request payload are masked as `***`; `description` is logged raw.
- **Google Workspace group prefix filter + system-group mapping.** Three new env vars wire the OAuth callback's group sync to a configurable Workspace prefix and route the admin/everyone Workspace groups into the seeded system rows.
  - `AGNES_GOOGLE_GROUP_PREFIX` — when set (e.g. `grp_acme_`), only Workspace groups whose email local part starts with the prefix are mirrored into `user_group_members`. Empty = legacy behavior (mirror every fetched group).
  - `AGNES_GROUP_ADMIN_EMAIL` — Workspace group email that maps onto the seeded `Admin` system row instead of creating a fresh `user_groups` entry. Members of that Workspace group land in `Admin` directly.
  - `AGNES_GROUP_EVERYONE_EMAIL` — same mechanism for `Everyone`.
- **Login gate.** When `AGNES_GOOGLE_GROUP_PREFIX` is set and the user's Workspace fetch returned a non-empty list with zero prefix matches, the callback redirects to `/login?error=not_in_allowed_group` with a friendly inline banner. Empty fetch results (transient Cloud Identity failures) preserve the cached membership and let the login proceed — fail-soft only the soft-fail path; an explicit no-match still blocks. New error code `group_check_unavailable` is wired through the login banner for future use.
- **Admin UI subtitle for synced groups.** The `/admin/groups` table and the `/admin/groups/{id}` detail page render a derived display name (prefix stripped, `@domain` removed, capitalized) above a small monospace subtitle showing the full Workspace email. Edit / Delete affordances are hidden on Google-managed rows, and a "managed by Google Workspace — read-only here" banner appears on the detail page.

### Changed

- **BREAKING** Auto-`Everyone` membership for new users was removed. `UserRepository.create` no longer writes a `user_group_members` row, and `app.auth.access._user_group_ids` no longer adds a virtual `Everyone` id to the result. Every membership now traces to a real source row (`admin`, `google_sync`, or an explicit `system_seed`). If you relied on the implicit-Everyone behavior for plugin visibility, grant the plugin to a real group (e.g. an `everyone@example.com` Workspace group mapped via `AGNES_GROUP_EVERYONE_EMAIL`).
- **Admin UI / API are read-only on Google-managed groups.** `created_by='system:google-sync'` rows, plus the seeded `Admin` / `Everyone` rows when the matching email-mapping env var is set, return `409` with body `{"detail": {"code": "google_managed_readonly", ...}}` from `PATCH /api/admin/groups/{id}`, `DELETE /api/admin/groups/{id}`, `POST /api/admin/groups/{id}/members`, `DELETE /api/admin/groups/{id}/members/{user_id}`, `POST /api/admin/users/{id}/memberships`, `DELETE /api/admin/users/{id}/memberships/{group_id}`. Edit through admin.google.com, then sign in again to refresh.
- **Audit action names for corporate-memory operations renamed** from `km_<action>` to `corporate_memory.<action>` to match the 0.15.0 CHANGELOG documentation. The audit-tab filter accepts both prefixes for back-compat with rows already in the audit log (no historical-row rewrite). Issue #62.
- **`onDomainChange()` UX bug fixed** on `/corporate-memory`: domain and category filters now compose instead of resetting each other when either changes. Issue #62.
- `POST /api/memory/admin/edit` continues to accept title/content as before; the new `PATCH /api/memory/admin/{id}` is the recommended path for everything else (including title/content). The legacy endpoint is kept one release for back-compat.

### Internal

- New env vars surfaced into `ConfigProxy` so templates can derive the friendly display name client-side.
- New `is_google_managed: bool` field on `GroupResponse` (the API surface for the admin UI's group list/detail).
- New `UserGroupMembersRepository.has_any_google_sync_membership` helper (currently diagnostic; kept for a future tightening of the gate).
- New tests in `tests/test_google_group_prefix_sync.py`; `tests/test_repositories.py::TestUserRepositoryEveryoneAutoMember` renamed to `TestUserRepositoryNoAutoMembership` with inverted assertion; two `tests/test_marketplace_filter.py` tests adapted to the no-implicit-Everyone semantics. See `docs/auth-groups.md` for the full reference.

### Fixed

- `PATCH /api/memory/admin/{id}` now switches from `model_dump(exclude_none=True)` to `exclude_unset=True`, so an explicit `null` in the request body clears the field (e.g. `{"audience": null}` resets a previously-set audience to NULL). Pre-fix nulls were silently dropped, leaving no path to clear `audience` and only the empty-string short-circuit for `domain`. The endpoint now distinguishes "field absent from body" (untouched) from "field explicitly set to null" (cleared). Both `PATCH /api/memory/admin/{id}` and `POST /api/memory/admin/bulk-update` now reject an explicit `null` for `title` (NOT NULL in the schema) at the boundary with HTTP 400 instead of bubbling up as a 500 (PATCH) or per-item Constraint Error (bulk). Issue #62 / PR #126 review.
- Empty-string `domain` is now consistently allowed across `POST /api/memory`, `PATCH /api/memory/admin/{id}`, and `POST /api/memory/admin/bulk-update` — previously create allowed it (short-circuit on falsy) but PATCH/bulk-update rejected it with 400, which made it impossible to clear a domain through the admin endpoints. Issue #62 / PR #126 review.
- Bulk-edit modal `(unset)` option for the domain field is now actually submittable. Pre-fix the JS rejected empty values with "Please enter a value" before the request fired, so the operator-visible "(unset)" option couldn't ever clear a domain even though the backend supports it. Issue #62 / PR #126 review.
- **`POST /api/memory/admin/bulk-update` now enforces an API-layer allowlist** of mutable fields (`category`, `domain`, `tags`, `tags_add`, `tags_remove`, `audience`, `title`, `content`). Pre-fix the endpoint forwarded any key in the repo's `_UPDATABLE_FIELDS` set, which included `status`, `sensitivity`, `is_personal`, and `confidence` — an admin could `POST {"updates": {"status": "mandatory"}}` and silently bypass `/admin/mandate`'s dedicated audit trail (the bulk audit row also stamped `updated_fields: []` for those mutations, leaving no trace of what changed). Disallowed keys now return HTTP 400 with the offending list; the repo layer is unchanged so the per-item `repo.update` path keeps its broader access. Issue #62 / PR #126 review.
- **Tree endpoint `audience=all` chip now includes NULL-audience items**, matching the SQL audience filter (`audience IS NULL OR audience = 'all'`), `count_by_audience` (COALESCE→'all'), and `_bucket_key` (NULL → "all"). Pre-fix the in-memory chip filter compared `item.audience != 'all'` and dropped NULL-audience items from the bucket they were supposed to land in. Issue #62 / PR #126 review.
- **`GET /api/memory/admin/audit` honors `page`** — the SQL had `LIMIT` only and silently returned the first page for every page parameter. Both branches (`action`-filtered and unfiltered) now apply `OFFSET (page - 1) * per_page`. Issue #62 / PR #126 review.
- **`POST /api/memory` validates `domain`** against the same allowlist `PATCH /admin/{id}` and `POST /admin/bulk-update` use, so an item can't be created with a domain it can't later be patched to. Empty / missing domain is still accepted. Issue #62 / PR #126 review.
- **`da admin memory edit --add-tag/--remove-tag` could silently drop existing tags** when the target item lived past page 1 of `/api/memory`. The CLI did GET-then-PATCH for tag mutations, looked the item up in the first 50 rows of the unfiltered list, and overwrote the tag set with `[just_added]` when it didn't find the row. Tag mutations now route through `POST /api/memory/admin/bulk-update` (single-id array, server-side merge with the existing tags). Issue #62.
- **`da admin memory duplicates list` couldn't list both resolution states** — the CLI always sent `resolved=true|false` and the API defaulted to `resolved=false` when omitted, so neither path returned the full set. `GET /api/memory/admin/duplicate-candidates` now treats omitted `resolved` as "no filter"; the CLI omits the flag by default and only sets it when the user passes `--resolved` or `--unresolved`. The web UI continues to pass `resolved=false` explicitly so the actionable backlog stays the default surface. Issue #62.
- `PUT /api/admin/registry/{id}` now preserves the original `registered_at` timestamp instead of resetting it to `now()` on every edit. `TableRegistryRepository.register` accepts `registered_at` as an optional kwarg; `update_table` re-passes the existing value from the row it just read. Closes #130.

## [0.17.0] — 2026-04-29

### Added

- **Shared-secret auth path for the in-cluster scheduler service** (`SCHEDULER_API_TOKEN`). Both the `app` and `scheduler` containers source the same `/opt/agnes/.env` via Docker Compose `env_file:`, so a 256-bit secret generated once at VM provisioning serves both sides symmetrically. The app validates incoming `Authorization: Bearer <secret>` against the env var (constant-time compare; minimum length 32 chars; rejected when env is empty) and resolves matches to a synthetic `scheduler@system.local` user that is a member of the `Admin` system group — every existing RBAC gate (`require_admin`, `require_resource_access`) works unchanged. Audit-log entries from the scheduler are attributed to this user. Rotation: edit `.env`, `docker compose restart app scheduler`. See `app/auth/scheduler_token.py` for the threat model.
- **`POST /api/marketplaces/sync-all`** — admin-only endpoint that runs `src.marketplace.sync_marketplaces()` inside the app process. Wired up so the scheduler container can drive the nightly refresh over HTTP without opening `system.duckdb` directly.

### Fixed

- **Scheduler `marketplaces` job 500-ed every cron tick with `IO Error: Could not set lock on file system.duckdb` after v0.12.1.** The previous implementation called `src.marketplace.sync_marketplaces()` in-process from the scheduler container, but DuckDB permits only one writer per file across processes — the scheduler raced the app's long-lived handle. Switched the job to `POST /api/marketplaces/sync-all`, making the app the sole writer; the scheduler is now a pure cron clock.
- **Scheduler `data-refresh` job 401-ed every 15 minutes** with `Missing or invalid Authorization header` because `SCHEDULER_API_TOKEN` was never propagated by `infra/modules/customer-instance/startup-script.sh.tpl`. The startup script now generates a 64-hex-char secret on first boot via `openssl rand -hex 32`, persists it across reboots by reading back from an existing `.env` (rotation requires explicit operator action — both containers must restart together), and writes it into `/opt/agnes/.env` alongside the other secrets. `app/main.py` seeds the matching synthetic user at startup so the very first cron tick has a valid actor to attribute audit-log entries to. Existing VMs need a one-time `sudo /opt/agnes/agnes-rotate-scheduler-token.sh` (or simply re-run the startup script via `terraform apply -replace='module.agnes.google_compute_instance.vm["<vm-name>"]'`); see migration note in this changelog or rerun the startup script manually.
- **Non-root container couldn't write to host-bind-mounted `/data` after the v0.12.1 USER-agnes flip.** `infra/modules/customer-instance/startup-script.sh.tpl` now `chown -R 999:999 /data` after creating the persistent-disk subdirs (`state`, `analytics`, `extracts`). Without this, a freshly-attached PD is root-owned by default and `USER agnes` (uid 999) cannot open `/data/state/system.duckdb` for write — every authed request 500s with `IOException: Cannot open file ... Permission denied` while `/api/health` (which doesn't open the system DB) keeps returning 200, masking the failure from health-only monitoring. Regression first observed on `agnes-development` on 2026-04-29 after the auto-upgrade picked up `:stable` from the 0.12.1 release. **Existing VMs with PD-backed `/data` need a one-time host-side `sudo chown -R 999:999 /var/lib/docker/volumes/agnes_data/_data && sudo docker restart agnes-app-1 agnes-scheduler-1` to recover** — Terraform `metadata_startup_script` only runs on boot, so an apply alone does not retro-fix running VMs.
- `Dockerfile` pins the `agnes` user to `uid:gid 999:999` explicitly (`useradd --uid 999`). Previously the uid was whatever Debian's `useradd --system` assigned next — happened to be 999 today, but a future base-image change picking 998 or 1000 would silently desync from the startup-script's `chown 999:999`, reintroducing the same incident. Pinning makes the contract grep-able from both sides.
- `scripts/smoke-test.sh` no longer silently SKIPs every authed check when `bootstrap` returns 403 (users exist) and `SMOKE_TOKEN` is not set — it now FAILs loudly. Also adds an unauthenticated DB-touching probe (`POST /auth/email/request`) before bootstrap, since `/api/health` deliberately doesn't open `system.duckdb` (kept cheap for LB probes) and so cannot detect filesystem/permission issues. The new probe catches a class of regression that bypasses health-only monitoring even on instances where bootstrap is closed.
- Corporate memory pages (`/corporate-memory`, `/corporate-memory/admin`) now render the shared app header at full viewport width, matching the dashboard. Previously the `_app_header.html` include sat inside `.container-memory` (max-width: 1000px) and was cropped on wide viewports.
- `release.yml` now publishes a `:dev-<slug>` + `:dev-<prefix>-latest` image when a fresh branch is pushed off `main` with no extra commits. Pre-fix, `paths-ignore` on the `push` event diffed the new ref against the default branch — a same-SHA branch had zero diff, every file matched paths-ignore, and the workflow was skipped, so a developer creating a personal branch off main to deploy main's exact state to their dev VM (which pins to `:dev-<user>-latest`) had to either commit something or trigger the workflow manually. The `build-and-push` job's `if` was also tightened to `main || workflow_dispatch` only, which prevented branch-push images regardless. Both fixed: added `create:` trigger (filtered to branch refs at the job level so tag creates don't double-build with `keboola-deploy.yml`), and broadened `build-and-push.if` to also publish on non-main branch pushes / branch creates.
- Web header admin nav (All tokens, Marketplaces, Admin → Users / Groups / Resource access / Server config) is now visible to admin users again. Pre-fix, `_app_header.html` gated the admin block on `session.user.role == 'admin'`, but the v13 RBAC migration nulled `users.role` and moved admin authority onto `user_group_members` (Admin system group) — so the gate evaluated to false for everyone, including actual admins. `get_current_user` now injects `user["is_admin"]` (computed via `app.auth.access.is_user_admin`, the same call all server-side admin gates use), and the header reads `session.user.is_admin`. The role badge in the user-menu dropdown now reads "Admin" or hides — `users.role` is no longer surfaced in the UI.
- `admin_tables` register modal payload now matches the `RegisterTableRequest` API contract — drops the phantom `id` and `version` fields the modal used to send (the API silently dropped them), and renames `dataset` → `bucket` so the source-bucket actually persists. Pre-fix the operator's bucket / dataset edit looked saved but never made it past the wire. Edit + delete handlers in the same template were dropping the same fields and are also corrected.
- Discovery JS in `admin_tables` now handles the actual `{tables: [...]}` flat shape returned by `GET /api/admin/discover-tables`. Pre-fix the JS expected `{buckets: [...]}` (a shape the API never emitted) and silently rendered an empty discovery panel after the first call.
- **#108 review fixes for BigQuery register-table.** (a) The post-register materialize worker (BackgroundTask + 5s-timeout daemon thread) no longer captures the request-scoped DuckDB connection — it opens a fresh `get_system_db()` handle per run, so the request's `finally: conn.close()` no longer races the worker. (b) `connectors/bigquery/extractor.init_extract` is now serialized by a module-level `_INIT_EXTRACT_LOCK` so the timeout-fallback BackgroundTask cannot collide with the still-running daemon thread on the `extract.duckdb` swap. (c) `PUT /api/admin/registry/{id}` now runs the same BQ-shape validation as register when the merged record is a BigQuery row (or the patch flips it to BigQuery), returning 400/422 instead of silently persisting an unsafe `bucket` / `source_table` / project_id and breaking at the next rebuild. (d) `POST /api/admin/register-table` no longer carries a misleading `status_code=201` on the route decorator — the Keboola branch explicitly returns 201, the BigQuery branch returns 200 (sync) or 202 (timeout fallback), and OpenAPI now documents all three.
- **#108 round-4 review fix for BigQuery register-table.** `_validate_bigquery_register_payload` now applies the same raw-value rule to `bucket` and `source_table` as round-3 added for `name`. Pre-fix the helper validated `bucket.strip()` / `source_table.strip()` but `register_table` persisted the un-stripped value, so a `bucket=" my_dataset"` slipped through validation, got stored verbatim, and 500'd at the next rebuild when the BQ extractor spliced it into `ATTACH … AS bq_<bucket>` and view DDL. The validator now rejects any `bucket` / `source_table` with leading/trailing whitespace and surfaces the offending raw value in the 400 detail. Applies identically to `POST /api/admin/register-table` and `POST /api/admin/register-table/precheck`.
- **#108 round-3 review fixes for BigQuery register-table.** (a) `_validate_bigquery_register_payload` now validates the **raw** view name (the value persisted to `table_registry.name` and read back by the BQ extractor), not a normalized `strip().lower().replace(" ", "_")` form. Pre-fix a name like `"my table"` passed validation (normalized `"my_table"` was safe), got stored verbatim, and then 500'd at the post-insert rebuild — defeating fast-fail-at-register. The validator now rejects any name with leading/trailing whitespace OR that fails the strict `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` check, and surfaces the offending raw value verbatim in the 400 response so the operator can retype with a corrected name. Server does NOT silently rewrite the input. Applies identically to `POST /api/admin/register-table` and `POST /api/admin/register-table/precheck`. (b) `_run_bigquery_materialize_with_timeout` now distinguishes worker-raised-within-budget (→ `{"status": "errors"}` → HTTP 500 with the exception in the body) from worker-still-running-at-timeout (→ `{"status": "timeout"}` → HTTP 202 + BackgroundTask retry). Pre-fix both outcomes mapped to "timeout" / 202, hiding the real failure for the budget window before the BG retry surfaced the same exception in the logs. (c) `register_table_precheck` is now a plain `def` (was `async def`) — the BQ branch makes synchronous `bigquery.Client(...)` / `client.get_table(...)` calls that would otherwise block the asyncio event loop on an async handler. Mirrors the same conversion already done for `register_table`.
- **#108 round-2 review fixes for BigQuery register-table.** (a) `POST /api/admin/register-table` is now a plain `def` (was `async def`) — the synchronous-materialize path waits on `threading.Event.wait()`, which blocks the asyncio event loop on an async handler and stalls every other request for up to the 5s budget. FastAPI runs sync handlers in a threadpool so the wait is harmless there. (b) `connectors/bigquery/extractor.rebuild_from_registry` now resolves `data_source.bigquery.project` via `app.instance_config.get_value` (deep-merge of static + writable overlay) instead of `config.loader.load_instance_config` (static only). Operators who set the project through `POST /api/admin/configure` got a silent rebuild failure pre-fix — validation passed (validation already used the overlay-aware read) but the rebuild reported "project missing" and the master view never appeared. (c) `register-table` now propagates `rebuild_from_registry` errors as **HTTP 500 with `{"status": "rebuild_failed", "errors": [...]}`** when the synchronous rebuild ran but reported an error (auth failure, missing project, unsafe identifier slipping the validator). Pre-fix those errors were silently logged and the API returned 200 ok. The BackgroundTask path now logs rebuild errors at ERROR level (was WARNING). (d) The admin tables UI's BigQuery register modal now splits precheck and register into two operator-driven clicks — Step 1 fires precheck and surfaces row count / size / column count in the modal AND swaps the primary button to "Register"; Step 2 fires the actual register call only when the operator clicks. Pre-fix the precheck and register fired in a single chained promise, so the operator never got to review the summary before the row was committed. (e) The Keboola register-modal payload now derives `source_table` from the discovered table's storage identifier (`t.id` minus the bucket prefix, e.g. `company` for `in.c-sfdc.company`) via a new hidden `regSourceTable` field. Pre-fix the JS sent `regTableName` (the human-friendly display name) as `source_table`; manual-entry callers fall back to the display name. (f) `da admin discover-and-register` accepts HTTP 200 / 201 / 202 as success (was 201 only); pre-fix every successful BigQuery row counted as an error because BQ register returns 200 (sync OK) or 202 (background) but never 201.

### Internal

- `_sanitize_for_audit` now masks against an explicit `_SECRET_FIELDS` allowlist instead of substring-scanning + maintaining a `primary_key` whitelist exception. New tests assert `not_actually_a_token` / `primary_key_hash` / `passwordless` flow through cleartext while known-secret fields (`keboola_token`, `client_secret`, `smtp_password`, `bot_token`) get masked. Operationally identical for the current registry payloads (no secret-bearing fields), but removes a class of false-positive / false-negative as the request body grows.
- `release.yml` adds an `e2e-bind-mount` job that boots the freshly built image against a host-bind-mounted `/data` directory (instead of the named volume the existing `smoke-test` job uses). Docker initializes a fresh named volume by copying from the image's `/data` — which the Dockerfile chowns to `agnes:agnes` before flipping USER — so the named-volume path always works. The bind-mount path mirrors what GCE VMs run via `docker-compose.host-mount.yml`, and includes a negative assertion (write must fail on root-owned `/data` before the operator chown) plus a positive assertion (smoke passes after the chown). Locks in the contract that broke a recent release: removing `chown 999:999` from `startup-script.sh.tpl` or changing the Dockerfile uid pin breaks CI.
- Extracted `bigquery.extractor.rebuild_from_registry()` from the `__main__` block of `connectors/bigquery/extractor.py` so the API can call it post-register without `runpy`-importing the module. The standalone CLI entrypoint (`python -m connectors.bigquery.extractor`) keeps working.

## [0.16.0] — 2026-04-29

Minor release. Comprehensive deploy safety audit — CI/CD pipeline hardening, 50+ new tests covering previously untested failure modes, DB schema health check, config versioning, and BigQuery ATTACH error resilience. Built on top of v0.15.0 / `2e1dfb7`.

PR: [#120](https://github.com/keboola/agnes-the-ai-analyst/pull/120) (ci/deploy-safety-audit).

### Added

- **ruff lint + mypy type check** in `release.yml` and `keboola-deploy.yml` CI workflows (both `continue-on-error: true` initially — 257 pre-existing ruff errors, mypy has pre-existing issues; neither blocks CI yet).
- **Automatic rollback** on smoke test failure in `release.yml` — tags the broken image as `:deprecated-<short-sha>`, reverts `:stable` to the previous good tag, opens a GitHub issue for investigation.
- **Smoke test in `keboola-deploy.yml`** — was completely missing; now runs the same `smoke-test.sh` as `release.yml`.
- **Expanded smoke-test.sh** — added `/api/catalog`, `/api/admin/tables`, `/marketplace.zip`, `/api/metrics` endpoint checks beyond the original `/api/health`.
- **Post-deploy smoke test** (`scripts/ops/post-deploy-smoke-test.sh`) — validates health, DB schema version, query endpoint, catalog, and marketplace on a prod VM after deploy.
- **DB schema version check** in `/api/health` — returns `db_schema: "ok" | "mismatch" | "unreachable"`; overall status becomes `"unhealthy"` on schema mismatch. Lets load balancers and monitoring detect half-migrated instances.
- **Config versioning** — `config_version: 1` in `instance.yaml`, validated at startup by `_validate_config_version()` in `config/loader.py`. Prevents silent misconfiguration when the config schema evolves.
- **`.github/settings.yml`** — required status checks on `main` branch.
- **`.github/dependabot.yml`** — weekly pip + github-actions dependency updates.
- **`.github/CODEOWNERS`** — default `@keboola/agnes-team`, special owners for `/infra/`, `/app/auth/`, `src/db.py`.
- **`.pre-commit-config.yaml`** — detect-private-key, check-yaml/json/merge-conflict, ruff, mypy.
- **`[tool.ruff]`** config in `pyproject.toml` — `line-length = 120`, `target-version = "py313"`.

### Test Coverage (~50 new tests)

- **v13→v14 migration** (`test_db.py`): orphan cleanup, FK constraints, rollback on failure.
- **Email magic link TTL** (`test_auth_providers.py`): expired token, token reuse, wrong token.
- **PAT** (`test_pat.py`): malformed JWT, empty bearer, `last_used_ip` tracking.
- **Marketplace ZIP** (`test_marketplace_server_zip.py`): ETag/304, PAT auth, content-addressed caching, `invalidate_etag_cache()` on mutation.
- **Marketplace Git** (`test_marketplace_server_git.py`): smart HTTP, Basic auth with PAT, RBAC filtering.
- **Jira webhooks** (`test_jira_webhooks.py`): HMAC validation, missing signature, malformed JSON (10 tests).
- **Hybrid Query BQ** (`test_remote_query.py`): `register_bq`, JOIN local+BQ, error handling (12 tests).
- **Keboola extractor** (`test_keboola_extractor.py`): crash, partial write, timeout, extension fallback (9 tests).
- **BigQuery extractor** (`test_bigquery_extractor.py`): corrupted DB, partial write, atomic swap, ATTACH timeout (6 tests).
- **Orchestrator** (`test_orchestrator.py`): corrupted extract.duckdb, empty `_meta`, mid-write, unsafe identifiers (5 tests).

### Fixed

- **BigQuery extractor ATTACH error handling** — `init_extract()` now catches exceptions on `INSTALL`/`ATTACH` and records them in `stats["errors"]` instead of propagating up. A network timeout or auth failure no longer crashes the extractor; all configured tables are marked as skipped.
- **ETag cache invalidation on disk mutation** — `invalidate_etag_cache()` is the documented way to force re-hash after marketplace sync. Tests now call it after mutating on-disk content.

### Internal

- `fetch-depth: 0` + `fetch-tags: true` in `release.yml` for rollback tag resolution.
- Docs updated: `ARCHITECTURE.md`, `docs/DATA_SOURCES.md`, `docs/QUICKSTART.md`, `docs/RBAC.md`, `docs/auth-groups.md`.

## [0.15.0] — 2026-04-29

Minor release. Adds corporate memory v1+v1.5 and /me/debug self-only auth diagnostic. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.15.0) for full notes.

## [0.14.0] — 2026-04-28

Minor release. Replaces BigQuery wrap-view pattern with Claude-driven fetch primitives. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.14.0) for full notes.

## [0.13.0] — 2026-04-28

Minor release. Admin server-config editor + Windows PowerShell wrapper. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.13.0) for full notes.

## [0.12.1] — 2026-04-28

Patch release. Hotfixes the pre-migration snapshot-integrity bug shipped in [v0.12.0](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.0) and bundles the security/ops hardening from issue groups #82 (auth hardening), #85 (API validation), #87 (deploy posture), plus #46 (SSRF) and #90 (memory stats blocking).

### Added

- Path-traversal validation on `/api/data/{table_id}/download` — `table_id` is
  now checked against `_SAFE_QUOTED_IDENTIFIER` regex (allows dots and hyphens
  for Keboola-style IDs like `in.c-crm.orders`) before any filesystem or DB
  operation; unsafe values return 404 (no info leakage). See issue #85/C2.
- SSRF protection on `POST /api/admin/configure` — `keboola_url` is validated
  against private/reserved networks (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16, localhost, IPv6 loopback/link-local/unique-local). Uses
  DNS resolution + `ipaddress` module for robust IPv6 handling (catches
  abbreviated forms like `fe80::1`, `fc00::1`). See issue #46.
- Caddyfile security headers: `X-Frame-Options DENY`,
  `X-Content-Type-Options nosniff`,
  `Referrer-Policy strict-origin-when-cross-origin`, `-Server` (strip).
  See issue #87/M22.
- Container runs as non-root user `agnes` — `USER` directive added to
  Dockerfile with `useradd` + `chown`. See issue #87/C13.
- Docker resource limits: `mem_limit: 4g`, `mem_reservation: 1g`,
  `cpus: 2.0` on `app`; `mem_limit: 2g`, `cpus: 1.0` on `scheduler`.
  See issue #87/M21.
- Startup warning when no user has `password_hash` — alerts operators that
  `/auth/bootstrap` is reachable. See issue #82/C8.
- Audit logging for failed web form login attempts (`/auth/password/login/web`)
  — mirrors the existing `/auth/token` audit trail. See issue #82/M9.
- `/api/health/detailed` endpoint (authenticated) — returns full diagnostics
  (version, schema, sync state, user count). Minimal `/api/health` (unauth)
  returns only `{"status": "ok"}` for load balancers. See issue #87/M17.
- Health endpoint monitoring guide in `docs/DEPLOYMENT.md` — documents both
  endpoints and how to wire external monitoring tools (Datadog, Prometheus,
  UptimeRobot) to `/api/health/detailed` with a PAT.

### Changed

- **BREAKING** `docker-compose.override.yml` renamed to `docker-compose.dev.yml`.
  Docker Compose auto-merges `docker-compose.override.yml` on every host with
  the repo, silently enabling dev mode (source mount + `--reload`) on
  production. The new name requires explicit `-f docker-compose.dev.yml`,
  eliminating the foot-gun. Update any scripts or workflows that relied on
  auto-merge. `scripts/run-local-dev.sh` and `Makefile` updated accordingly.
  See issue #87/M23.
- **BREAKING** `/api/health` now returns a minimal `{"status": "ok"}` payload
  (unauthenticated, for load balancers). Full diagnostics moved to
  `/api/health/detailed` (requires authentication). Scripts that parsed
  `/api/health` for version, sync state, or user count must switch to
  `/api/health/detailed` with an `Authorization` header. CLI commands
  (`da setup test-connection`, `da setup verify`, `da diagnose`, `da status`)
  updated to call `/api/health/detailed` for service-level checks, with
  graceful fallback to the minimal endpoint when auth is not configured.
  See issue #87/M17.
- `release.yml` CI workflow: `build-and-push` job now only runs on `main`
  pushes or manual `workflow_dispatch` triggers. Non-main branch pushes run
  tests only. Added `paths-ignore` for `docs/**`, `*.md`, `LICENSE`.
  See issue #87/M26.

### Fixed

- **Pre-migration snapshot integrity** — the snapshot file written
  before a v(N-k)→vN migration now captures the true on-disk state
  *before* any DDL runs, instead of the post-self-heal state the
  0.12.0 hoist (#106) introduced. With the unconditional
  `conn.execute(_SYSTEM_SCHEMA)` at the top of `_ensure_schema`, the
  full set of modern-binary tables (`view_ownership`,
  `marketplace_registry`, `user_groups`, `resource_grants`, etc.) was
  materialized first, then `CHECKPOINT` flushed them to disk, and
  `shutil.copy2` copied the already-modified DB as the
  "pre-migration" snapshot — so an operator inspecting the snapshot
  for rollback debugging saw the binary's full table set instead of
  the old schema. Functionally rollback still worked (extra empty
  tables are harmless and re-running migration is idempotent), but
  the snapshot was misleading. Fix: gate the self-heal call on
  `current >= SCHEMA_VERSION`. The split-brain (`current >
  SCHEMA_VERSION`) and same-version safety-net (`current ==
  SCHEMA_VERSION`) paths still self-heal as before; the migration
  path (`current < SCHEMA_VERSION`) takes its snapshot first and
  then runs `_SYSTEM_SCHEMA` from inside the existing migration
  block.
- `reset_token` no longer leaks in the JSON response body of
  `POST /api/users/{id}/reset-password`. The `reset_url` still contains the
  token (as intended), but the raw secret is no longer exposed to DevTools,
  proxy logs, or CLI stdout. CLI `admin reset-password` now prints the URL
  instead of the bare token. See issue #82/C5.
- `/api/memory/stats` no longer blocks the async event loop — replaced
  `repo.list_items(limit=10000)` + Python loop with a single SQL
  `GROUP BY` aggregation. See issue #90.
- Magic-link token consumption is now atomic — compare-and-swap pattern
  with a unique `CONSUMED:` marker prevents two concurrent verifies from
  both succeeding. DuckDB concurrent-write conflicts are caught and
  converted to 401. See issue #82/M10.
- Password reset confirm (`POST /auth/password/reset/confirm`) now uses
  the same compare-and-swap pattern as the magic-link flow — closes the
  remaining asymmetry on `users.reset_token` consumption. Lower severity
  than the magic-link race because the reset flow ends with a new
  password (an attacker would need the reset token *and* to race the
  legitimate user) but the consistency closes a polish gap. New
  regression `test_concurrent_reset_only_one_wins` in
  `tests/test_password_flows.py::TestResetConfirm`.
- Upload endpoints (`/sessions`, `/artifacts`) now stream to a temp file with
  cumulative size check instead of buffering the entire body in memory before
  the size cap — prevents OOM from oversized uploads. Temp file handle is
  properly closed before `shutil.move` to avoid FD leaks. See issue #85/M4.
- `/api/upload/local-md` uses a SHA-256 hashed filename instead of raw
  `user_email` — stable per user, no charset surprises from email addresses.
  See issue #85/M4.
- `/auth/bootstrap` 403 message no longer leaks user count. See issue #82/n1.

### Internal

- New regression `test_split_brain_future_version_with_missing_tables_self_heals`
  in `tests/test_db.py::TestMigrationSafety` — synthesizes a v99 DB whose only
  table is `schema_version`, runs `_ensure_schema`, asserts that the v13-era
  core tables (`users`, `user_groups`, `user_group_members`, `resource_grants`)
  get materialized *and* that `schema_version` stays at 99 (self-heal without
  falsely advertising a downgrade).
- New regression `test_pre_migration_snapshot_excludes_post_self_heal_tables`
  pins the snapshot-integrity contract: a v2→vN migration's snapshot must not
  contain any post-v2 table from the modern binary. Sanity-checked against the
  pre-fix unconditional hoist — fails with 6 leaked tables.
- `test_future_version_is_noop` docstring updated to reflect that the
  self-heal pass *does* run on a future-version DB, just doesn't touch the
  version row. The test still passes unchanged — its only assertion was the
  version-row contract, which holds.
- `test_no_override_file` regression test asserts `docker-compose.override.yml`
  does not exist post-rename. See issue #87/M23.

## [0.12.0] — 2026-04-28

### Changed

- `/admin/access` resource tree now visually separates the three-level hierarchy (resource type → block/bucket → item). Each resource-type section gets a colored left stripe and a faint tinted banner; sections are separated by an 8px neutral gap. Stripe colors cycle 4-wide via `nth-child` so adding new resource types to `app/resource_types.py` works without touching CSS. The first-position color is the project primary blue (`#0073D1`), avoiding the violet (`#6366f1`) reserved for granted items.

### Added

- `ResourceType.TABLE` — admins can grant table-level access per `user_group` via the `/admin/access` page. Tables registered in `table_registry` are listed grouped by `bucket`, with the existing per-block "Grant all" / "Revoke all" bulk actions. Listing and grant storage only — runtime enforcement still flows through legacy `dataset_permissions`; the migration plan lives in `docs/TODO-rbac-data-enforcement.md`.
- `AGNES_ENABLE_TABLE_GRANTS` env var (default off) gates the half-built `ResourceType.TABLE` chip. While disabled the chip is hidden from `/admin/access` and `POST /api/admin/grants` returns **422** with the env-var name in `detail` on a TABLE grant attempt. Existing TABLE rows in `resource_grants` stay listable and deletable — the flag controls UI exposure and new-grant acceptance only, never blocks cleanup.
- `da admin break-glass <user>` CLI — recovery path when the operator has locked themselves out of `/admin/access`. Adds the user to the Admin user_group with `source='system_seed'` regardless of RBAC state. Bypasses authentication; relies on filesystem access to `${DATA_DIR}/state/system.duckdb` implying host-level trust. Document this in deployment runbooks alongside `SEED_ADMIN_EMAIL`.

### Internal

- `scripts/seed_dummy_tables.py` — populates `table_registry` with 12 dummy tables across 3 buckets (`in.c-finance`, `in.c-marketing`, `in.c-product`), each with `is_public=False`, for exercising the new `/admin/access` Tables section without a configured data source.
- `/marketplace.zip` short-circuits to `304` before any file IO or ZIP compression on a matching `If-None-Match`. Hot path on every Claude Code SessionStart hook. Backed by an in-process `cachetools.TTLCache` over the resolved-plugins → ETag map (default 120s, env-tunable via `AGNES_MARKETPLACE_ETAG_TTL`, set `0` to disable). `invalidate_etag_cache()` is called by marketplace sync after refresh so the next request re-hashes against new on-disk content instead of waiting for TTL expiry. New explicit dependency: `cachetools>=5.3.0`.

### Fixed

- `/admin/access` group sidebar grant-count badges no longer revert to a stale value when switching between groups. The badge was reading `state.groups[i].grant_count`, a snapshot populated once at `/access-overview` load; toggling a grant only updated the DOM (via `refreshCounts`), not that field, so the next `renderGroups` call (triggered by `selectGroup`) would clobber the live count with the original snapshot. `renderGroups` now derives the count live from `state.grants`, the array that `toggleGrant`/`bulkSet` keep in sync. Server data was always correct — only the in-page badge drifted until refresh.
- `/catalog`, `/admin/tables`, and `/admin/permissions` pages now render the shared top header correctly. The pages include `_app_header.html` (which uses `.app-*` CSS classes) but were not linking `style-custom.css` where those classes are defined; only `dashboard.html` and `base.html` did. Without the stylesheet the nav links, dropdowns, and user menu rendered as unstyled inline text. Added the missing `<link>` to all three templates.
- `PATCH /api/admin/groups/{id}` on a system group now correctly accepts description-only updates while still rejecting renames. The endpoint guard previously short-circuited with `409 "System groups are immutable"` for any mutation, which contradicted the repository layer's narrowed contract (rename-only rejection) — a description-only payload like `{"description": "..."}` would hit the endpoint short-circuit and never reach the repo. The endpoint now 409s only when `payload.name` differs from the existing name; a no-op rename (same name in payload) is dropped from the update before reaching the repo.
- Google OAuth callback no longer wipes a user's `google_sync` group memberships on a transient Workspace API failure. `fetch_user_groups` is fail-soft and returns `[]` for both "no groups" and "API error" — the callback used to feed that empty list into `replace_google_sync_groups`, which deletes all `source='google_sync'` rows for the user and then inserts zero. A login during a transient Cloud Identity hiccup would silently drop every Workspace-synced membership the user had built up. Admin-added memberships (`source='admin'`) were already protected. The callback now skips `replace_google_sync_groups` when the fetch returns empty and logs "preserving existing memberships" instead. Trade-off: a user whose Workspace groups were genuinely cleared keeps stale memberships until the next non-empty sync — accepted until `fetch_user_groups` learns to distinguish empty-success from empty-failure.
- `docker-compose.host-mount.yml` now uses `o: bind,rbind` instead of `o: bind` for the `data` volume. With a plain bind, sub-mounts under `/data` on the host (e.g. the dual-disk layout where sdc is mounted on `/data/state`) are silently shadowed inside the container by an empty subdirectory on the parent disk. The container then writes `system.duckdb` and other state to the wrong disk; the dedicated state disk receives no writes and accumulates only the snapshot left by the migration script. Recursive bind propagates existing sub-mounts at container start, so the container sees the same filesystem the host does. Operators on dual-disk VMs need to copy the live DB from `/var/lib/docker/volumes/agnes_data/_data/state/` (sdb's empty subdir) onto `/data/state/` (sdc) **before** redeploying with the fix, or the next start will surface the stale snapshot.

### Changed

- **BREAKING** Marketplace endpoint (`/marketplace.zip`, `/marketplace.git/*`) no longer god-modes for Admin members. `src.marketplace_filter.resolve_allowed_plugins` now filters every caller — admins included — through `resource_grants`. Admins curate their own marketplace view by granting plugins to the Admin group (or any group they belong to). Existing installs where the only membership on Admin is the admin themselves will see an empty marketplace until grants are added in `/admin/access`. App-level authorization (`require_admin`, `can_access` for non-marketplace types) is unaffected — Admin is still god mode there.
- **BREAKING** RBAC redesigned around two layers: app-level access via the `Admin` user-group (god mode short-circuit) and resource-level access via a generic `(group, resource_type, resource_id)` grant model. The four-value `core.viewer/analyst/km_admin/admin` hierarchy with `implies` BFS expansion is gone — every protected endpoint now uses either `require_admin` or `require_resource_access(ResourceType.X, "{path}")` from the new `app.auth.access` module. Authorization is decided per-request via a single DB lookup; no session cache, no dual-path resolver, no `_hydrate_legacy_role` shim. See `docs/RBAC.md`.
- **BREAKING** `internal_roles`, `group_mappings`, `user_role_grants`, and `plugin_access` tables removed. Replaced by `user_group_members` (binds users to user_groups with a `source` enum: `admin` / `google_sync` / `system_seed`) and `resource_grants` (group → `(resource_type, resource_id)`). Schema v13; the migration backfills from v12 atomically — `users.groups` JSON is converted into `user_group_members` rows with `source='google_sync'`, `core.admin` grants become Admin-group memberships with `source='system_seed'`, and `plugin_access` rows become `resource_grants` of type `marketplace_plugin`. The `users.groups` JSON column is dropped; the deprecated `users.role` column is kept NULL as a legacy artifact.
- **BREAKING** Schema v14 — `user_group_members` and `resource_grants` now declare DuckDB foreign-key constraints on `group_id` (referencing `user_groups.id`). Cascade deletes can no longer leave orphaned member / grant rows pointing at a deleted group. Migration is RENAME → CREATE-with-FK → INSERT → DROP, wrapped in `BEGIN TRANSACTION` so a partial failure rolls back without leaving the DB at a half-applied schema. Forks that touched these tables outside the documented repository APIs need to verify the FK direction matches their writes.
- **BREAKING** Admin REST surface unified under `/api/admin/groups`, `/api/admin/groups/{id}/members`, `/api/admin/grants`, `/api/admin/resource-types`. `app.api.role_management` and `app.api.plugin_access` removed. The web UI route `/admin/role-mapping` and `/admin/plugin-access` are replaced by a single `/admin/access` page; the `_app_header.html` link is renamed to "Access".
- **BREAKING** CLI subcommands `da admin role *`, `da admin mapping *`, `da admin grant-role`, `da admin revoke-role`, `da admin effective-roles` removed. New subcommands: `da admin group {list,create,delete,members,add-member,remove-member}` and `da admin grant {list,create,delete,resource-types}`. `da admin set-role <user> admin` still works as a thin wrapper that toggles Admin-group membership.
- Module authors no longer call `register_internal_role(...)`. Resource types are an `app.resource_types.ResourceType` `StrEnum` paired with a `ResourceTypeSpec` registered in `RESOURCE_TYPES`; adding a new resource type means adding one enum member, one `list_blocks(conn)` projection delegate, and one spec entry — all in `app/resource_types.py`. The registry drives both `/api/admin/resource-types` and `/api/admin/access-overview`, so there's no second wiring step. No DB migration, no startup hook.
- Google OAuth callback writes Cloud Identity group memberships into `user_group_members` (source='google_sync') instead of `users.groups` JSON. Manual admin-added memberships (source='admin') survive subsequent logins.

### Removed

- `app/auth/role_resolver.py`, `app/api/role_management.py`, `app/api/plugin_access.py`.
- `src/repositories/internal_roles.py`, `src/repositories/group_mappings.py`, `src/repositories/user_role_grants.py`.
- `app/web/templates/admin_role_mapping.html`, `app/web/templates/admin_plugin_access.html`.
- `Role` enum + `has_role`, `is_admin`, `is_km_admin`, `is_analyst`, `_is_admin_user_dict`, `set_user_role`, `get_user_role` from `src/rbac.py`. Dataset-access helpers (`can_access_table`, `get_accessible_tables`, `has_dataset_access`) preserved.
- Test files: `test_role_resolver.py`, `test_api_role_management.py`, `test_admin_role_mapping_ui.py`, `test_cli_admin_role.py`, `test_schema_v9_migration.py`, `test_plugin_access_api.py`.

### Internal

- `src/db.py` schema bumped to v13. New helpers `_seed_system_groups` (idempotent Admin/Everyone seed, runs on every connect) and `_v12_to_v13_finalize` (one-shot backfill + DROP cascade) replace `_seed_core_roles` and `_backfill_users_role_to_grants`.
- `app.auth.access` is the new authorization vocabulary: `_user_group_ids`, `is_user_admin`, `can_access`, `require_admin`, `require_resource_access`. Lives in its own module to avoid the circular import that would happen if it sat in `app.auth.dependencies` (the dependency factory needs `get_current_user` from there).
- New `tests/helpers/auth.py::grant_admin(conn, user_id)` — adds a user to the Admin system group so `require_admin` resolves to True. Updated test fixtures across `test_admin_tokens_ui`, `test_password_flows`, `test_pat`, `test_api`, `test_api_complete`, `test_api_scripts`, `test_web_ui` to call it after `UserRepository.create(role="admin")`. The legacy `users.role` column alone is no longer the admin marker.
- Skipped at module level (rewrite required for v13): `test_admin_user_capabilities_ui` (asserts the gone v9 capabilities UI), `test_marketplace_server_zip` and `test_marketplace_server_git` (depend on the removed `PluginAccessRepository`).
- Skipped individually as v13 behavior changes: `TestScriptRBAC` in `test_security` (scripts are now any-signed-in-user, not analyst+), profile-page tests in `test_web_ui` that asserted `core.analyst` / `Direct grants` / `Effective roles` markers from the dropped role hierarchy.

### Added

- `/api/v2/{catalog,schema,sample,scan,scan/estimate}` — discovery + scoped fetch primitives for remote-mode tables. See `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md`.
- `da catalog`, `da schema`, `da describe`, `da fetch`, `da snapshot {list,refresh,drop,prune}`, `da disk-info` — CLI primitives backed by the v2 API.
- `cli/skills/agnes-data-querying.md` — Claude rails skill loaded for Agnes-flavored projects; covers discovery-first protocol, `da fetch` workflow, BigQuery SQL flavor cheat-sheet, and snapshot hygiene.
- `cli/skills/agnes-table-registration.md` — admin-side companion skill: when to register single vs. bulk-discover, source-side verification before registration, idempotence rules, update/delete via REST (no CLI today), and confirmation flow.
- `instance.yaml: api.scan.*` knobs — `max_limit`, `max_result_bytes`, `max_concurrent_per_user`, `max_daily_bytes_per_user`, `bq_cost_per_tb_usd`, `request_timeout_seconds`. All optional; defaults applied if absent.
- `instance.yaml: api.catalog_cache_ttl_seconds`, `api.schema_cache_ttl_seconds`, `api.sample_cache_ttl_seconds` — TTL knobs for server-side discovery caches.
- `instance.yaml: data_source.bigquery.legacy_wrap_views` — opt-in toggle to restore the pre-v2 behavior of exposing BigQuery VIEW/MATERIALIZED_VIEW tables as DuckDB master views in `analytics.duckdb`. Default `false`. Set `true` for one release cycle when migrating existing scripts (see **BREAKING** note below).
- `instance.yaml: data_source.bigquery.billing_project` — optional GCP project to bill BQ jobs to / submit jobs from. Defaults to `data_source.bigquery.project` for backwards compatibility. Set when the SA has `bigquery.data.*` on the data project but lacks `serviceusage.services.use` there (cross-project read pattern); otherwise `/api/v2/scan/estimate` and BQ-mode `/api/v2/scan` fail with 403.
- **BigQuery extractor detects table type** (BASE TABLE vs. VIEW / MATERIALIZED_VIEW) via `INFORMATION_SCHEMA.TABLES` using DuckDB's `bigquery_query()` table function. Emits the appropriate DuckDB view:
  - BASE TABLE → direct `bq."dataset"."table"` reference (queries hit BigQuery Storage Read API).
  - VIEW / MATERIALIZED_VIEW → `bigquery_query('project', 'SELECT * FROM \`dataset.table\`')` wrapper (queries hit BigQuery Jobs API, required for views).
- **GCE metadata-server authentication for BigQuery.** New `connectors/bigquery/auth.py` module (`get_metadata_token()` function) fetches OAuth access tokens from the GCE metadata server on GCE instances. No service-account key file required. Both the extractor (at sync time) and the orchestrator / read-side (at ATTACH time) fetch fresh tokens on every rebuild / readonly-conn open. Raises `BQMetadataAuthError` on failure (network or malformed metadata-server response).
- **SQL identifier-validation helper** in `src/sql_safe.py`. New functions `is_safe_identifier()` and `validate_identifier()` enforce safe character sets before f-stringing identifiers into SQL. BigQuery extractor and orchestrator `_attach_remote_extensions` both validate `dataset`, `source_table`, and view names before use, closing a SQL-injection surface if admin config is untrusted.
- **`/api/sync/manifest` response now includes `query_mode` and `source_type` per table**, joined from `table_registry`. Clients can branch on table semantics (remote vs. local, source type) without a second API call.
- **`da sync --json` output** now includes a `skipped_remote` list with IDs of `query_mode='remote'` tables that were skipped during sync (they're not downloaded locally; only queried via `/api/query`).
- **Schema v10** introduces `view_ownership` to detect cross-connector view-name collisions in the master analytics DB (issue #81 Group C). When two connectors register the same `_meta.table_name`, the orchestrator now refuses to silently overwrite the prior owner's view — it logs a `view_ownership collision` ERROR identifying both sources and the colliding name, and the second source's view is NOT created. Previously this was last-write-wins, which depended on directory iteration order and could change deployment-to-deployment. Operators resolve a collision by renaming `name` in `table_registry` on one side (registry-side aliasing — `source_table` stays unchanged, only the view name changes). The orchestrator pre-scans every connector's `_meta` at the start of each rebuild and releases stale ownerships immediately (when ALL pre-scans succeed; if any fail, reconcile is skipped to avoid silently stealing a transient-IO source's name), so a renamed table frees its name in the SAME rebuild that introduces the rename — no two-step waits needed. New module `src/repositories/view_ownership.py` exposes the repository.

### Changed

- **BREAKING:** BigQuery `VIEW` and `MATERIALIZED_VIEW` tables (i.e. `query_mode='remote'` tables whose underlying BQ object is a view) are no longer wrapped as DuckDB master views in `analytics.duckdb`. `da query --remote "SELECT * FROM <bq_view>"` no longer resolves the view name by default. Use `da fetch <table_id> --where ... --as <snapshot_name>` to materialize a local snapshot, or `da query --remote "SELECT ... FROM bigquery_query('<project>', '<inner BQ SQL>')"` for one-shot execution. To restore the previous behavior for a migration window, set `instance.yaml: data_source.bigquery.legacy_wrap_views: true`. BQ `BASE TABLE` entities are unaffected — their direct-ref master views remain.
- **`da sync` skips `query_mode='remote'` tables.** Previously they produced 404s on download attempts. Now the CLI prints a one-line stderr summary (`Skipping N remote-mode tables: a, b, c (and M more)`) and a separate summary line (`Skipped (remote-mode): N`) in the final output, distinct from existing `Skipped (unchanged): M` counts.

### Fixed

- **`/api/v2/scan` 500 on local-mode tables.** `arrow_table_to_ipc_bytes()` only handled `pa.Table`; DuckDB's local query path returns a `pa.RecordBatchReader`. Helper now accepts both. (Caught during dev-VM E2E.)
- **`/api/v2/schema/{table_id}` 500 on BigQuery tables.** `_fetch_bq_schema()` selected `description` from `INFORMATION_SCHEMA.COLUMNS`, which BigQuery doesn't expose there — column descriptions live in `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` for nested fields. Removed the column from the SELECT; descriptions default to empty string until a real source is wired. (Caught during dev-VM E2E.)
- **BigQuery views failed at first query when FastAPI / CLI reopened `analytics.duckdb`.** `SyncOrchestrator._attach_remote_extensions` fetches a fresh GCE-metadata access token and creates a `bigquery` DuckDB SECRET before ATTACH, but secrets are session-scoped and don't persist with the on-disk database. The mirror code in `src.db._reattach_remote_extensions` (called from `get_analytics_db_readonly()`) still ATTACHed BigQuery without auth, so the next query against `bq."dataset"."table"` failed. Fixed by adding the same three-branch structure to `src.db`: BigQuery → fetch metadata token → `CREATE OR REPLACE SECRET bq_secret_<alias> (TYPE bigquery, ACCESS_TOKEN '<token>')` → ATTACH; otherwise fall back to env-var-token / no-auth paths. Metadata-server failures log at ERROR and skip the source so other connectors still resolve.
- **`src/orchestrator.py::_attach_remote_extensions` was ineffective for BigQuery.** It filtered `_remote_attach` lookups by `table_schema=<source_name>`, but DuckDB lists an attached database with `table_catalog=<source_name>` (not `table_schema`), so the loop never executed and `_remote_attach` rows were silently ignored. Switched the filter to `table_catalog`, matching the corresponding query already in `src.db`.
- **BigQuery extractor `python -m connectors.bigquery.extractor` standalone CLI** now reads project ID from `data_source.bigquery.project` matching `instance.yaml.example`. Previously it looked for an undocumented top-level `bigquery.project_id` key and silently produced an empty string on miss, causing cryptic BigQuery API errors downstream. Now exits with code 2 + a clear `logger.error` when the key is missing.

### Internal

- Test pattern: BigQuery extractor is exercised with a dual-path strategy (BASE TABLE + VIEW detection) via `_CapturingProxy` SQL-capture wrappers. DuckDB's C-implemented `execute` attribute is read-only and can't be monkey-patched directly; the proxy wraps the connection and captures outgoing SQL before forwarding to the real DuckDB conn.
- Implementation plan: `docs/superpowers/plans/2026-04-27-bq-pipeline-views-and-metadata-auth.md` — subagent-driven development for Tasks 1-7 of this PR.

### Changed (issue #81 / #44 / #88 — security & OSS neutralization)

- **BREAKING (ops)**: Keboola extractor now exits with three distinct
  codes instead of two (issue #81 Group B / M14): `0` = full success,
  `1` = full failure, `2` = **partial** failure (some tables succeeded,
  some failed). Previously `exit(0)` fired even when 9 of 10 tables
  failed, masking partial failures from the sync API and any operator
  alerting hooked to non-zero exit codes. The sync API
  (`POST /api/sync/trigger`) now logs `PARTIAL FAILURE (exit 2)` as a
  data-quality alert (distinct from `FAILED (exit 1)`) and continues to
  the orchestrator rebuild step — successful tables from this run plus
  unchanged tables from previous runs stay queryable. Operators whose
  alerting treated any non-zero exit as a hard error must teach it that
  exit 2 is a partial-failure signal, not a deploy failure.
- **BREAKING (security)**: The entire Script API is now **admin-only** (issue #44).
  `GET /api/scripts`, `POST /api/scripts/deploy`, `POST /api/scripts/run`, and
  `POST /api/scripts/{id}/run` all require the admin role; previously the list
  endpoint was open to any authenticated user and deploy/run were analyst-accessible.
  Two reasons: (1) the AST + string-blocklist sandbox in `_execute_script` is
  defense-in-depth and known to be bypassable through introspection chains
  (`__class__.__base__.__subclasses__()`, `__globals__['__builtins__']`,
  `__mro__` traversal — the dunder pattern list was tightened in this PR but
  the policy is "the role gate is the trust boundary, not the blocklist");
  (2) gating only `/run` left a planted-script attack open — an analyst could
  deploy a malicious script and wait for an admin to run it. Operators who
  need scripted workflows for non-admin users should run them on the user's
  behalf or expose the relevant data via the read-only `/api/data` surface
  instead. **Migration for cron / scheduler PATs:** if a non-admin PAT is
  wired into a scheduler that hits `/api/scripts/{id}/run` or
  `/api/scripts/run`, the request now returns 403. Add the PAT user to the
  Admin group via `/admin/access` or
  `da admin group add-member Admin <pat-user-email>`. PATs themselves do not
  need re-issuing — group membership is read at request time.
- **BREAKING (ops)**: Generic ops scripts moved out of the customer-named
  `scripts/grpn/` directory into `scripts/ops/` as part of the OSS
  vendor-neutralization (issue #88):
  - `scripts/grpn/agnes-tls-rotate.sh` → `scripts/ops/agnes-tls-rotate.sh`
  - `scripts/grpn/agnes-auto-upgrade.sh` → `scripts/ops/agnes-auto-upgrade.sh`

  Downstream consumer infra repos that copy these scripts onto VMs (e.g. via
  their own `startup.sh`) must update the source path. The OSS-shipped
  `infra/modules/customer-instance/` Terraform module is unaffected — it
  embeds equivalent logic inline via heredoc and does not source-by-path
  from `scripts/`. Script behaviour and env vars are unchanged. Cross-refs
  in `README.md`, `CLAUDE.md`, `docs/DEPLOYMENT.md`, `Caddyfile`, and
  `docker-compose.yml` were updated.
- **OSS neutralization (wave 2 — code, tests, planning docs)**. Customer
  identifiers replaced with placeholders across the codebase to ready the
  repo for public release (issue #88):

  - **Code docstrings**: `connectors/openmetadata/{client,transformer,enricher}.py`,
    `src/catalog_export.py`, `scripts/duckdb_manager.py` — `prj-grp-…` →
    `my-bq-project` / `prj-example-1234`, `AIAgent.FoundryAI` →
    `AIAgent.MyAgent` (in docstrings) / `AIAgent.Example` (in test fixtures),
    `FoundryAIDataModel` → `AnalyticsDataModel`.
  - **Test fixtures** in `tests/test_openmetadata_enricher.py`,
    `tests/test_duckdb_manager.py`, `tests/test_catalog_export.py`,
    `tests/test_openmetadata_transformer.py` — same set of replacements,
    behaviour-preserving (157 tests still green).
  - **Terraform module** `infra/modules/customer-instance/variables.tf`:
    `customer_name` description rewritten in English, examples switched
    from `keboola, grpn` to `acme, example`.
  - **Workflow** `.github/workflows/keboola-deploy.yml`: comment "Groupon-side
    dev VMs" → generic "per-developer dev VMs".
  - **Caddyfile**: TLS-rotation cross-ref updated to `scripts/ops/…` and
    Keboola-specific aside removed.
  - **Auth docs** `docs/auth-groups.md` and the OAuth probe in
    `scripts/debug/probe_google_groups.py`: GCP project name `kids-ai-data-analysis`
    replaced with placeholder `acme-internal-prod`.
  - **Planning docs** under `docs/superpowers/plans/` and `…/specs/`: the
    five hackathon-era documents (`2026-04-21-deployment-log.md`,
    `…-multi-customer-deployment.md`, `…-issues-14-and-10.md`,
    `…-hackathon-dry-run.md`, the spec) had `34.77.94.14` / `34.77.102.61`
    replaced with `<dev-vm-ip>` / `<prod-vm-ip>`, `Groupon`/`GRPN`/`grpn`
    with `Acme`/`another-customer`, and `prj-grp-…` with `prj-example-…`.

### Fixed

- **BREAKING (security CRITICAL)**: Jira webhook handler is now
  fail-closed (issue #83). Previously, if `JIRA_WEBHOOK_SECRET` was
  unset, `_verify_signature` returned `True` and any unauthenticated
  POST to `/webhooks/jira` could trigger the full ingest pipeline. The
  handler now returns **503** when the secret is missing
  (operator-misconfiguration signal, distinct from 401 wrong-signature).
  Operators relying on the no-secret = accept-everything mode (don't —
  it was never documented) must set `JIRA_WEBHOOK_SECRET` before this
  merges.
- **Security (CRITICAL)**: Jira issue keys arriving via webhooks are now
  validated against the canonical `^[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}\Z` format
  (`[0-9]` not `\d` to refuse non-ASCII Unicode digits, `\Z` not `$` to
  refuse trailing newlines that `$` would tolerate)
  before any filesystem operation (issue #83). Previously, `issue_key` flowed
  unsanitized into `connectors/jira/service.py` (`save_issue`,
  `download_attachment`, `_handle_deletion`, `process_webhook_event`) and
  `connectors/jira/incremental_transform.py`, enabling path traversal
  (`../../etc/passwd` style writes outside the Jira data dir). New module
  `connectors/jira/validation.py` provides `is_valid_issue_key` (regex
  whitelist; underscore deliberately excluded — Atlassian rejects underscores
  in real project keys) and `safe_join_under` (`Path.resolve()` containment
  check). Both are enforced at every filesystem boundary, defense-in-depth.
- **Security (CRITICAL)**: `webhookEvent` (the second attacker-controlled field
  in Jira webhook payloads) was used as a filename component in
  `_log_webhook_event` without sanitization (issue #83 reviewer follow-up).
  A payload with `webhookEvent: "../../tmp/pwn"` could write a JSON dump
  outside `WEBHOOK_LOG_DIR`. The handler now strips everything that isn't
  `[A-Za-z0-9_-]` (dot deliberately excluded to defeat `..` survival),
  clips length to 64 chars, and routes the final filename through
  `safe_join_under`.
- **Security (CRITICAL)**: hardened the connector → orchestrator trust
  boundary on BOTH the rebuild path
  (`src/orchestrator.py::_attach_remote_extensions`) AND the read-only
  query path (`src/db.py::_reattach_remote_extensions`, called by
  `get_analytics_db_readonly()` on every request) — issue #81 Group A.
  Three fixes: (1) DuckDB extensions referenced by `_remote_attach` are
  matched against a hard allowlist (default: `keboola, bigquery`;
  override via `AGNES_REMOTE_ATTACH_EXTENSIONS`). Install path splits
  built-in (LOAD only) from community (`INSTALL FROM community; LOAD`
  on rebuild path; LOAD only on the read-only query path which must
  not touch the network). (2) `token_env` names are matched against a
  hard allowlist (default: `KBC_TOKEN`, `KBC_STORAGE_TOKEN`,
  `KEBOOLA_STORAGE_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`; override
  via `AGNES_REMOTE_ATTACH_TOKEN_ENVS`). Names must additionally match
  `^[A-Z][A-Z0-9_]{0,63}$`. A malicious connector cannot ask the
  orchestrator to read `JWT_SECRET_KEY` / `SESSION_SECRET` /
  `OPENAI_API_KEY` and exfiltrate them via `ATTACH ... TOKEN`.
  (3) The URL passed to `ATTACH` is now single-quote-escaped on both
  paths. Also fixed a `table_schema` vs `table_catalog` mismatch that
  silently no-op'd `_attach_remote_extensions` for every connector
  (the rebuild-path hardening would have been moot in production
  without this fix). New module `src/orchestrator_security.py`
  centralises the policy and exposes `log_effective_policy()`, called
  from app startup so an operator's typo in
  `AGNES_REMOTE_ATTACH_EXTENSIONS` (which **replaces** the default,
  not extends it — a setting of `httpfs` would silently lock out
  `keboola, bigquery`) is visible at boot rather than at the next
  failed attach. See
  `docs/superpowers/plans/2026-04-27-issue-81-trust-boundary.md`.
- **Security (MEDIUM)**: extractor-side identifier validation (issue
  #81 Group D / M15). The Keboola and BigQuery extractors interpolate
  `table_name`, `bucket` / `dataset`, and `source_table` from
  `table_registry` directly into `CREATE OR REPLACE VIEW`,
  `INSERT INTO _meta`, and `COPY ... TO` SQL. Anyone with write access
  to `table_registry` (admin, registry-write API) could inject SQL via
  these identifiers. New shared module `src/identifier_validation.py`
  exposes a strict `validate_identifier` (for our own view names —
  `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`, used for `table_name` so it matches
  the orchestrator's rebuild-time check and dashed names fail fast at
  extraction rather than being silently dropped at rebuild) and a
  relaxed `validate_quoted_identifier` (for upstream-typed names like
  Keboola `in.c-foo` / BigQuery `my-dataset`:
  `[a-zA-Z0-9_][a-zA-Z0-9_.\-]*`, refusing any character that could
  close a `"..."` identifier literal). The orchestrator's existing
  `_validate_identifier` was lifted into the new module so both layers
  share a single source of truth; both extractors skip-and-continue on
  unsafe rows (logged + counted in failure stats; the rest of the
  registry still processes).

### Removed

- Customer-specific manual-deploy helper `scripts/grpn/Makefile` and its
  README, plus the corresponding hackathon deploy log under
  `docs/superpowers/plans/2026-04-22-grpn-deploy-learnings.md`. These
  documented one operator's hand-rolled stopgap for an org-policy-blocked
  Terraform flow and do not belong in vendor-neutral OSS.
- `scripts/switch-dev-vm.sh` — hackathon-era helper hardcoded to a specific
  shared dev VM. Per-developer dev VMs are
  the supported pattern now; operators who need an equivalent should use
  `gcloud compute ssh <vm> --command "sed -i …/.env && sudo /usr/local/bin/agnes-auto-upgrade.sh"`
  with their own VM details.

### Internal

- Sandbox blocklist now flags introspection-chain dunders explicitly:
  `__subclasses__`, `__globals__`, `__class__`, `__base__`, `__bases__`,
  `__mro__`, `__dict__`, `__code__`, `__builtins__`. `__init__` and
  `__getattribute__` are intentionally **not** in the list — substring match
  would flag every legitimate `def __init__(self):`. The chain breaks at
  the next link anyway.
- New regression test `test_run_pwn_payload_blocked` parametrized over the
  exact PoC from issue #44 plus two equivalent variants (lambda+`__globals__`,
  `__mro__` traversal). If the dunder list is silently weakened in a future
  refactor, the test fails. New `test_*_requires_admin` tests parametrized
  over all three non-admin core roles (analyst, viewer, km_admin).
- `tests/conftest.py::seeded_app` extended with `viewer_token` and
  `km_admin_token` so role-gating tests cover all four core roles.

### Migrated

- **Schema bumped from v9 to v10**. Auto-migration applies on next start
  (creates the `view_ownership` table; data on disk is unaffected). The
  pre-migration snapshot machinery (added at v8→v9) covers v9→v10 too —
  if anything goes wrong during the migration, the snapshot at
  `<DATA_DIR>/state/system.duckdb.pre-migrate` lets you roll back.

---

## [0.11.5] — 2026-04-27

Follow-up release for PR #73: addresses four rounds of Devin AI review on the role-management-complete branch. No new public-API surface; the user-visible payoff is that v8→v9-migrated installations now work end-to-end (login flows, user list, admin nav, privilege revocation), and `make local-dev` startup is finally quiet.

### Fixed

- **Privilege retention after grant revocation via the new REST API** (Devin review #73). `_hydrate_legacy_role` previously short-circuited on a truthy `user.get("role")`. The role-management endpoints (`POST/DELETE /api/admin/users/{id}/role-grants`, plus the `changeCoreRole` UI flow) only mutate `user_role_grants` — they don't touch the legacy `users.role` column. After a downgrade-via-API, the stale legacy value would keep `user["role"] = "admin"` in memory; `_is_admin_user_dict` and the catalog/sync admin-bypass short-circuits then silently retained elevated table access even though `require_internal_role` correctly denied the API gates. Fix: always re-resolve from `user_role_grants` regardless of the legacy column, making the grants table the single source of truth on every authenticated request. Cost: one DB round-trip per request (same as the existing PAT-aware fallback).
- **Dev-bypass + OAuth callback dropped direct grants from the session cache** (Devin review #73). Both call sites passed `external_groups` only to `resolve_internal_roles`, never the user's id — so `user_role_grants` rows were resolved on the per-request DB-fallback path inside `require_internal_role` instead of the cache. Functionally correct, but every admin-gated request paid a DB round-trip and the dev-bypass log line read "resolved 0 internal role(s)" for an obviously-admin user, which was confusing during debugging. Fix: pass `user_id` so the cache reflects the union at sign-in.
- `GET /api/users` returned **HTTP 500** for any v8→v9-migrated installation. The migration NULL-s legacy `users.role` (kept as a deprecated artifact because DuckDB FK blocks DROP COLUMN), but `UserResponse.role` is a required `str` Pydantic field — every user listing failed validation. `/admin/users` showed only "Failed to load users" and the new `/admin/users/{id}` Detail link was unreachable. Fix: route every user dict returned by the API through `_hydrate_legacy_role` (same shim already used by `get_current_user`), which derives the legacy enum value from `user_role_grants` for migrated users. Also fixes a quieter dual of the same bug — `target["role"] == "admin"` short-circuits in `update_user`/`delete_user` would silently no-op on migrated admins, letting the operator demote/delete the last admin against the documented protection.
- **Scheduler log-noise**: every cron tick produced a `POST /auth/token 401 Unauthorized` access-log line because the scheduler's auto-fetch fallback was always broken — it called `/auth/token` with just an email, but the endpoint requires email + password. Fix: removed the auto-fetch path entirely. Operators set `SCHEDULER_API_TOKEN` (a long-lived PAT) in production; in `LOCAL_DEV_MODE` the dev-bypass auto-authenticates the un-tokenized request, so jobs continue to work.
- **HTTP 500 on `POST /auth/token` for v8-migrated users** (Devin review #73 round 3). `TokenResponse.role` is a required `str` Pydantic field, but the v8→v9 migration NULL-s the legacy `users.role` column for every existing user. The login endpoint passed the raw NULL through to Pydantic, raising `ValidationError` → 500. Same root cause produced semantically wrong (but non-crashing) JWTs from Google OAuth, password, and email-magic-link flows — they wrote `role: null` into the issued token; downstream `_hydrate_legacy_role` in `get_current_user` would correct the per-request view, but the token payload itself stayed misleading. Fix: hydrate inline in each login flow before reading `user["role"]` — `app/auth/router.py` (`POST /auth/token`), `app/auth/providers/google.py` (OAuth callback), `app/auth/providers/password.py` (5 flows: JSON login, web login, JSON setup, web reset, web setup), and `app/auth/providers/email.py` (centralized in `_consume_token`, covers both magic-link `/verify` endpoints). New regression class `TestAuthLoginFlowsPostMigration` in `tests/test_schema_v9_migration.py` pins both the no-crash and the correct-role contracts for all four legacy levels (viewer/analyst/km_admin/admin).
- **`docs/RBAC.md` documented an `implies=[…]` keyword on `register_internal_role()` that the function doesn't accept** (Devin review #73 round 3). A module author copying the example would hit `TypeError: got an unexpected keyword argument 'implies'` at import time. Reality: `implies` is currently seeded only for the `core.*` hierarchy via `_seed_core_roles` in `src/db.py` — the registry-side write path doesn't exist yet. Rewrote the *Implies hierarchy* and *Module-author workflow* sections to document what's actually supported in 0.11.4 and what a future change would need to add.
- **`_seed_core_roles` was advertised as a per-connect safety net but only ran during fresh installs and the v8→v9 migration** (Devin review #73 round 4). The docstring promised "called from `_ensure_schema` on every connect" so an accidental `DELETE FROM internal_roles WHERE key = 'core.admin'` (or a doc-tweak release that updated `_CORE_ROLES_SEED` without bumping the schema version) would self-heal on the next process start. In reality both call sites lived inside `if current < SCHEMA_VERSION:` — once the DB was on v9, the seed function never ran again, leaving any deletion permanent and any in-code `display_name`/`description`/`implies` change requiring a manual SQL deploy. Fix: added an unconditional tail call to `_seed_core_roles(conn)` at the bottom of `_ensure_schema`, gated only by `current <= SCHEMA_VERSION` so the future-version-rollback contract still holds. New regression class `TestSeedCoreRolesSafetyNet` in `tests/test_schema_v9_migration.py` pins all three contracts (deleted row re-seeds, mutated `display_name` re-syncs from code, `applied_at` doesn't churn on already-current DBs).
- **`make local-dev` startup spammed an `AuthlibDeprecationWarning` from upstream's own `_joserfc_helpers.py`** every time `app/auth/providers/google.py` triggered the `from authlib.integrations.starlette_client import OAuth` import chain. The warning is upstream-internal — authlib telling itself to migrate from `authlib.jose` to `joserfc` before its 2.0 cut — and isn't actionable on our side until either authlib ships the fix or we rewrite OAuth on top of `joserfc` directly. Filtered the specific warning class at the top of `app/main.py` (with a message-based fallback if the class moves in a future authlib release) so the warning no longer pollutes operator-facing stdout. Other `DeprecationWarning`s remain visible.

### Added

- **`/profile` now self-services every user's role situation.** Three new sections rendered server-side for *all* signed-in users (not just admins): *Effective roles* (the full resolver output as chip cloud — direct grants ∪ group-derived ∪ implies-expanded), *Direct grants* (rows in `user_role_grants` with source label: `auto-seed` from v8 backfill vs. `direct` admin grant), and *Roles via groups* (which Cloud Identity / dev group grants which role for the current user). Non-admins finally see *why* a particular feature is or isn't accessible without asking an admin to read the DB. Admins additionally see a deep-link to `/admin/users/{id}` for editing their own grants in place.
- **`/admin/role-mapping` group ID picker.** A new "Known groups" panel above the create-mapping form surfaces clickable chips of group IDs known to the system: the calling admin's own `session.google_groups` (with human-readable names + a "your group" tag) merged with distinct `external_group_id`s already used in existing mappings (tagged "already mapped"). Click a chip → fills the form's external-group-id input and focuses the role select. Empty-state copy points the operator at `LOCAL_DEV_GROUPS` / Google sign-in when the picker is empty, instead of leaving them to guess Cloud Identity opaque IDs from memory.

### Changed

- Renamed `docs/internal-roles.md` → **`docs/RBAC.md`**. Standard industry term, more discoverable for engineers grepping for "RBAC" in a new repo. Added Quickstart-by-role sections (operator / end-user / module author) and a step-by-step *Module-author workflow* with code examples for registering a key, gating endpoints, declaring implies hierarchies, and writing a contract test against the gate. Cross-references in code (`app/api/admin.py`, `tests/test_role_resolver.py`) updated. `CLAUDE.md` now points contributors at the new doc from the *Extensibility → RBAC* section. Historical CHANGELOG entries (`[0.11.3]` / `[0.11.4]` body) keep the original `internal-roles.md` filename — they describe what shipped at that version and aren't retro-edited.

---

## [0.11.4] — 2026-04-27

Role-management complete release. Sjednocuje legacy `users.role` enum (viewer/analyst/km_admin/admin) with the v8 internal-roles foundation under one model with implies hierarchy, ships admin UI + REST API + CLI for managing both group mappings and direct user grants, and wires `require_internal_role` for PAT-aware resolution so admin endpoints work uniformly across OAuth and headless callers.

### Added

- **Schema v9 — unified role model.** New `user_role_grants(user_id, internal_role_id, granted_by, source)` table for direct user→role assignments (complementary to `group_mappings` which assigns via Cloud Identity group). Two new columns on `internal_roles`: `implies` (JSON array of role keys this role transitively grants) and `is_core` (BOOL, distinguishes seeded core.* hierarchy from module-registered roles). Migration v8→v9 seeds four `core.*` rows (`core.viewer/analyst/km_admin/admin`) with the legacy hierarchy as `implies` (`core.admin → core.km_admin → core.analyst → core.viewer`), backfills one `user_role_grants` row per existing user mirroring their pre-v9 `users.role` value (`source='auto-seed'`), and NULLs the legacy column.
- **PAT-aware `require_internal_role`.** Two-path resolution: session cache first (OAuth flow), DB-backed `user_role_grants` fallback (PAT/headless flow). Admin CLI scripts now hit gated endpoints uniformly without an OAuth round-trip. The PAT-specific 403 message from 0.11.3 is removed — PAT now legitimately resolves through direct grants.
- **Implies expansion at resolve time.** New `expand_implies(role_keys, conn)` helper in `app.auth.role_resolver` does BFS over the `implies` graph; `resolve_internal_roles` calls it at the end so a single `core.admin` grant expands to the full four-level hierarchy automatically.
- **Dotted role-key namespace.** Regex extended to allow `core.admin`, `context_engineering.admin`, `corporate_memory.curator` style keys (max 64 chars, lower-snake-case segments separated by dots). The owner_module column should match the prefix before the first dot.
- **REST API for role management.** New router `app/api/role_management.py` under `/api/admin`: `GET/POST/DELETE` on `group-mappings`, `users/{id}/role-grants`, plus `GET internal-roles` and `GET users/{id}/effective-roles` (debug). All gated by `require_internal_role("core.admin")` — works for both OAuth admins (cookie) and admin PATs.
- **Admin UI `/admin/role-mapping`.** Browse internal roles, manage Cloud Identity group → role mappings (table view + create/delete forms). User detail page extended with three sections: *Core role* (single-select for `core.*`), *Additional capabilities* (multi-checkbox for module roles), *Effective roles* (debug view of direct + group-derived + expanded set).
- **`da admin` CLI subcommands.** `role list`, `role show <key>`, `mapping list/create/delete`, `grant-role <email> <key>`, `revoke-role <email> <key>`, `effective-roles <email>`. All run over PAT — use them in CI scripts to grant/revoke roles without going through the browser.

### Changed

- **BREAKING (semantics, not API).** `users.role` column NULL-ed during v8→v9 migration. Reads via `UserRepository.get_by_*` still return the column but the value is always NULL after upgrade — code reading `user["role"]` directly in business logic gets `None`. The legacy `Role` enum (`Role.VIEWER/ANALYST/KM_ADMIN/ADMIN`) and convenience helpers (`is_admin`, `has_role`, etc. in `src/rbac.py`) continue to work — they now read from `user_role_grants` via the resolver. Sweeping `user.get("role") == "admin"` checks were rewritten to the new helper. The column itself is preserved physically because DuckDB rejects DROP COLUMN while a FK references the table; physical drop is deferred to a future schema-rebuild migration.
- `require_role(Role.X)` and `require_admin` are now thin wrappers over `require_internal_role(f"core.{role}")`. Behavior identical for OAuth users (admin role from group_mappings); PAT users now succeed when they hold a direct `core.admin` grant.
- `UserRepository.create()` and `update()` mirror role changes into `user_role_grants` automatically (`_grant_core_role` helper); existing setup code keeps working without changes.
- `UserRepository.delete()` pre-deletes `user_role_grants` rows (DuckDB FK doesn't auto-cascade).
- `UserRepository.count_admins()` reads `user_role_grants ⨝ internal_roles WHERE key='core.admin'` — the legacy `users.role = 'admin'` count would always return 0 after backfill.
- `app/api/admin.py` module-level docstring documents the v9 pattern for module authors who want to add their own capability gates.
- `docs/internal-roles.md` rewritten to remove the v8 "no UI yet" caveat, document the implies hierarchy, the dual session/DB resolution pathway, and the dotted-namespace key convention.

### Removed

- `require_internal_role`'s session-only enforcement (the v8 *"This endpoint needs an interactive (OAuth) session — Bearer/PAT tokens do not carry session-resolved roles"* error message). PAT clients with a matching `user_role_grants` row now pass the gate uniformly.

### Internal

- New `UserRoleGrantsRepository` in `src/repositories/user_role_grants.py` mirrors the style of `GroupMappingsRepository` (list/get/create/delete + per-user / per-role indices).
- INFO-level audit log on grant + mapping mutations (action strings: `role_mapping.created/deleted`, `role_grant.created/deleted`, resource `mapping:<id>` / `grant:<id>`).
- "Last admin protection" on `DELETE /api/admin/users/{id}/role-grants/{grant_id}`: refuses to delete the final `core.admin` grant in the system (mirrors existing `count_admins` protection on user deletion / deactivation).

## [0.11.3] — 2026-04-26

Authorization-foundation release — adds the internal-roles layer between Cloud Identity groups and per-module capability checks. Schema v8 migration; no admin UI yet (follow-up).

### Added

- **Internal roles + group mapping (foundation).** Schema v8 adds two tables: `internal_roles` (app-defined capabilities like `context_admin`, `agent_operator`, registered by Agnes modules at import time) and `group_mappings` (many-to-many bindings of Cloud Identity group IDs to internal role keys, managed by admins). New `app.auth.role_resolver` module exposes `register_internal_role(...)` for module authors, `sync_registered_roles_to_db(...)` (run once at startup, idempotent), `resolve_internal_roles(external_groups, conn)` (called at sign-in, writes resolved keys into `session["internal_roles"]`), and a `require_internal_role("…")` FastAPI dependency factory for permission checks. Resolution runs at sign-in (Google OAuth callback + dev-bypass — populates on first request and whenever external groups change, mirroring the OAuth callback's always-write semantics). No DB hit per request. Refresh requires re-login, same semantics as `session.google_groups`. **No admin UI yet** — mapping rows must be created via the repository directly until the management UI ships in a follow-up. PAT/headless clients carry no session and therefore cannot pass `require_internal_role` gates by design — `require_internal_role` distinguishes "signed-in but missing role" from "no session at all" and surfaces a PAT-specific 403 detail in the second case so an API consumer hitting the wall sees what to fix. See `docs/internal-roles.md` → *PAT and headless requests*.

### Changed

- `docs/internal-roles.md` documents `Admin → Users → deactivate then reactivate` as the supported "force re-resolve now" lever for users you can't get to log out (long-lived sessions, automated clients) — invalidates the existing session and forces a fresh sign-in on the next request.

### Internal

- INFO-level audit log on every successful resolve (OAuth callback + dev-bypass) so a "wrong role" complaint is debuggable from the log alone — admin can correlate "user X claims they lost access" with the resolver output without replaying the request.
- Startup warning when `SESSION_SECRET` is shorter than 32 chars, matching the existing `JWT_SECRET_KEY` gate. Both HMAC surfaces sign trust-laden state (`session.internal_roles`, `session.google_groups`, JWTs) — keeping the two gates consistent so a weak secret gets surfaced at boot, not after a quiet downgrade.
- `_clear_registry_for_tests()` now refuses to run unless `TESTING=1` so a stray import path in production can't drop the registered capabilities.

## [0.11.2] — 2026-04-26

Dev-experience patch release — make `LOCAL_DEV_MODE` realistic enough to actually exercise group-aware code paths on `localhost`, and consolidate scattered dev-onboarding instructions into a single `docs/local-development.md`.

### Added

- **`LOCAL_DEV_GROUPS` env var** mocks `session.google_groups` for the auto-logged-in dev user when `LOCAL_DEV_MODE=1`. JSON array matching the production shape (`[{"id":"…","name":"…"}]`) so group-aware UI and access-control code paths can be exercised on `localhost` without a Google OAuth round-trip. Honored only under `LOCAL_DEV_MODE=1`. The startup banner reports the parsed group IDs (or warns loudly when the value is set but malformed), so a typo gets surfaced at boot rather than silently on the first authenticated request. Session injection mirrors the production OAuth callback's "always-write" semantics — including clearing stale groups when the operator unsets `LOCAL_DEV_GROUPS` mid-session. See `docs/auth-groups.md` → *Local-dev mock*.
- **`make local-dev` now seeds two default mocked groups** (`Local Dev Engineers` + `Local Dev Admins` on `example.com`) via `scripts/run-local-dev.sh`, so first-boot `/profile` is non-empty out of the box. Override with `LOCAL_DEV_GROUPS='[…]' make local-dev`; disable with `LOCAL_DEV_GROUPS= make local-dev`.
- **`docs/local-development.md`** — single onboarding doc for working on Agnes locally: TL;DR, what `LOCAL_DEV_MODE` actually bypasses, group mocking, what isn't mocked, and the security-rails reminder that dev mode must never reach a production deploy.

### Internal

- Fix nightly `docker-e2e` CI failures: refresh two stale assertions that had drifted from the live API. `tests/test_docker_full.py::test_app_returns_html_on_root` now expects the auth-aware `302 → /login` (root has redirected since the auth middleware landed); `tests/test_e2e_docker.py::TestDockerHealth::test_health_has_duckdb` now reads `services["duckdb_state"]` (current health-payload shape, already validated by `tests/test_api.py`). No application behavior change — these only ran in the scheduled nightly job, so the drift went unnoticed for several PRs.

## [0.11.1] — 2026-04-26

Patch release — hotfix the missed Caddy env passthrough that should have shipped with 0.11.0, plus codify changelog discipline so this kind of drift gets caught at PR review time next time.

### Fixed

- `docker-compose.yml` caddy service now passes `CADDY_TLS` through to the container (`- CADDY_TLS` bare-form passthrough). Without it the `Caddyfile` `{$CADDY_TLS:default}` substitution always falls back to cert-file mode regardless of what the operator wrote into `.env`, and Caddy crash-loops on Let's Encrypt / internal-CA deployments. Should have shipped with #52; first attempt was #55, accidentally closed before merging.

### Internal

- `CLAUDE.md` — non-negotiable changelog discipline: every PR touching user-visible behavior must update `CHANGELOG.md` under `## [Unreleased]` in the same PR.

## [0.11.0] — 2026-04-26

First tagged semver release. The `version = "2.x"` strings that appeared in earlier `pyproject.toml` snapshots were arbitrary placeholders from the initial scaffold and never reflected actual API maturity — resetting to pre-1.0 to signal that things may still shift.

### Added — Auth

- **Google Workspace groups on `/profile`.** OAuth callback fetches the signed-in user's group memberships via Cloud Identity (`searchTransitiveGroups` with the `security` label — see `docs/auth-groups.md` for the GCP setup checklist and the `security`-vs-`discussion_forum` gotcha). Profile link added to the user dropdown.
- **Password reset + invite flows** for web and admin (`/auth/password/reset`, `/admin/users/invite`).
- **Personal access tokens (PAT)** with separate `:typ=pat` JWT claim, per-token revoke, last-used IP tracking, "My tokens" + admin "All tokens" UI.
- **Email magic-link provider** (itsdangerous-signed token).
- **Optional `SEED_ADMIN_PASSWORD`** to pre-hash the seed admin (dev convenience).

### Added — Deploy

- **`keboola-deploy.yml` workflow.** Tag-triggered alternative to `release.yml` for shared dev VMs that want explicit "deploy when I tag" semantics. Publishes immutable `:keboola-deploy-<tag>` + floating `:keboola-deploy-latest` alias.
- **Caddy + Let's Encrypt + corporate-CA TLS.** `Caddyfile` parametrized via `$CADDY_TLS` env var so a single file serves three regimes: cert-file (corp PKI), Let's Encrypt auto-issue, Caddy-internal-CA. URL-driven cert rotation with self-signed fallback (`scripts/grpn/agnes-tls-rotate.sh`). `docker-compose.tls.yml` overlay closes host `:8000` when Caddy fronts.
- **`dev_instances` schema in `customer-instance` Terraform module** gains optional `tls_mode` + `domain` (mirrors `prod_instance`). `infra-v1.6.0` tag.
- **Optional Google OAuth credentials from Secret Manager.** Module reads `google-oauth-client-{id,secret}` at boot if present; graceful fallback so non-Google deployments aren't affected.
- **`LOCAL_DEV_MODE` + `make local-dev-up` / `local-dev-down`** for one-keystroke local stack with magic-link auth pre-wired.
- **Per-developer `dev-<prefix>-latest` GHCR alias** for branches matching `<prefix>/<branch>` — push-to-deploy on personal dev VMs.
- **`/setup` web wizard** for first-time instance setup, plus headless `POST /api/admin/configure` and `POST /api/admin/discover-and-register`.
- **Smoke-test job in CI** (Docker-in-CI after every release) + `scripts/smoke-test.sh` for post-deploy verification.

### Added — CLI

- **Wheel distribution** + auto-update check on startup.
- `--version` flag, `--dry-run` + `X/N` progress on `da sync`, durable sync (atomic writes + manifest hash + retry on transient errors).
- gzip on JSON/HTML responses (server-side).

### Added — Data

- **Remote query engine.** Two-phase BigQuery + DuckDB engine for tables too large to sync locally (`--register-bq` flag).
- **Business metrics.** Standardized `metric_definitions` table in DuckDB with starter pack importer (`da metrics import`).
- **`/api/health`** returns `version`, `channel`, `commit_sha`, `image_tag`, `schema_version`.
- **Custom connector mount support** (`connectors/custom/`).
- **OpenAPI snapshot test** for breaking-change detection.

### Added — Docs / tooling

- `docs/auth-groups.md`, `docs/DEPLOYMENT.md`, `docs/HACKATHON.md`, `docs/ONBOARDING.md` runbooks.
- `scripts/debug/probe_google_groups.py` — stdlib-only probe for diagnosing Cloud Identity API issues without a deploy cycle.
- Schema migration safety tests (idempotency, data preservation, snapshot).
- Pre-migration snapshot of `system.duckdb` before schema upgrades.
- Auto-generated JWT and session secrets with file persistence (`/data/state/.jwt_secret`).
- Startup banner logging version, channel, and schema version.

### Changed

- **BREAKING (deployment)** — Caddy compose profile renamed `production` → `tls`. Existing `docker compose --profile production up -d` invocations need to switch.
- **BREAKING (deployment)** — Default `Caddyfile` mode is now cert-file (`tls /certs/fullchain.pem /certs/privkey.pem`); for the previous Let's Encrypt auto-issue behaviour set `CADDY_TLS=tls <ops-email>` in `.env`. See `docs/auth-groups.md` and `Caddyfile` inline docs.
- Schema migration v5→v6→v7: adds `users.active`, `personal_access_tokens` table, `personal_access_tokens.last_used_ip`. Auto-applied at boot.
- Image-level `AGNES_VERSION` now sourced from `pyproject.toml` at build time (no more drift between `da --version` and the package metadata).
- **Vendor-agnostic OSS rule** codified in `CLAUDE.md` — customer-specific names, hostnames, project IDs belong in consumer infra repos, not in this OSS distribution.

### Fixed — Security

- Open-redirect guard for backslash in `safe_next_path`.
- `SessionMiddleware max_age=3600 + https_only` (was browser-session forever, plain-HTTP-OK).
- Timezone-aware datetimes in Keboola metadata cache.
- Atomic magic-link token consumption (closes double-use race under concurrent clicks).
- Bootstrap backdoor closed when passwordless seed admin exists.
- urllib3 1.26→2.6.3 (resolves 4 Dependabot security alerts).
- argon2-cffi adopted for password hashing.
- See [docs/security-audit-2026-04.md](docs/security-audit-2026-04.md) for the full audit (renamed from `docs/padak-security.md` in #94).

### Fixed — Other

- `uvicorn --proxy-headers --forwarded-allow-ips='*'` so OAuth callbacks resolve to https when behind a TLS terminator.
- `scripts/grpn/agnes-tls-rotate.sh` hardened: `--max-redirs 0` + `--proto '=https'` on cert fetch, post-fetch PEM validation (rejects HTML error pages from corp portals), `ulimit -c 0` to suppress coredumps that could leak the unencrypted privkey, POSIX-safe `${arr[@]+"${arr[@]}"}` array expansion.
- `scripts/tls-fetch.sh` — generic URL fetcher (`sm://`, `gs://`, `https://`, `file://`) with redirect refusal + PEM validation.
- `kbcstorage` moved to optional dep — unblocks urllib3 security updates; primary Keboola path now uses the DuckDB Keboola extension.
- Dependencies consolidated into `pyproject.toml` (no more `requirements.txt`).

### Internal

- Test suite expanded to 1357+ tests (4 layers — unit, integration, web smoke, journey).

[0.16.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.16.0
[0.15.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.15.0
[0.14.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.14.0
[0.13.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.13.0
[0.12.1]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.1
[0.12.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.0
[0.11.5]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.5
[0.11.4]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.4
[0.11.3]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.3
[0.11.2]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.2
[0.11.1]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.1
[0.11.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.0
