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

Source modes:
- **Batch pull** (Keboola, `query_mode='local'`): DuckDB extension downloads to parquet, scheduled
- **Remote attach** (BigQuery, `query_mode='remote'`): DuckDB BQ extension, no download, queries go to BQ
- **Materialized SQL** (BigQuery, `query_mode='materialized'`): scheduler runs admin-registered SQL through DuckDB BQ extension (via `BqAccess` from `connectors/bigquery/access.py`) and writes the result to `/data/extracts/bigquery/data/<id>.parquet`. Distributed via the same manifest + `da sync` flow as Keboola tables. Cost guardrail via `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; set `0` to disable — YAML `null` falls through to the default).
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

`da sync` is the canonical analyst-side distribution path: pulls the RBAC-filtered manifest from the server, downloads parquets whose MD5 changed (skipping `query_mode='remote'` rows), rebuilds local DuckDB views over them.

`da analyst setup` writes two hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → `da sync --quiet` — pulls fresh parquets at the start of every Claude Code session
- `SessionEnd`   → `da sync --upload-only --quiet` — uploads session jsonl + `CLAUDE.local.md` to the server

Both pass `--quiet` so they don't pollute Claude Code stdout, and trail with `|| true` so a server outage never blocks a session. Workspace-level (not user-home) so the hooks fire only when Claude Code opens this analyst workspace, not in unrelated sessions on the same machine.

Admin RBAC for auto-sync: `query_mode IN ('local', 'materialized')` plus a `resource_grants` row for one of the analyst's groups → table appears in their manifest → `da sync` downloads it. No per-user sync config; the admin layer is the single source of truth.

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
- Schema v19 with auto-migration v1→…→v19 (v5 adds `users.active`, v6 adds `personal_access_tokens`, v7 adds `personal_access_tokens.last_used_ip`, v8/v9 added the legacy internal_roles/role-grants tables, v10 added `view_ownership` for cross-connector view-name collision detection (issue #81 Group C), v11 added marketplace_registry + marketplace_plugins + user_groups + plugin_access, v12 added users.groups JSON + user_groups.is_system, **v13 replaces internal_roles/group_mappings/user_role_grants/plugin_access with user_group_members + resource_grants and drops users.groups JSON**, v14 adds FK constraints on user_group_members + resource_grants after orphan cleanup, v15 adds knowledge_items context-engineering columns + contradictions + session_extraction_state, v16 adds verification_evidence, v17 adds knowledge_item_relations, **v18 drops stranded non-google memberships from google-managed groups**, **v19 adds `source_query` TEXT to `table_registry` to back `query_mode='materialized'` (BigQuery scheduled-query parquet path)** — see CHANGELOG and docs/RBAC.md)
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

## Git Commits & Pull Requests

- Keep commit messages clean and concise
- Do not include AI attribution in commits or PRs
- Before opening a PR, scan the diff and the PR body for the customer-specific tokens listed above (`grep -niE '<token1>|<token2>|...'`). If anything matches, generalize or remove it.
