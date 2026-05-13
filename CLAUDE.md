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
1. Use the FastAPI admin API (`POST /api/admin/register-table`, then `PUT /api/admin/registry/{id}` for updates) or webapp UI to register tables
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
├── cli/                    # CLI tool (`agnes pull`, `agnes query`, `agnes admin`)
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
          (serve)    (agnes pull)
```

Source modes:
- **Batch pull** (Keboola, `query_mode='local'`): DuckDB extension downloads to parquet, scheduled
- **Remote attach** (BigQuery, `query_mode='remote'`): DuckDB BQ extension, no download, queries go to BQ
- **Materialized SQL** (BigQuery, `query_mode='materialized'`): scheduler runs admin-registered SQL through DuckDB BQ extension (via `BqAccess` from `connectors/bigquery/access.py`) and writes the result to `/data/extracts/bigquery/data/<id>.parquet`. Distributed via the same manifest + `agnes pull` flow as Keboola tables. Cost guardrail via `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; set `0` to disable — YAML `null` falls through to the default).
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

### Local sync & Claude Code hooks

`agnes pull` is the canonical analyst-side distribution path: pulls the RBAC-filtered manifest from the server, downloads parquets whose MD5 changed (skipping `query_mode='remote'` rows), rebuilds local DuckDB views over them. `agnes push` mirrors it for the upload direction (sessions, CLAUDE.local.md).

`agnes init` writes two hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → `agnes pull --quiet` — pulls fresh parquets at the start of every Claude Code session
- `SessionEnd`   → `agnes push --quiet` — uploads session jsonl + `CLAUDE.local.md` to the server

Both pass `--quiet` so they don't pollute Claude Code stdout, and trail with `|| true` so a server outage never blocks a session. Workspace-level (not user-home) so the hooks fire only when Claude Code opens this analyst workspace, not in unrelated sessions on the same machine.

Admin RBAC for auto-sync: `query_mode IN ('local', 'materialized')` plus a `resource_grants` row for one of the analyst's groups → table appears in their manifest → `agnes pull` downloads it. No per-user sync config; the admin layer is the single source of truth.

## Business Metrics

Standardized metric definitions live in DuckDB (`metric_definitions` table). Import starter pack:

```bash
agnes admin metrics import docs/metrics/
```

### For AI agents analyzing data:
Before computing any business metric, look up the canonical definition:
1. `agnes catalog --metrics` — find the relevant metric
2. `agnes catalog --metrics --show revenue/mrr` — read the SQL and business rules
3. Use the SQL from the metric definition, adapt to the specific question

Never invent metric calculations — always use the canonical definitions.

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

Admin UI: `/admin/access`. CLI: `agnes admin group {list,create,delete,members,
add-member,remove-member}` and `agnes admin grant {list,create,delete}`.

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

# Git channel — one-time registration. Two paths; pick the first that works.

# (a) Direct registration — preferred when it works.
/plugin marketplace add https://x:$AGNES_PAT@agnes.example.com/marketplace.git/

# (b) Two-step fallback — required when (a) fails. Bun-compiled `claude` on
#     macOS / Windows ignores the OS trust store and CA env vars on the
#     marketplace HTTPS path, so direct add can fail with TLS errors against
#     a private-CA Agnes instance even when system tools work fine. System
#     `git` honors GIT_SSL_CAINFO + the OS trust store, so cloning manually
#     and pointing Claude Code at the local clone sidesteps the Bun TLS path
#     entirely.
git clone https://x:$AGNES_PAT@agnes.example.com/marketplace.git/ ~/agnes-marketplace
claude plugin marketplace add ~/agnes-marketplace
# Optional hardening: strip the PAT from the cloned repo's origin so it
# doesn't sit in plaintext at ~/agnes-marketplace/.git/config — re-clone via
# the dashboard's setup flow when the PAT rotates.
git -C ~/agnes-marketplace remote set-url origin https://agnes.example.com/marketplace.git/
```

The dashboard-served setup payload (see `app/web/setup_instructions.py`) already
branches between (a) and (b) automatically based on platform when a private CA
is in play. The block above is the manual equivalent for users registering
outside that flow (e.g. operators bringing up a new instance, or
analysts whose first attempt failed and need to retry by hand).

## Hybrid Queries (BigQuery + Local)

Server-side only. Admins can POST `{sql, register_bq: {alias: bq_sql}}` to
`/api/query/hybrid` (see `app/api/query_hybrid.py`), which runs the BQ
sub-queries server-side (where BQ credentials live) and joins the result
against the server's local parquet views in a single DuckDB session.

There is no analyst-facing CLI flag for this — analysts who need to combine
a local table with a remote one should `agnes snapshot create` a filtered
subset of the remote table and `agnes query` the join locally, or run the
join server-side via `agnes query --remote`. The earlier `agnes query
--register-bq` flag ran in-process on the caller's machine and required
local BigQuery credentials that analysts don't have; it was removed.

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

Two separate release.yml-style workflows produce GHCR images. Pick the one that matches what you're shipping.

### `release.yml` — auto-build on every push
Runs on **every** push to **every** branch.
- Push to `main` → `:stable`, `:stable-YYYY.MM.N` (CalVer).
- Push to non-main `<prefix>/<branch>` → `:dev`, `:dev-YYYY.MM.N`, `:dev-<branch-slug>`, and (when prefix isn't a Git Flow convention) `:dev-<prefix>-latest` alias.

VMs that pin to a floating tag (`:dev`, `:dev-<prefix>-latest`) auto-upgrade within ~5 min via the cron in `agnes-auto-upgrade.sh`. Convenient for per-developer dev VMs; **footgun for shared dev VMs** (last pusher wins, regardless of who).

### `keboola-deploy.yml` — tag-triggered, explicit deploy only
Runs **only** on git tags matching `keboola-deploy-*`. Publishes:
- `:keboola-deploy-<git-tag-suffix>` — immutable, tied to the exact commit
- `:keboola-deploy-latest` — floating alias the consumer pins to

**Operator workflow:**
```bash
git checkout <commit-or-branch>
git tag keboola-deploy-<descriptive-name>
git push origin keboola-deploy-<descriptive-name>
# → workflow builds + publishes both tags
# → VM cron picks up :keboola-deploy-latest within ~5 min
# → manual cron trigger (skip the wait): sudo /usr/local/bin/agnes-auto-upgrade.sh on the VM
```

Use this when the consumer (e.g. a customer dev VM) needs **deploy-when-I-decide** semantics — no surprise rollouts from upstream branch pushes by other contributors. The infra repo pins `image_tag = "keboola-deploy-latest"` on the relevant VM.

### Module versioning
The customer-instance Terraform module under `infra/modules/customer-instance/` is published as `infra-vMAJOR.MINOR.PATCH` git tags (separate from app CalVer tags). Bump on any module-API change; downstream infra repos pin to the tag in their `source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.X.Y"`.

After merging a module change to `main`:
```bash
git tag infra-vX.Y.Z origin/main
git push origin infra-vX.Y.Z
```

### Replacing a VM after a startup-script change
Module sets `lifecycle { ignore_changes = [metadata_startup_script] }` on `google_compute_instance.vm` so normal `terraform apply` doesn't churn running VMs. To propagate a startup-script update, trigger the consumer's apply workflow manually with the VM resource address — typical workflow_dispatch input is `recreate_targets='module.agnes.google_compute_instance.vm["<vm-name>"]'`.

## Key Implementation Details

### DuckDB Schema (src/db.py)
- Schema v35 with auto-migration v1→…→v35 (v5 adds `users.active`, v6 adds `personal_access_tokens`, v7 adds `personal_access_tokens.last_used_ip`, v8/v9 added the legacy internal_roles/role-grants tables, v10 added `view_ownership` for cross-connector view-name collision detection (issue #81 Group C), v11 added marketplace_registry + marketplace_plugins + user_groups + plugin_access, v12 added users.groups JSON + user_groups.is_system, **v13 replaces internal_roles/group_mappings/user_role_grants/plugin_access with user_group_members + resource_grants and drops users.groups JSON**, v14 adds FK constraints on user_group_members + resource_grants after orphan cleanup, v15 adds knowledge_items context-engineering columns + contradictions + session_extraction_state, v16 adds verification_evidence, v17 adds knowledge_item_relations, v18 drops stranded non-google memberships from google-managed groups, **v19 drops legacy `dataset_permissions`, `access_requests` tables and `users.role`, `table_registry.is_public` columns — table access is now exclusively per-group via `resource_grants(resource_type='table')`**, **v20 adds `source_query` TEXT to `table_registry` to back `query_mode='materialized'` (BigQuery scheduled-query parquet path)**, **v21 adds `welcome_template` singleton table backing the Agent Setup Prompt admin override (`/admin/agent-prompt`)**, **v22 reserves the `setup_banner` table — feature dropped mid-development; table retained for forward compatibility with already-migrated instances**, **v23 adds `claude_md_template` singleton table backing the Agent Workspace Prompt admin override (`/admin/workspace-prompt`)**, **v24 rewrites materialized BQ `source_query` from DuckDB-flavor `bq."ds"."t"` to BQ-native `` `<project>.ds.t` `` so the new wrapping path accepts them; idempotent + warns when project unconfigured**, **v25 adds `store_entities` + `user_store_installs` + `user_plugin_optouts` backing the flea-market and my-stack views (now served at `/marketplace?tab=flea` + `/marketplace?tab=my`; the original standalone `/store` and `/my-ai-stack` page routes were dropped post-v25) — the served marketplace is now `(admin_granted ∖ opt_outs) ∪ store_installs`**, **v26 unifies Keboola `query_mode='local'` rows into `'materialized'` — the old local mode (DuckDB Keboola extension's COPY through QueryService) is replaced by the new Storage API export-async path which works regardless of project flags; existing `local` Keboola rows are flipped, NULL `source_query` means full-table export**, **v27 adds 7 columns to `table_registry` for Keboola per-table sync-strategy support: `incremental_window_days`, `max_history_days`, `incremental_column`, `where_filters`, `partition_by`, `partition_granularity`, `initial_load_chunk_days`. Layered on top of v26: admins can opt specific tables back to `query_mode='local'` (via the Direct extract Edit-modal radio) to enable the new dispatcher. The pre-existing `sync_strategy` column (default `'full_refresh'`) is reused — pre-v27 it was inert catalog metadata; post-v27 the Keboola extractor dispatches off it (`full_refresh` | `incremental` | `partitioned`). All new columns NULL on existing rows; meaningful only when paired with the matching strategy.**, **v28 introduces explicit-install (Model B) for curated marketplace plugins — served set flips from `(rbac ∖ user_plugin_optouts)` to `(rbac ∩ subscriptions)`. The `user_plugin_optouts` table+columns are reused (no DDL rename) so existing operator instances skip migration churn; row PRESENCE flips meaning from "excluded" to "subscribed", and the migration wipes existing rows so the inverted reading starts from a clean baseline. Also adds `marketplace_plugins.created_at` (per-plugin newest-first sort on /marketplace), backfilled from parent `marketplace_registry.registered_at` so existing plugins get a sensible date until the next sync overwrites with `CURRENT_TIMESTAMP`.**, **v29 adds `store_submissions` table backing flea-market upload guardrails (manifest + static-security + LLM-review verdicts) plus `store_entities.visibility_status` (`pending | approved | hidden`) — entity visible in flea browse only when `visibility_status='approved'`. Existing rows backfilled to `'approved'` so live flea content stays visible.**, **v30 adds `store_submissions.{file_size, bundle_sha256, bundle_purged_at}` so blocked-inline bundles persist for forensics + admin rescan/override (instead of the prior rmtree-on-reject); SHA256 survives the 30-day TTL purge, `bundle_purged_at` flips on at purge time so detail page can render "purged on YYYY-MM-DD"**, **v31 reshapes `store_submissions` (drops legacy unique on `entity_id` so multiple submissions per entity work — re-uploads/rescans land as new rows; idempotent table rebuild)**, **v32 adds `store_entities.{archived_at, archived_by}` columns plus broadens `visibility_status` enum to include `'archived'` for soft-delete; `DELETE /api/store/entities/{id}` is now soft (archive) by default, hard delete moves to `?hard=true` (admin-only)**, **v33 drops `store_submissions.retry_count` — counter mixed automatic LLM retries (capped) with admin-initiated rescans (unbounded), no useful semantics; admin Rescan button + audit_log carry the operational signal**, **v34 ensures `idx_store_submissions_entity` exists after the v33 column drop (DuckDB rebuilds the table sans index when dropping a column referenced by an index)**, **v35 broadens `store_entities.visibility_status` enum to include lifecycle value beyond `'archived'` already added in v32 — column-rebuild migration to register the new value with DuckDB's CHECK constraint, so `set_visibility('archived')` works against the constrained column. Also marks the architectural cutover from denormalizing `'archived'`/`'deleted'` onto `store_submissions.status` to LEFT-JOINing `store_entities` at query time: verdict (sub.status) becomes immutable forensic record, lifecycle (entity.visibility_status) becomes the live source of truth that the admin queue's Archived chip filters by.** — see CHANGELOG and docs/RBAC.md)
- `table_registry`: id, name, source_type, bucket, source_table, query_mode, sync_schedule, etc.
- `sync_state`, `sync_history`: track extraction progress
- `users`, `audit_log`: account state + audit trail. RBAC lives in `user_groups` + `user_group_members` + `resource_grants`.
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

## Changelog discipline — non-negotiable

**Every PR that adds, removes, or changes user-visible behavior MUST update `CHANGELOG.md` in the same PR.** No exceptions, no follow-ups, no "I'll do it after merge". User-visible = anything an operator, end-user, or downstream integrator can observe: CLI flags / output / exit codes, REST endpoints / payloads / status codes, web UI, `instance.yaml` schema, env vars, `extract.duckdb` contract, Docker / compose / Caddyfile knobs, default behaviors, breaking changes, security fixes.

**How:**
- Add a bullet under the topmost `## [Unreleased]` heading (create one if missing — it sits above the latest released version).
- Group by `### Added` / `### Changed` / `### Fixed` / `### Removed` / `### Internal` (Keep-a-Changelog sections).
- Mark breaking changes with `**BREAKING**` at the start of the bullet — operators grep for that string before bumping the pin.
- Reference the relevant doc/runbook if one exists (e.g. `see docs/auth-groups.md`), don't restate it.
- Internal-only changes (refactors, test additions, dependency bumps without behavior change) go under `### Internal` — still log them, just keep them terse.

**When you cut a release:**
- Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`.
- Append a new empty `## [Unreleased]` section at the top so the next PR has somewhere to land.
- Bump `version` in `pyproject.toml` to match `X.Y.Z`.
- Tag the merge commit as `vX.Y.Z` and push the tag.

**If you find yourself opening a PR without a CHANGELOG entry, stop and add one before requesting review.** Reviewers should bounce PRs that touch user-visible behavior without a changelog update — same way they'd bounce a PR with no test changes for new logic.

## Release-cut belongs to the PR — non-negotiable

**The version bump + CHANGELOG rename + new empty `[Unreleased]` are the LAST commit on the PR that earned the version. Never a standalone follow-up PR.**

When a PR lands the only `[Unreleased]` content (or is the last in a queue of in-flight feature PRs), the release-cut MUST ship as part of the same merge. Standalone release-cut PRs add review-overhead PRs to history with no behavior change of their own and pollute `git log` with the worst kind of churn — bookkeeping commits separated from the work that earned them.

**Mandatory checklist before approving / enabling auto-merge on ANY PR:**

1. **Stop.** Will this PR land alone in `[Unreleased]` (no other in-flight PRs queued behind it)?
2. **If yes**, the release-cut is REQUIRED in the same PR before merge. BEFORE pushing the final commit:
   - Bump `pyproject.toml` to `X.Y.Z`
   - Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`, add a new empty `## [Unreleased]` on top
   - Either squash these into the consolidation commit OR add as a separate `release: X.Y.Z` commit on the same branch
3. **THEN** push, approve, enable auto-merge.
4. After auto-merge fires: tag `vX.Y.Z` against the merge commit + create a GitHub Release. Done — one PR, one merge, one release.

**Failure mode to avoid:** enabling auto-merge on the feature PR thinking "I'll add the release-cut after." Auto-merge fires faster than the second commit lands. The window closes; the only fix is a standalone release-cut PR — exactly what this rule prohibits.

**Acceptable standalone release-cut** (rare): only when `[Unreleased]` accumulated bullets from MULTIPLE already-merged PRs AND no further behavior-change PR is queued — i.e. the cut is the only outstanding work and there's no PR to attach it to.

## Release workflow — concrete recipe

The rule above tells you WHAT to ship in a release-cut. This recipe tells you HOW to land one end-to-end without tripping on the operational quirks. Follow it linearly the first few times; once you've internalized the steps the order matters less, but the **non-obvious gotchas at the end** never go away.

### Happy path (8 steps)

```bash
# 1. Branch from a fresh checkout. iCloud Drive worktrees randomly hang
#    on git operations — use a fresh shallow clone in /tmp instead.
cd /tmp && git clone --depth 50 --branch main \
  https://github.com/keboola/agnes-the-ai-analyst.git agnes-<topic>
cd agnes-<topic> && git checkout -b zs/<branch-name>

# 2. Make the change + tests. Run the AREA pytest while iterating
#    (e.g. `pytest tests/test_X.py -p no:xdist -q`).

# 3. Add a CHANGELOG bullet under [Unreleased].
#    Group: Added | Changed | Fixed | Removed | Internal
#    Mark BREAKING with **BREAKING** prefix.

# 4. Commit the change(s). Multiple logical commits OK; release-cut
#    will be a SEPARATE last commit (next step). DO NOT bundle the
#    release-cut into the same commit as the change — it pollutes
#    the SHA that auto-close keywords reference and makes revert
#    targeted at the change-only difficult.

# 5. Run the full pytest suite locally:
#    `pytest tests/ -p no:xdist -q` (or `-n auto` if xdist works).
#    Pre-existing fails (e.g. test_readers_in_pre_init_dir under
#    subprocess timeout) are OK to ignore; verify by reverting your
#    diff and reproducing on bare main.

# 6. Release-cut commit (LAST commit on the PR per the rule above):
#    - Bump pyproject.toml: version = "X.Y.Z"
#    - Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`
#    - Add a fresh empty `## [Unreleased]` line above
#    Commit message: `release: X.Y.Z — <one-line summary>`

# 7. Push branch + open PR + enable auto-merge SQUASH:
#    git push -u origin HEAD
#    gh pr create --repo keboola/agnes-the-ai-analyst \
#      --head <branch> --title "<...>" --body "<...>"
#    gh pr merge <N> --repo keboola/agnes-the-ai-analyst \
#      --squash --auto --delete-branch

# 8. After auto-merge fires (poll or `Monitor`):
#    git fetch origin --tags
#    git tag vX.Y.Z <merge-sha>
#    git push origin vX.Y.Z
#    gh release create vX.Y.Z --repo keboola/agnes-the-ai-analyst \
#      --title "vX.Y.Z — <...>" --notes "<copy-paste from CHANGELOG>"
```

### Picking the next version

`pyproject.toml`'s current `version` is the **next-release target** (post-cut from the previous release). Pre-1.0 we patch-bump for everything that doesn't break operator-facing APIs:

- `instance.yaml` schema additions, new env vars, new endpoints → patch (e.g. 0.54.3 → 0.54.4)
- New CLI subcommands, BREAKING removals, schema migrations → still patch within the current 0.5x cycle (no minor bumps cut today)
- The CHANGELOG `**BREAKING**` marker is what operators grep for; the version number is secondary

Always check `git tag -l "v0.X*"` before naming — if `v0.54.0` is already tagged, the next one is `v0.54.1`, even if `pyproject.toml` still says `0.54.0` from a stale post-cut commit (we've shipped that race before).

### Authoring expectations on the PR

- **Self-PRs** (you're both author and reviewer): GitHub forbids self-approve. If branch protection requires N approving reviews (we don't today — `required_approving_review_count = 0`), you need someone else to approve. With our current 0-review setup, self-PRs can still merge automatically once required CI passes.
- **Other people's PRs you're taking over**: dismiss any prior CHANGES_REQUESTED reviews (yours or someone else's) before auto-merge can fire. `gh pr review <N> --approve --body "..."` after pushing your fixes.
- **Devin Review**: not a required check today; runs in parallel and posts a comment. Don't wait on it for merge unless the human reviewer explicitly asks.

### CI quirks you WILL hit

- **`gh pr checks` glosses CANCELLED as `fail`.** When you force-push (rebase, amend), GitHub auto-cancels the in-flight `Release` workflow run on the older SHA. Those cancelled jobs show up as "fail" in the PR's check summary and tab forever, even after newer runs succeed. **Look at the conclusion column, not just the count.** Rule of thumb: if the same check name appears with both `pass` and `fail` rows, the `fail` row is from an older auto-cancelled SHA. Verify with `gh api repos/keboola/agnes-the-ai-analyst/commits/<sha>/check-runs` — the raw API distinguishes `cancelled` from `failure` truthfully.
- **Branch protection's "strict" mode caches cancelled `test` as blocking** even after newer `test` runs succeed. Symptom: `mergeable_state: blocked` despite all required checks green on the latest SHA. Fix: re-run the cancelled `Release` workflow run (`gh run rerun <run-id>`); once its `test` job lands as success, the block clears. We've hit this on PRs #273, #281, #285, #286.
- **Required checks** (per branch protection): `test` + `docker-build` only. Other workflows (`cli-wheel-clean-install`, `build-and-push`, `Release`-pipeline, Devin Review) are advisory — green/red doesn't gate merge.
- **`enforce_admins: true`** in branch protection means `--admin` flag on `gh pr merge` does NOT bypass. Don't try; just fix the underlying block.

### Recovery when something derails

- **Force-pushed and lost auto-merge?** GitHub *usually* preserves auto-merge across force-pushes for the same PR; if it cleared, just re-run `gh pr merge <N> --squash --auto --delete-branch`.
- **Release-cut commit forgot to land?** That's the failure mode the "Release-cut belongs to the PR" rule prevents. If it happens anyway: open a follow-on PR with ONLY the release-cut commit, ship it, and write up why in your post-mortem comment.
- **Wrong version number tagged?** `git tag -d vX.Y.Z && git push --delete origin vX.Y.Z` then re-tag against the right SHA. Update the GitHub Release if you already created it.

## Issue economy — fix or close, don't spawn

**The default reaction to "I noticed something while doing X" is NOT "let me file an issue." The default is one of: fix it now, close as moot after audit, or leave a `TODO` in the touching diff.**

This codebase has accumulated issues that turn out to be:
- Already fixed in a different PR but the issue stayed open
- Stale (the code structure that motivated them is gone)
- Phantom (the symptom described doesn't actually fire on current main)
- Trivially fixable in 5 minutes inside the PR you're already in

Filing follow-up issues for these wastes everyone's attention. Issue count grows, signal-to-noise drops, real bugs sit alongside obsolete tickets, and the next person triaging asks "what's actually live here?" Quoting one observed pattern from this repo: a takeover-review PR found 3 "LOW hygiene items," filed them as a follow-up issue, then a week later the same author (me) closed the issue moot after a deeper audit showed the production callers already handled the problem correctly. Net contribution: 1 distracting issue + 2 round-trips of context-switching, zero behavior change.

### Mandatory checks BEFORE filing any follow-up issue

1. **Is the underlying claim still true on main?** Audit the code paths the issue describes. Issues from > 2 weeks ago routinely cite line numbers that have moved, function names that were renamed, and call sites that were deleted in unrelated work. If the underlying premise is gone → **close the parent, don't file a child**.
2. **Could you fix it in the PR you're already in (≤ 30 min, ≤ 1 file)?** If yes, just fix it. Bundle into a separate commit so the diff stays reviewable; the release-cut already gives you the version-bump vehicle. **Filing a follow-up "to keep this PR focused" is almost always wrong** — the focus argument trades 5 minutes of additional review now for an indefinite-future round-trip later.
3. **Is the fix a single-file change with obvious tests?** Same as #2 — fix it, don't file.
4. **If you're filing because the work needs design discussion** (interface choice, multi-file refactor, performance tradeoff) — fine, file with sufficient context that the next person can act without re-deriving. Include: code anchors with line numbers, exact reproduction steps, what you considered and rejected, and the design questions the next author needs to answer.

### Audit-first reflex when investigating an existing issue

Before writing ANY code on an open issue, **verify the symptom on current main**:

- Reproduce the bug locally (or in a fresh clone of main). If you can't reproduce, the issue is probably stale — close with comment explaining what you found.
- Grep for the cited line numbers / function names. If they've moved, the issue's code anchors are stale; either update them or close.
- Check git log + recent merges — the issue may already be fixed by a PR that landed after the issue was filed but didn't reference it.

When the audit shows the issue is moot, **close it with a closing comment that documents the audit** (what you grepped, what you checked, why the symptom doesn't fire today). Future readers seeing the closed issue need to know it was deliberately closed after audit, not abandoned.

### Patterns to avoid

- **"Found a small thing while reviewing — let me file an issue"** without checking whether it's a 5-minute fix you could do in this PR.
- **"Sub-agent flagged 3 LOW findings — let me file an issue"** without checking which of them are still valid post-audit.
- **"The issue says X is broken — let me build a fix"** without first verifying X is actually broken on current main.
- **"This deserves a follow-up issue"** when the residual is a single-line cleanup.
- **Filing two issues to close one** (e.g. closing #163 by filing #287 and #288 — net +1 open).

### Acceptable filing scenarios

- Multi-file refactor with design questions the current PR can't resolve.
- Production behavior change that needs operator coordination (e.g. requires a config rollout before code can be enabled).
- Cross-team work where ownership is unclear and the issue is the way to flag it.
- Bugs you can repro but the fix would balloon the current PR's scope ≥ 3×.

### Acceptable closing scenarios (close, don't fix)

- Audit shows the symptom doesn't fire on current main (phantom issue).
- Underlying code structure was deleted in unrelated work (stale issue).
- Resolved by a PR that didn't reference the issue number (close with link to the PR + commit).
- Original author indicates the requirement changed (idea-level issues).

When in doubt: **fix it, or close it**. Filing is the third choice, not the first.

## Run tests before every push — non-negotiable

**Before `git push`, run the full pytest suite locally.** CI runs the same command (`.github/workflows/ci.yml:29` → `pytest tests/ -v --tb=short -n auto`); a failure that surfaces in CI was discoverable in 90 seconds locally. Pushing first and watching CI fail wastes operator time, slows the PR, and trains everyone to ignore CI badges.

**Command (matches CI):**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

`-n auto` parallelizes across CPU cores; the suite runs in ~90s on a modern laptop. Local-only env (no `instance.yaml`, dev defaults) is fine — fixtures use `fresh_db` per-test isolation.

**When tests fail:**
- **Failures in code you touched** → fix before pushing. Update test expectations when the behavior change is intentional and documented (e.g. template restructure that changes assertion strings).
- **Failures unrelated to your diff** → confirm with `git stash && pytest <failing-test> && git stash pop`. If they reproduce on a clean branch, they are pre-existing — note them in the PR body but don't block your push on them. Don't silently skip; flag them so someone owns the fix.
- **Flaky tests** → re-run once. Two consecutive failures = real failure, fix or quarantine with a tracked issue.

**Smoke shortcuts (when full suite is too slow during iteration):**
- `pytest tests/ -k <pattern> -q` for area-scoped checks while iterating
- `pytest tests/test_X.py tests/test_Y.py -q` for the modules you touched

But the **full** `pytest tests/ -n auto` runs once before push. No exceptions.

If a CHANGELOG entry, doc edit, or pure-formatting commit genuinely doesn't touch any code path the tests exercise, you can skip the full run — but be honest with yourself about whether that's actually the case.

## Git Commits & Pull Requests

- Keep commit messages clean and concise
- Do not include AI attribution in commits or PRs
- Before opening a PR, scan the diff and the PR body for the customer-specific tokens listed above (`grep -niE '<token1>|<token2>|...'`). If anything matches, generalize or remove it.
