# AI Data Analyst

Open-source data distribution platform for AI analytical systems. Extracts data from sources into DuckDB, serves via FastAPI, and distributes parquets to analysts who use Claude Code for local analysis.

Full documentation index: [`docs/README.md`](docs/README.md).

## First-Time Setup

When a user opens this project for the first time, guide them through interactive setup. Ask for:

1. Company domain (e.g. `acme.com`) — used for Google OAuth
2. Data source type — `keboola` / `bigquery` / `csv`
3. Instance name (e.g. `Acme Data Analyst`)

Then: copy `config/instance.yaml.example` → `config/instance.yaml` and fill it in, copy `config/.env.template` → `.env` and add data-source credentials, and register tables via the admin API (`POST /api/admin/register-table`) or the web UI at `/admin/tables`.

Full step-by-step (local dev, Docker, TLS) lives in [`docs/QUICKSTART.md`](docs/QUICKSTART.md) and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). New-instance GCP deployment is [`docs/ONBOARDING.md`](docs/ONBOARDING.md).

## Project Structure

```
├── src/                    # Core engine
│   ├── db.py               # DuckDB schema (system.duckdb, analytics.duckdb)
│   ├── orchestrator.py     # SyncOrchestrator — ATTACHes extract.duckdb files
│   ├── repositories/       # DuckDB-backed CRUD (sync_state, table_registry, users, etc.)
│   ├── profiler.py         # Data profiling
│   └── catalog_export.py   # OpenMetadata catalog export
├── app/                    # FastAPI application
│   ├── main.py             # App setup, router registration
│   ├── api/                # REST API (sync, data, catalog, admin, auth)
│   └── web/                # HTML dashboard routes
├── connectors/             # Data source connectors (extract.duckdb contract)
│   ├── keboola/            # Keboola: extractor.py (DuckDB extension) + client.py (fallback)
│   ├── bigquery/           # BigQuery: extractor.py (remote-only via DuckDB BQ extension)
│   └── jira/               # Jira: webhook + incremental parquet → extract.duckdb
├── cli/                    # CLI tool (`agnes pull`, `agnes query`, `agnes admin`)
├── app/auth/               # Authentication (FastAPI-based providers)
├── services/               # Standalone services (scheduler, telegram_bot, ws_gateway, etc.)
├── server/                 # Legacy deployment infrastructure
├── scripts/                # Utility + migration scripts
├── config/                 # Configuration templates (instance.yaml.example)
├── docs/                   # Documentation + metric YAML definitions
└── tests/                  # Test suite
```

## Architecture: extract.duckdb Contract

Every data source produces the same output:
```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

The SyncOrchestrator scans `/data/extracts/*/extract.duckdb`, ATTACHes each into master `analytics.duckdb`, and creates views.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Keboola    │  │   BigQuery   │  │   Jira       │
│  extractor   │  │  extractor   │  │  webhooks    │
│ (DuckDB ext) │  │ (remote BQ)  │  │ (incremental)│
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
   extract.duckdb    extract.duckdb    extract.duckdb
   + data/*.parquet  (views → BQ)      + data/*.parquet
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
              SyncOrchestrator.rebuild()
              ATTACH → master views in analytics.duckdb
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
          FastAPI                  CLI
          (serve)               (agnes pull)
```

Source modes (per-table `query_mode`):
- **Batch pull** (Keboola, `local`): DuckDB extension downloads to parquet, scheduled.
- **Remote attach** (BigQuery, `remote`): DuckDB BQ extension, no download, queries go to BQ.
- **Materialized SQL** (`materialized`): scheduler runs admin-registered SQL through DuckDB and writes the result to a parquet under `/data/extracts/<source>/data/`. Distributed via the same manifest + `agnes pull` flow as local tables. BigQuery cost guardrail: `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; `0` disables).
- **Real-time push** (Jira): webhooks update parquets incrementally.

### Remote table support (`_remote_attach`)

Extractors with `query_mode='remote'` tables include a `_remote_attach` table in `extract.duckdb` (`alias`, `extension`, `url`, `token_env`) so the orchestrator can re-ATTACH the external DuckDB extension at query time — installing/loading the extension, fetching the token (via `token_env` lookup, or an extension-specific auth path when `token_env=''`, e.g. BigQuery's GCE metadata server), creating a session-scoped SECRET when required, and ATTACHing the source so views like `kbc."bucket"."table"` resolve. The mechanism is generic — any connector can plug in.

Deeper architecture notes: [`docs/architecture.md`](docs/architecture.md).

## Configuration

Instance-specific config: `config/instance.yaml` (see example).
Environment variables: `.env` (never committed).
Table definitions: DuckDB `table_registry` table in `system.duckdb`.

## Development

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
uv pip install ".[dev]"

# Run FastAPI locally
uvicorn app.main:app --reload

# Run tests
.venv/bin/pytest tests/ --tb=short -n auto -q

# Trigger sync manually
curl -X POST http://localhost:8000/api/sync/trigger

# Docker
docker compose up
```

### Local sync & Claude Code hooks

`agnes pull` is the canonical analyst-side distribution path: pulls the RBAC-filtered manifest from the server, downloads parquets whose MD5 changed (skipping `query_mode='remote'` rows), rebuilds local DuckDB views over them. `agnes push` mirrors it for the upload direction (sessions, CLAUDE.local.md).

`agnes init` writes two hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → `agnes pull --quiet` — pulls fresh parquets at the start of every Claude Code session
- `SessionEnd`   → `agnes push --quiet` — uploads session jsonl + `CLAUDE.local.md` to the server

Both pass `--quiet` so they don't pollute Claude Code stdout, and trail with `|| true` so a server outage never blocks a session. Workspace-level (not user-home) so the hooks fire only when Claude Code opens this analyst workspace.

Admin RBAC for auto-sync: `query_mode IN ('local', 'materialized')` plus a `resource_grants` row for one of the analyst's groups → table appears in their manifest → `agnes pull` downloads it. No per-user sync config; the admin layer is the single source of truth.

## Business Metrics

Standardized metric definitions live in DuckDB (`metric_definitions` table). Import the starter pack with `agnes admin metrics import docs/metrics/`.

**For AI agents analyzing data:** before computing any business metric, look up the canonical definition — `agnes catalog --metrics` to find it, `agnes catalog --metrics --show revenue/mrr` to read the SQL and business rules. Use that SQL, adapted to the question. Never invent metric calculations.

## Querying Agnes data — agent rails

When asked about ANY data in Agnes, follow this protocol.

### Discovery first

Before writing ANY query against a table, run:

    agnes catalog --json | jq <filter>     # know what's available
    agnes schema <table>                   # learn columns + types
    agnes describe <table> -n 5            # see real values for shape

NEVER write `SELECT * FROM <table>` blindly. For local-mode tables it's
wasteful; for remote-mode tables it can blow up at 225M rows.

### Choose the right tool

Tables in `agnes catalog` have a `query_mode`:

- **`local`**: data is on the laptop as parquet (synced via `agnes pull`).
  Query directly with `agnes query "SELECT … FROM <table>"`.

- **`remote`** (typically BigQuery): the parquet does NOT exist on the laptop.
  You MUST either:
  1. **`agnes snapshot create`** a filtered subset → query the local snapshot, OR
  2. **`agnes query --remote`** for one-shot server-side execution. Works on
     all `query_mode='remote'` rows regardless of upstream BQ entity type
     (BASE TABLE → Storage Read API with predicate pushdown; VIEW /
     MATERIALIZED_VIEW → BQ jobs API, no pushdown). Cost-guarded by a
     5 GiB scan cap (configurable in /admin/server-config). Direct
     `bq."<dataset>"."<table>"` paths are registry-gated — unregistered
     paths return 403 `bq_path_not_registered`.

### `agnes snapshot create` workflow (preferred for remote tables)

    # 1. estimate first
    agnes snapshot create web_sessions_example \
        --select event_date,country_code,session_id \
        --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
                 AND country_code = 'CZ'" \
        --estimate
    # → "estimated_scan_bytes: 4.2 GB, result: ~250k rows, 12 MB locally"

    # 2. if reasonable, fetch
    agnes snapshot create web_sessions_example ... --as cz_recent

    # 3. query the local snapshot
    agnes query "SELECT event_date, COUNT(*) FROM cz_recent GROUP BY 1 ORDER BY 1"

### Heuristics for `agnes snapshot create`

- ALWAYS list specific columns in `--select`. Avoid implicit SELECT *.
- ALWAYS include a `--where` for remote tables; otherwise add `--limit`.
- ALWAYS run `--estimate` first when:
  - You're not sure of the data shape
  - The table has `partition_by` or `clustered_by` set (per `agnes schema`)
  - The fetch could plausibly exceed 1 GB local bytes
- Reuse `agnes snapshot list` before fetching — if a snapshot covers your
  query already, skip the fetch.

### BigQuery SQL flavor for `--where`

For `source_type=bigquery` (per `agnes catalog`):

- Date literal: `DATE '2026-01-01'` (NOT `'2026-01-01'::date`)
- Timestamp literal: `TIMESTAMP '2026-01-01 00:00:00 UTC'`
- Now: `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`
- Date arithmetic: `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`
- Regex: `REGEXP_CONTAINS(col, r'pattern')` (raw string!)
- NULL: `col IS NOT NULL` (standard)
- Cast: `CAST(x AS INT64)` (NOT `INT`)

For `source_type=keboola` / `source_type=jira` (local), use DuckDB SQL flavor
in your `agnes query` calls — there's no `--where` on local since fetch is implicit.

### Snapshot hygiene

- Reuse snapshots across questions in the same conversation.
- Use descriptive names: `cz_recent`, `orders_q1_us`, `sessions_today`.
- Drop with `agnes snapshot drop <name>` when done with a topic.
- `agnes disk-info` to see total cache size.

### When NOT to use `agnes snapshot create`

- Single aggregate on remote BASE TABLE (`SELECT COUNT(*) FROM remote`):
  use `agnes query --remote "SELECT COUNT(*) FROM web_sessions_example"`.
  Storage Read API pushes the COUNT into BQ — cheap, no materialization.
- Single aggregate on remote VIEW/MATERIALIZED_VIEW: same syntax works
  (#160), but the BQ jobs API can't push WHERE/COUNT into the view body.
  Cost guardrail (default 5 GiB) catches expensive scans → 400
  `remote_scan_too_large` with `agnes snapshot create` suggestion. Pivot to
  `agnes snapshot create <id> --where '<predicate>'` if the cap is hit.
- Throwaway exploration: `agnes query --remote "SELECT … FROM <registered_id>"`.
  Direct `bq."<dataset>"."<table>"` paths are now registry-gated — register
  first or use the catalog id.
- Cross-table JOIN with both tables remote: combine `agnes snapshot create` for one
  side + `agnes query --remote` for the other; full cross-remote JOIN
  requires more thought (see #101 for design space).

## Hybrid Queries (BigQuery + Local)

Server-side only. Admins can POST `{sql, register_bq: {alias: bq_sql}}` to `/api/query/hybrid` (see `app/api/query_hybrid.py`), which runs the BQ sub-queries server-side (where BQ credentials live) and joins the result against the server's local parquet views in a single DuckDB session.

There is no analyst-facing CLI flag for this — analysts who need to combine a local table with a remote one should `agnes snapshot create` a filtered subset of the remote table and `agnes query` the join locally, or run the join server-side via `agnes query --remote`.

## Marketplace

Agnes ingests admin-registered Claude Code marketplaces (git repos cloned nightly to `${DATA_DIR}/marketplaces/<slug>/`) and re-serves a single aggregated, RBAC-filtered marketplace back to user instances over two PAT-gated channels: `GET /marketplace.zip` and `GET /marketplace.git/*`. Content is filtered per caller by joining `resource_grants ↔ marketplace_plugins` against the caller's groups.

Full reference — ingestion, the served endpoint, RBAC filtering, user registration inside Claude Code: [`docs/marketplace.md`](docs/marketplace.md). Content-authoring side (`marketplace-metadata.json`): [`docs/curated-marketplace-format.md`](docs/curated-marketplace-format.md).

## Access control

Two layers, no role hierarchy:

- `user_groups` — named groups. `Admin` (god-mode short-circuit on every authorization check) and `Everyone` (auto-membership) are seeded as `is_system=TRUE`.
- `user_group_members` — `(user_id, group_id, source)`; `source` segregates writers so Google's nightly sync doesn't clobber admin-added members.
- `resource_grants` — generic `(group, resource_type, resource_id)` triples for any entity-scoped grant.

Gate endpoints with `Depends(require_admin)` (app-level mutations) or `Depends(require_resource_access(ResourceType.X, "{path}"))` (entity-scoped), both from `app.auth.access`. Add a resource type by extending the `ResourceType` `StrEnum` and registering a `ResourceTypeSpec` (with a `list_blocks` projection delegate) in `app/resource_types.py` — no DB migration.

Admin UI: `/admin/access`. CLI: `agnes admin group …` and `agnes admin grant …`. Full reference: [`docs/RBAC.md`](docs/RBAC.md).

## Extensibility

### Data Sources (extract.duckdb contract)
New connector = `connectors/<name>/extractor.py` producing `extract.duckdb + data/`. Must create a `_meta` table with columns: `table_name`, `description`, `rows`, `size_bytes`, `extracted_at`, `query_mode`. The orchestrator ATTACHes it automatically.

### Authentication
Auth providers in `app/auth/` (FastAPI-based):
- **Google**: OAuth via Google (Workspace group memberships pulled at sign-in — see [`docs/auth-groups.md`](docs/auth-groups.md) for the GCP setup checklist + the `security` label gotcha)
- **Email**: magic link (itsdangerous token)
- **Desktop**: JWT for API

### Web pages
HTML dashboard pages use the design-system **page shell** (#367/#482): `{% extends "base_page.html" %}` (gradient hero + `{% block toolbar %}` + `{% block page %}`) or `{% extends "base_ds.html" %}` (everything else; body in `{% block content %}`). **Never `base.html`** — it is legacy. The base auto-imports the `ds.*` macros (no `{% import "_components.html" %}`), sets theme/favicon/nav/global-JS, and provides the canonical `.container`; page CSS goes in `{% block head_extra %}`, never inline in the body. Contract guards in `tests/test_design_system_contract.py` reject `.container:has()` opt-outs, bare `:root{}`, raw `#hex`, and `var(--primary)` (use `var(--ds-primary)`). Full step-by-step recipe: [`docs/architecture.md`](docs/architecture.md) → *Extending the Platform → New Web Page*.

## Key Implementation Details

### DuckDB Schema (src/db.py)
- Auto-migrating schema (`v1 → vN`). The current version and migration ladder live in `src/db.py`; per-version schema change notes are in `CHANGELOG.md` — do not maintain a duplicate history here.
- `table_registry`: id, name, source_type, bucket, source_table, query_mode, sync_schedule, etc.
- `sync_state`, `sync_history`: track extraction progress.
- `users`, `audit_log`: account state + audit trail. RBAC lives in `user_groups` + `user_group_members` + `resource_grants`.
- System DB at `{DATA_DIR}/state/system.duckdb`, analytics DB at `{DATA_DIR}/analytics/server.duckdb`.

### SyncOrchestrator (src/orchestrator.py)
- `rebuild()`: scans extracts dir, ATTACHes all, creates master views, updates sync_state.
- `rebuild_source(name)`: single source (used after Jira webhooks).
- Thread-safe via `_rebuild_lock`.

### Connector Pattern
- **Keboola**: `connectors/keboola/extractor.py` uses the DuckDB Keboola extension, falls back to `client.py` (legacy Storage API wrapper).
- **BigQuery**: `connectors/bigquery/extractor.py` uses the DuckDB BQ extension (remote-only, no download).
- **Jira**: `connectors/jira/webhook.py` → `incremental_transform.py` → `extract_init.py` updates `_meta`.

### Config Loading
1. `config/loader.py` loads `instance.yaml`.
2. `app/instance_config.py` exposes `get_data_source_type()`, `get_value()`.
3. Table config lives in DuckDB `table_registry` (not markdown files).

### Files NOT to modify (stable infrastructure)
- `connectors/jira/file_lock.py` — advisory file locking
- `services/ws_gateway/` — WebSocket notification gateway

(`connectors/jira/transform.py` was previously listed here but has been
removed: the `_remote_links` hardening in 0.54.19 required modifying
`transform_remote_links` and `transform_all` to honor a new "overlay
absent → preserve existing rows" contract. The transform module remains
sensitive — touch it only when you understand the JSON-overlay /
parquet-rewrite pipeline end-to-end — but it is no longer off-limits.)

## Release process

Full recipe, deploy workflows, manual rollback runbook, weekly tag-housekeeping, and CI quirks: [`docs/RELEASING.md`](docs/RELEASING.md). The non-negotiable rules:

- **Changelog discipline.** Every PR that changes user-visible behavior MUST add a bullet under `## [Unreleased]` in `CHANGELOG.md`, in the same PR — grouped Added/Changed/Fixed/Removed/Internal, `**BREAKING**` prefix for breaking changes. No follow-ups.
- **Release-cut belongs to the PR.** The version bump (`pyproject.toml`) + CHANGELOG rename + new empty `[Unreleased]` are the LAST commit on the PR that earned the version — never a standalone follow-up PR. If a PR lands the only `[Unreleased]` content, the release-cut ships in the same merge. After merge: tag `vX.Y.Z` on the merge commit + create the GitHub Release.
- **Run the full test suite before every push** — `.venv/bin/pytest tests/ --tb=short -n auto -q` (this is what CI runs). Failures in code you touched: fix before pushing. Failures unrelated to your diff: confirm with `git stash` they reproduce on a clean branch, note them in the PR body, don't block on them.
- **Watch the post-merge `release.yml` run.** On `main` pushes a `smoke-test` job pulls the just-built `:stable` image and runs a docker-compose stack; if it fails, the `rollback-on-smoke-fail` job calls the reusable `rollback.yml` workflow which re-points `:stable` to the previous known-good build and opens a tracking issue labeled `bug`. Success signal after merge = `smoke-test` green + `rollback-on-smoke-fail` skipped. If the rollback fires, the merge shipped a broken image to GHCR — investigate the tracking issue before any further push (the issue body has the failing image, commit SHA, deprecated tag, and rollback target). Manual rollback / forced target / weekly tag-pruning operator commands are in [`docs/RELEASING.md`](docs/RELEASING.md).

## Specialized agents and skills

Two committed locations carry Agnes-specific Claude Code behavior:

- `.claude/skills/agnes-*.md` — knowledge skills (`agnes-orchestrator`, `agnes-rbac`, `agnes-connectors`, `agnes-release-process`). Loaded into the main agent's context when their description matches the work, or invoked explicitly via `Skill(<name>)`. Read these before editing the corresponding part of the codebase.
- `.claude/agents/agnes-*.md` — specialist subagents (`agnes-reviewer-rules`, `agnes-reviewer-rbac`, `agnes-reviewer-architecture`, `agnes-releaser`). Spawned via the Agent tool at the end of PR work (reviewers, in parallel) or explicitly before/after merge (releaser).

Design rationale: `docs/superpowers/specs/2026-05-15-agnes-agents-design.md`.

## Project conventions

### Vendor-agnostic OSS — no customer-specific content
This repo is the public OSS distribution. **Nothing customer-specific belongs in code, config defaults, comments, docs, commit messages, or PR titles/bodies** — no specific deployments or brands, cloud project IDs, internal hostnames, runbook paths, internal SA emails, or cross-references to private repos. Frame motivations abstractly ("behind a TLS-terminating reverse proxy"); use placeholders in examples (`example.com`, `<your-host>`, `<install-dir>`). Customer-specific automation lives in the private infra repos that *consume* this OSS. Before opening a PR, scan the diff and PR body for customer-specific tokens.

### Issue economy — fix or close, don't spawn
The default reaction to "I noticed something while doing X" is **fix it now**, **close it as moot after audit**, or **leave a `TODO` in the touching diff** — not "file an issue". Before filing any follow-up issue: verify the claim is still true on current `main` (issues routinely cite moved line numbers and deleted call sites — if the premise is gone, close the parent), and check whether it's a ≤30-min, ≤1-file fix you could just do in the current PR. Filing is acceptable only for multi-file refactors with open design questions, production changes needing operator coordination, unclear cross-team ownership, or bugs whose fix would balloon the current PR ≥3×. When investigating an existing issue, reproduce the symptom on current `main` first; if it doesn't fire, close with a comment documenting the audit. When in doubt: fix it, or close it.

### Dual-backend discipline (DuckDB + Postgres parity)

DuckDB and Postgres are **both** first-class long-term backends for app-state, not a legacy/destination pair. Every feature must work on either engine, with cross-engine contract tests catching drift.

Non-negotiable rules:

- **Add a method to `src/repositories/X.py` (DuckDB)? Add the matching method to `src/repositories/X_pg.py` (PG) in the same PR.** No exceptions for "I'll do PG later". The DuckDB-bias drift in the codebase happens commit-by-commit; one PR with only `_pg.py` change is the canonical first step toward unmaintainable parity gaps.
- **Cross-engine contract tests must stay green.** `tests/db_pg/test_<cluster>_contract.py` parametrizes both backends through the same assertion set. If you add a method, extend the contract test in the same PR.
- **Alembic migration for PG? Matching `_vN_to_v(N+1)` step in `src/db.py` for DuckDB.** Both ladders must reach the same schema endpoint; `tests/test_db_schema_version.py` is the integration gate.
- **No PG-only optimizations without a DuckDB fallback path.** If a query has a PG-native window function, the DuckDB sibling either uses the same syntax (DuckDB ⊇ PG in most window-function support) or implements an equivalent in DuckDB's flavor.
- **DuckDB extensions (BQ, FTS, etc.) are not "DuckDB legacy".** They live next to the PG repos; analytics and state both ride DuckDB where appropriate.

When DuckDB-Quack matures (DuckDB 2.0, ~fall 2026), it becomes a fourth backend state (`duckdb_quack`); the dual-repo pattern absorbs it via the same factory layer. The state machine in `src/db_state_machine.py` already reserves the enum value.

The framing of *"DuckDB only for analytics, Postgres for state"* (from the PR #388 era) is **explicitly retired** — both backends are valid for state, and the platform supports multi-destination transitions in either direction.

### Git commits & pull requests
- Keep commit messages clean and concise.
- Do not include AI attribution in commits or PRs.
