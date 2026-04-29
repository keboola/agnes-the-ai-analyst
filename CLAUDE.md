# AI Data Analyst

Open-source data distribution platform for AI analytical systems. Extracts data from sources into DuckDB, serves via FastAPI, and distributes parquets to analysts who use Claude Code for local analysis.

## First-Time Setup

When a user opens this project for the first time, guide them through interactive setup:

### Step 1: Gather Information
Ask the user for:
1. Company domain (e.g., "acme.com") - used for Google OAuth
2. Data source type: keboola / bigquery / csv
3. Instance name (e.g., "Acme Data Analyst")

### Step 2: Generate Configuration
1. Copy `config/instance.yaml.example` to `config/instance.yaml`
2. Fill in values from Step 1
3. If Keboola: ask for Storage API token, stack URL, project ID
4. Create `.env` from `config/.env.template`

### Step 3: Register Tables
1. Use the FastAPI admin API (`POST /api/admin/tables/{id}`) or webapp UI to register tables
2. Tables are stored in DuckDB `table_registry` with source_type, bucket, source_table, query_mode
3. For migration from old format: `python scripts/migrate_registry_to_duckdb.py`

### Step 4: Docker Deployment
```bash
docker compose up          # Start app + scheduler
docker compose --profile full up  # Include telegram bot

# HTTPS mode — Caddy + corporate-CA certs at /data/state/certs
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
    --profile tls up -d
```

See `docs/DEPLOYMENT.md` → **TLS** for cert provisioning + `scripts/ops/agnes-tls-rotate.sh` (daily refetch from `TLS_FULLCHAIN_URL`, `SIGUSR1` reload on diff, no-op when unchanged). The infra repo's `startup.sh` installs this as a systemd timer automatically.

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
├── cli/                    # CLI tool (`da sync`, `da query`, `da admin`)
├── app/auth/               # Authentication (FastAPI-based providers)
├── services/               # Standalone services (scheduler, telegram_bot, ws_gateway, etc.)
├── server/                 # Legacy deployment infrastructure
├── scripts/                # Utility + migration scripts
├── config/                 # Configuration templates (instance.yaml.example)
├── docs/                   # Documentation + metric YAML definitions
└── tests/                  # Test suite (633 tests)
```

## Architecture: extract.duckdb Contract

Every data source produces the same output:
```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

### Remote table support (`_remote_attach`)

Extractors with remote/passthrough tables (query_mode='remote') include a `_remote_attach` table
in extract.duckdb so the orchestrator can re-ATTACH the external DuckDB extension at query time:

```sql
CREATE TABLE _remote_attach (
    alias     VARCHAR,  -- DuckDB alias used in views, e.g. 'kbc'
    extension VARCHAR,  -- Extension name, e.g. 'keboola'
    url       VARCHAR,  -- Connection URL
    token_env VARCHAR   -- Env-var name holding the auth token, OR empty for
                        -- extensions with built-in auth (e.g. BigQuery uses the
                        -- GCE metadata server — see `connectors/bigquery/auth.py`).
);
```

The orchestrator reads this table, installs/loads the extension, fetches the token
(via `token_env` lookup, or via the extension-specific auth path when `token_env=''`),
creates a session-scoped DuckDB SECRET when the extension requires one (BigQuery), and
ATTACHes the external source. Views referencing `kbc."bucket"."table"` then resolve correctly.
This mechanism is generic — any connector can plug in.

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
              ┌──────────┼──────────┐
              ▼          ▼          ▼
          FastAPI      CLI
          (serve)    (da sync)
```

Three source types:
- **Batch pull** (Keboola): DuckDB extension downloads to parquet, scheduled
- **Remote attach** (BigQuery): DuckDB BQ extension, no download, queries go to BQ
- **Real-time push** (Jira): Webhooks update parquets incrementally

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
pytest tests/ -v

# Trigger sync manually
curl -X POST http://localhost:8000/api/sync/trigger

# Docker
docker compose up
```

## Business Metrics

Standardized metric definitions live in DuckDB (`metric_definitions` table). Import starter pack:

```bash
da metrics import docs/metrics/
```

### For AI agents analyzing data:
Before computing any business metric, look up the canonical definition:
1. `da metrics list` — find the relevant metric
2. `da metrics show revenue/mrr` — read the SQL and business rules
3. Use the SQL from the metric definition, adapt to the specific question

Never invent metric calculations — always use the canonical definitions.

## Querying Agnes data — agent rails

When asked about ANY data in Agnes, follow this protocol.

### Discovery first

Before writing ANY query against a table, run:

    da catalog --json | jq <filter>     # know what's available
    da schema <table>                   # learn columns + types
    da describe <table> -n 5            # see real values for shape

NEVER write `SELECT * FROM <table>` blindly. For local-mode tables it's
wasteful; for remote-mode tables it can blow up at 225M rows.

### Choose the right tool

Tables in `da catalog` have a `query_mode`:

- **`local`**: data is on the laptop as parquet (synced via `da sync`).
  Query directly with `da query "SELECT … FROM <table>"`.

- **`remote`** (typically BigQuery): the parquet does NOT exist on the laptop.
  You MUST either:
  1. **`da fetch`** a filtered subset → query the local snapshot, OR
  2. **`da query --remote`** for one-shot server-side execution, OR
  3. **`da query --register-bq`** for hybrid joins (rarely needed).

### `da fetch` workflow (preferred for remote tables)

    # 1. estimate first
    da fetch web_sessions_example \
        --select event_date,country_code,session_id \
        --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
                 AND country_code = 'CZ'" \
        --estimate
    # → "estimated_scan_bytes: 4.2 GB, result: ~250k rows, 12 MB locally"

    # 2. if reasonable, fetch
    da fetch web_sessions_example ... --as cz_recent

    # 3. query the local snapshot
    da query "SELECT event_date, COUNT(*) FROM cz_recent GROUP BY 1 ORDER BY 1"

### Heuristics for `da fetch`

- ALWAYS list specific columns in `--select`. Avoid implicit SELECT *.
- ALWAYS include a `--where` for remote tables; otherwise add `--limit`.
- ALWAYS run `--estimate` first when:
  - You're not sure of the data shape
  - The table has `partition_by` or `clustered_by` set (per `da schema`)
  - The fetch could plausibly exceed 1 GB local bytes
- Reuse `da snapshot list` before fetching — if a snapshot covers your
  query already, skip the fetch.

### BigQuery SQL flavor for `--where`

For `source_type=bigquery` (per `da catalog`):

- Date literal: `DATE '2026-01-01'` (NOT `'2026-01-01'::date`)
- Timestamp literal: `TIMESTAMP '2026-01-01 00:00:00 UTC'`
- Now: `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`
- Date arithmetic: `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`
- Regex: `REGEXP_CONTAINS(col, r'pattern')` (raw string!)
- NULL: `col IS NOT NULL` (standard)
- Cast: `CAST(x AS INT64)` (NOT `INT`)

For `source_type=keboola` / `source_type=jira` (local), use DuckDB SQL flavor
in your `da query` calls — there's no `--where` on local since fetch is implicit.

### Snapshot hygiene

- Reuse snapshots across questions in the same conversation.
- Use descriptive names: `cz_recent`, `orders_q1_us`, `sessions_today`.
- Drop with `da snapshot drop <name>` when done with a topic.
- `da disk-info` to see total cache size.

### When NOT to use `da fetch`

- Single aggregate on remote table (`SELECT COUNT(*) FROM remote`):
  use `da query --remote "SELECT COUNT(*) FROM web_sessions_example"`.
  No materialization needed; cheap.
- Throwaway exploration with raw BQ syntax: `da query --remote`.
- Cross-table JOIN with both tables remote: combine `da fetch` for one
  side + `da query --remote` for the other; full cross-remote JOIN
  requires more thought (see #101 for design space).

## Marketplace Repositories

Admin-managed git repos cloned nightly to `${DATA_DIR}/marketplaces/<slug>/`
so FastAPI can read their contents from disk.

- Register via `/admin/marketplaces` (admin UI) or `POST /api/marketplaces`.
- Scheduler calls `POST /api/marketplaces/sync-all` (admin-only, authed via `SCHEDULER_API_TOKEN`) at `daily 03:00` UTC. Routing through HTTP keeps the app the sole writer to `system.duckdb` — the previous in-process call from the scheduler container raced the app's long-lived DB handle and 500-ed on `Could not set lock on file`.
- Manual re-sync from the UI ("Sync now") hits `POST /api/marketplaces/{id}/sync`.
- PATs for private repos persist to `${DATA_DIR}/state/.env_overlay` (chmod 600) as `AGNES_MARKETPLACE_<SLUG>_TOKEN`. DuckDB stores only the env-var name (`token_env`), never the secret.
- Registry lives in DuckDB table `marketplace_registry` (schema v9).
- After each successful sync, `src/marketplace.py` parses `.claude-plugin/marketplace.json`
  from the cloned repo and caches the plugin list in `marketplace_plugins`
  (keyed by `(marketplace_id, plugin_name)`).
- `src/marketplace.py` handles clone/fetch/reset with token redaction in any surfaced error message.

## Access control (v13)

Two layers, no role hierarchy. Full reference: [`docs/RBAC.md`](docs/RBAC.md).

- `user_groups` — named groups. Two seeded as `is_system=TRUE` at startup:
  `Admin` (god-mode short-circuit on every authorization check) and
  `Everyone` (auto-membership for every user).
- `user_group_members` — `(user_id, group_id, source)`. `source ∈
  {admin, google_sync, system_seed}` so each writer only manipulates its own
  rows; Google's nightly DELETE+INSERT does not clobber admin-added members.
- `resource_grants` — generic `(group, resource_type, resource_id)` triple.
  Replaces `plugin_access` from v12; the same shape now covers any future
  entity-scoped grant (datasets, knowledge categories, …).

Resource types are an `app.resource_types.ResourceType` `StrEnum` paired with
a `ResourceTypeSpec` registered in `RESOURCE_TYPES` — adding a new one is one
enum member, one `list_blocks(conn)` delegate (projects domain tables into the
`(block → items)` shape the /admin/access tree renders), and one spec entry.
No DB migration, no second wiring step. Endpoints gate with either
`require_admin` (app-level) or `require_resource_access(ResourceType.X,
"{path}")` (entity-level), both from `app.auth.access`.

Admin UI: `/admin/access`. CLI: `da admin group {list,create,delete,members,
add-member,remove-member}` and `da admin grant {list,create,delete}`.

## Claude Code marketplace endpoint

Agnes serves a single aggregated Claude Code marketplace over two channels,
both gated by PAT auth and filtered per caller:

- `GET /marketplace.zip` — deterministic ZIP download with `ETag` /
  `If-None-Match` (304 when content unchanged). Consumed by a client-side
  SessionStart hook.
- `GET /marketplace.git/*` — git smart-HTTP (dulwich via a2wsgi). Registered
  in Claude Code once, then Claude Code owns the clone/fetch cycle.

Auth: ZIP uses `Authorization: Bearer <PAT>`. Git uses HTTP Basic where the
password field carries the PAT (`https://x:<PAT>@host/marketplace.git/`) —
git CLI does not speak Bearer.

Content: filtered via `src.marketplace_filter.resolve_allowed_plugins` which
joins `resource_grants ↔ marketplace_plugins` (matching
`mp.marketplace_id || '/' || mp.name = rg.resource_id`) scoped to the
caller's `user_group_members`. Admin is treated as a regular group here —
no god-mode shortcut for the marketplace feed, so admins curate their own
view by granting plugins to the Admin group (or any group they belong to).
On-disk layout in the served ZIP / git tree uses a slug-prefixed directory
(`plugins/<slug>-<plugin>/`) so two marketplaces shipping a same-named
plugin don't overwrite each other's files. The synth marketplace.json's
`name` field, however, is the plugin's authoritative name from its own
`.claude-plugin/plugin.json` (with a fallback to the upstream
marketplace.json `name`) — Claude Code's `/plugin` UI resolves a loaded
plugin back to its catalog entry by `plugin.json` name, so the catalog
entry's `name` must match. Same-named plugins from two upstream
marketplaces therefore collide in the catalog by design; admin RBAC
(which grants survive the filter) decides which one wins, identical to
how Claude Code behaves when a user adds two upstream marketplaces with
overlapping plugin names directly. `/marketplace/info` exposes both
`name` and `prefixed_name` so operators can disambiguate.

Cache: content-addressed bare repos at `${DATA_DIR}/marketplaces/git-cache/`
keyed by sha256(filtered content). Two users with the same RBAC view share
one repo; content change → new repo next to the old one. No TTL / prune yet.

User registration inside Claude Code:

```
# ZIP channel (typically via a SessionStart hook that unpacks into ./marketplace/)
curl -H "Authorization: Bearer $AGNES_PAT" https://agnes.example.com/marketplace.zip

# Git channel — one-time registration
/plugin marketplace add https://x:$AGNES_PAT@agnes.example.com/marketplace.git/
```

## Hybrid Queries (BigQuery + Local)

For tables too large to sync locally, use hybrid queries that JOIN local data with on-demand BigQuery results:

```bash
da query --sql "SELECT o.*, t.views FROM orders o JOIN traffic t ON o.date = t.date" \
         --register-bq "traffic=SELECT date, SUM(views) as views FROM dataset.web WHERE date > '2026-01-01' GROUP BY 1"
```

The `--register-bq` flag executes a BigQuery subquery, loads the result into memory, and makes it available as a DuckDB view for the final SQL. Multiple `--register-bq` flags can be used for multiple BQ sources.

For complex SQL, use stdin mode:
```bash
echo '{"register_bq": {"traffic": "SELECT ..."}, "sql": "SELECT ..."}' | da query --stdin
```

## Extensibility

### Data Sources (extract.duckdb contract)
New connector = `connectors/<name>/extractor.py` producing `extract.duckdb + data/`.
Must create `_meta` table with columns: table_name, description, rows, size_bytes, extracted_at, query_mode.
Orchestrator ATTACHes it automatically.

### Authentication
Auth providers in `app/auth/` (FastAPI-based):
- **Google**: OAuth via Google (Workspace group memberships pulled at sign-in — see `docs/auth-groups.md` for the GCP setup checklist + the `security` label gotcha)
- **Email**: Email magic link (itsdangerous token)
- **Desktop**: JWT for API

### RBAC

See **[Access control (v13)](#access-control-v13)** above and [`docs/RBAC.md`](docs/RBAC.md) for the full reference. TL;DR for module authors: gate endpoints with `Depends(require_admin)` for app-level mutations or `Depends(require_resource_access(ResourceType.X, "{path}"))` for entity-scoped grants. Add a new resource type by extending the `ResourceType` `StrEnum` and registering a `ResourceTypeSpec` (with a `list_blocks` projection delegate) in `app/resource_types.py`.

## Release & deploy workflows

Three GitHub Actions workflows publish images to GHCR. They share `_test.yml` for the test stage. Operator-facing reference: `docs/release-process.md`.

### `release.yml` — main-only auto-build + per-branch manual

Runs on every push to `main` (auto) and on `workflow_dispatch` from any branch (manual). Image identity uses `${{ github.run_number }}` — no more CalVer claim-tag race, no more git-tag side-effects on the repo.

**Push to main publishes:**
- `:stable` — floating, the latest main build
- `:stable-<run-number>` — immutable, pinnable
- `:sha-<7>` — git-SHA-pinned debugging tag

**`workflow_dispatch -r <branch>` from non-main publishes:**
- `:dev` — floating, latest dev across all branches
- `:dev-<run-number>` — immutable
- `:dev-<branch-slug>` — per-branch immutable (e.g. `:dev-zs-foo` from `zs/foo`)
- `:dev-<prefix>-latest` — per-developer floating alias (e.g. `:dev-zs-latest` from any `zs/*`); skipped when prefix matches a Git Flow keyword (`feature`, `fix`, `hotfix`, etc.)
- `:sha-<7>`

Pushes to non-main branches do NOT auto-build. To deploy non-main code:

    gh workflow run release.yml -r <branch> --field reason="why"

Smoke-test on the freshly-built `:stable` runs `scripts/smoke-test.sh`. On smoke failure, the downstream `rollback-on-smoke-fail` job in `release.yml` calls `.github/workflows/rollback.yml` which re-tags `:stable` to `stable-<run_number-1>`, deprecates the failed image, and opens a tracking issue (label `bug,rollback`) — primary operator notification.

### `keboola-deploy.yml` — tag-triggered, explicit deploy

Runs only on `keboola-deploy-*` git tag pushes. Publishes:

- `:keboola-deploy-<git-tag-suffix>` — immutable
- `:keboola-deploy-latest` — floating alias

Operator workflow unchanged — see `docs/release-process.md`.

### `_test.yml` — reusable test stage

Called by `release.yml`, `keboola-deploy.yml`, and `ci.yml` via `uses: ./.github/workflows/_test.yml`. One copy of the install/lint/mypy/pytest recipe; default `pytest-args` is serial (no `-n auto`), `ci.yml` overrides to parallel.

### `release-drafter.yml` — rolling draft GitHub Release

Updates the draft release on every push to main (and PR sync). Categories derive from PR labels or conventional-commit title prefixes (`feat:` / `fix:` / `chore:` / `docs:` / etc.). When you push a `vX.Y.Z` git tag and publish the draft, that release's notes auto-fill from PR titles since the previous tag.

### `rollback.yml` — extracted auto-rollback

Callable via `workflow_call` (from `release.yml` smoke-fail) or `workflow_dispatch` (manual). Re-tags `:stable` to a previous build, deprecates the failing image, opens a tracking issue.

Manual trigger:

    gh workflow run rollback.yml -f failed_image_tag=stable-475
    # or with explicit target
    gh workflow run rollback.yml -f failed_image_tag=stable-475 -f target_image_tag=stable-470

### `prune-dev-tags.yml` — weekly housekeeping

Runs Sundays 04:00 UTC. Prunes legacy CalVer `dev-YYYY.MM.N` / `stable-YYYY.MM.N` git tags + matching GHCR image versions. Default retention: current month + previous month (`KEEP_MONTHS=1`). Manual trigger supports dry-run + `keep_months` override. The new short-form scheme (`stable-475`, `dev-475`) is NOT pruned by this job.

### Versioning

Package version is read from git tags via `setuptools_scm` (filtered to `v*` semver tags via `--match 'v*'`). To cut a release: push a `vX.Y.Z` tag, publish the Release Drafter draft on the Releases page. The next `:stable` build's `/api/version` reports `X.Y.Z`.

### Module versioning

The customer-instance Terraform module under `infra/modules/customer-instance/` is published as `infra-vMAJOR.MINOR.PATCH` git tags. Bump on any module-API change; downstream infra repos pin to the tag in their `source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.X.Y"`.

After merging a module change to `main`:

    git tag infra-vX.Y.Z origin/main
    git push origin infra-vX.Y.Z

### Replacing a VM after a startup-script change

Module sets `lifecycle { ignore_changes = [metadata_startup_script] }` on `google_compute_instance.vm` so normal `terraform apply` doesn't churn running VMs. To propagate a startup-script update, trigger the consumer's apply workflow manually with the VM resource address — typical workflow_dispatch input is `recreate_targets='module.agnes.google_compute_instance.vm["<vm-name>"]'`.

## Key Implementation Details

### DuckDB Schema (src/db.py)
- Schema v13 with auto-migration v1→…→v13 (v5 adds `users.active`, v6 adds `personal_access_tokens`, v7 adds `personal_access_tokens.last_used_ip`, v8/v9 added the legacy internal_roles/role-grants tables, v10 added `view_ownership` for cross-connector view-name collision detection (issue #81 Group C), v11 added marketplace_registry + marketplace_plugins + user_groups + plugin_access, v12 added users.groups JSON + user_groups.is_system, **v13 replaces internal_roles/group_mappings/user_role_grants/plugin_access with user_group_members + resource_grants and drops users.groups JSON** — see docs/RBAC.md and the GitHub Releases page for the v13-cut release)
- `table_registry`: id, name, source_type, bucket, source_table, query_mode, sync_schedule, etc.
- `sync_state`, `sync_history`: track extraction progress
- `users`, `dataset_permissions`, `audit_log`: auth + RBAC
- System DB at `{DATA_DIR}/state/system.duckdb`
- Analytics DB at `{DATA_DIR}/analytics/server.duckdb`

### SyncOrchestrator (src/orchestrator.py)
- `rebuild()`: scans extracts dir, ATTACHes all, creates master views, updates sync_state
- `rebuild_source(name)`: single source (used after Jira webhooks)
- Thread-safe via `_rebuild_lock`

### Connector Pattern
- **Keboola**: `connectors/keboola/extractor.py` uses DuckDB Keboola extension, fallback to `client.py`
- **BigQuery**: `connectors/bigquery/extractor.py` uses DuckDB BQ extension (remote-only, no download)
- **Jira**: `connectors/jira/webhook.py` → `incremental_transform.py` → `extract_init.py` updates `_meta`
- `connectors/keboola/client.py`: legacy Keboola Storage API wrapper (kept as fallback)

### Config Loading
1. `config/loader.py` loads `instance.yaml`
2. `app/instance_config.py` exposes `get_data_source_type()`, `get_value()`
3. Table config lives in DuckDB `table_registry` (not markdown files)

### Files NOT to modify (stable infrastructure)
- `connectors/jira/file_lock.py` - Advisory file locking
- `connectors/jira/transform.py` - Core Jira transform logic
- `services/ws_gateway/` - WebSocket notification gateway

## Vendor-agnostic OSS — no customer-specific content

This repo is the public OSS distribution. **Nothing customer-specific belongs in code, configuration defaults, comments, docs, commit messages, PR titles, or PR bodies.** That includes:

- Specific deployments or brands (private VM names, internal product brands, organization names that aren't already public sponsors).
- Cloud project IDs, internal hostnames, runbook paths from a particular install (`/opt/<deployment>`, `<host>.<internal-domain>`, `prj-<org>-…`, internal SA emails).
- Cross-references to private repos (`<private-org>/<private-repo>#NN`). Describe the integration in generic terms or link to public examples instead.

When you motivate a change, frame it abstractly ("behind a TLS-terminating reverse proxy", "in containerized deploys") rather than naming a specific operator. When you show examples, use placeholders (`example.com`, `<your-host>`, `<install-dir>`). When config has reasonable defaults pulled from one deployment's habits, generalize them or surface them as documented examples — not hard-coded assumptions.

Customer-specific automation, hostnames, and identities live in private infra repos that *consume* this OSS. The OSS describes capabilities, defaults, and configuration knobs — not how a specific operator wired them up.

## Release notes

Release notes live in **GitHub Releases** (https://github.com/keboola/agnes-the-ai-analyst/releases), not in a tracked file. The legacy `CHANGELOG.md` was retired — `git log` and the Releases page are the authoritative history.

**Per-PR contract:** the PR title is the release-note bullet. Write it so an operator reading a list of merged PRs since the last tag understands what changed and whether it affects them. Prefix breaking changes with `BREAKING:` so a tag-by-tag scan can pick them out. PR description carries the *why* and any migration steps; reviewers expect both.

**At release time:** push a `vX.Y.Z` tag, then `gh release create vX.Y.Z --generate-notes` (auto-fills from PR titles since the previous tag) and edit the draft to surface breaking changes / migration steps at the top before publishing. Mirror the prose structure of recent releases on the same repo.

## Git Commits & Pull Requests

- Keep commit messages clean and concise
- Do not include AI attribution in commits or PRs
- Before opening a PR, scan the diff and the PR body for the customer-specific tokens listed above (`grep -niE '<token1>|<token2>|...'`). If anything matches, generalize or remove it.
