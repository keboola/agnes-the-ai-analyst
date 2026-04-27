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

See `docs/DEPLOYMENT.md` → **TLS** for cert provisioning + `scripts/grpn/agnes-tls-rotate.sh` (daily refetch from `TLS_FULLCHAIN_URL`, `SIGUSR1` reload on diff, no-op when unchanged). The infra repo's `startup.sh` installs this as a systemd timer automatically.

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
    token_env VARCHAR   -- Env-var name holding the auth token (NOT the token itself)
);
```

The orchestrator reads this table, installs/loads the extension, reads the token from the
environment, and ATTACHes the external source. Views referencing `kbc."bucket"."table"` then
resolve correctly. This mechanism is generic — any connector can use it.

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

### RBAC (role-based access control)

Three-layer model: external Cloud Identity groups → admin-curated `group_mappings` → internal roles (`internal_roles` table) → resolved into `session["internal_roles"]` at sign-in OR fetched from `user_role_grants` per request for PAT/headless callers. `core.*` roles (viewer/analyst/km_admin/admin) carry the legacy hierarchy via `implies` JSON; module authors register their own keys (e.g. `corporate_memory.curator`) at import time and gate endpoints with `Depends(require_internal_role("<key>"))`.

**Contributors building a new module or capability — read [`docs/RBAC.md`](docs/RBAC.md) before adding endpoints.** It covers: picking a role key (naming convention, namespace), `register_internal_role` lifecycle, gating with `require_internal_role` vs. the `require_admin` / `require_role(Role.X)` thin wrappers, declaring implies hierarchies inside your module, the `_hydrate_legacy_role` shim that keeps `user["role"]` reads working, and the admin workflows (UI / CLI / REST) for binding groups and granting roles. Quickstart sections by audience: operator, end-user, module author.

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
- Schema v9 with auto-migration v1→…→v9 (v5 adds `users.active`, v6 adds `personal_access_tokens`, v7 adds `personal_access_tokens.last_used_ip`, v8 adds `internal_roles` + `group_mappings`, v9 adds `user_role_grants` + `internal_roles.implies/is_core` and seeds `core.*` hierarchy from legacy `users.role`)
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
