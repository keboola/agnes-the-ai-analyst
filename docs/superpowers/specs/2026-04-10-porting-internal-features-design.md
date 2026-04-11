# Porting Internal Features to OSS — Design Spec

**Date:** 2026-04-10
**Status:** Approved
**Approach:** Metric-First (A) — metriky → bootstrap → metadata writer

## Context

Comparison of `keboola/internal_ai_data_analyst` (private, Jan 2026) with the OSS version revealed three feature gaps worth porting. Many features initially thought missing (session collector, corporate memory, Jira SLA polling, CI/CD, telegram bot) already exist in OSS.

**Primary user:** Local Claude Code agent analyzing data. Web UI is secondary.

**What's being ported:**
1. Business metrics layer (20+ YAML metrics → DuckDB-backed framework + starter pack)
2. Analyst bootstrap flow (onboarding for analysts connecting to a remote instance)
3. Metadata writer (column descriptions + basetype push back to Keboola Storage API)

**What's NOT being ported:**
- macOS desktop app (narrow use-case, WebSocket gateway covers most needs)
- Linux user management (replaced by DuckDB RBAC)
- rsync distribution (replaced by FastAPI API)
- systemd services (replaced by Docker Compose)

---

## 1. Business Metrics in DuckDB

### 1.1 DuckDB Schema — `metric_definitions` table

New table in `system.duckdb`, added as part of schema migration v3→v4:

```sql
CREATE TABLE metric_definitions (
    id              VARCHAR PRIMARY KEY,     -- 'revenue/mrr'
    name            VARCHAR NOT NULL,        -- 'mrr'
    display_name    VARCHAR NOT NULL,        -- 'Monthly Recurring Revenue'
    category        VARCHAR NOT NULL,        -- 'revenue'
    description     TEXT,
    type            VARCHAR DEFAULT 'sum',   -- sum, count, ratio, comparison
    unit            VARCHAR,                 -- 'USD', 'percentage', 'count'
    grain           VARCHAR DEFAULT 'monthly', -- monthly, weekly, daily
    table_name      VARCHAR,                 -- primary table
    tables          VARCHAR[],               -- for JOIN metrics
    expression      VARCHAR,                 -- 'SUM(total_amount)'
    time_column     VARCHAR,                 -- 'order_date'
    dimensions      VARCHAR[],              -- ['channel', 'region']
    filters         VARCHAR[],              -- descriptive WHERE conditions
    synonyms        VARCHAR[],              -- for NL matching
    notes           VARCHAR[],              -- business rules
    sql             TEXT NOT NULL,           -- canonical SQL query
    sql_variants    JSON,                   -- {"by_channel": "SELECT ...", "by_region": "..."}
    validation      JSON,                   -- {"method": "...", "result": "..."}
    source          VARCHAR DEFAULT 'manual', -- 'yaml_import', 'manual', 'api'
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now()
);
```

### 1.2 Schema Versioning

Bump `SCHEMA_VERSION` from 3 to 4 in `src/db.py`. Implementation requires:

1. Define `_V3_TO_V4_MIGRATIONS` list with CREATE TABLE statements for `metric_definitions` and `column_metadata`
2. Extend the `_ensure_schema()` function's `if current < N` chain with `if current < 4: _apply(_V3_TO_V4_MIGRATIONS)`
3. After table creation, auto-import YAML metrics if `docs/metrics/*/*.yml` files exist

Follows the established pattern of `_V1_TO_V2_MIGRATIONS` and `_V2_TO_V3_MIGRATIONS`.

### 1.3 Repository (`src/repositories/metrics.py`)

Follows existing pattern from `table_registry.py`:

- `list(category=None)` → all metrics, optionally filtered
- `get(metric_id)` → single metric or None
- `create(**kwargs)` → insert metric
- `update(metric_id, **kwargs)` → update fields
- `delete(metric_id)` → remove metric
- `find_by_table(table_name)` → metrics referencing a table
- `find_by_synonym(term)` → NL matching for Claude Code agent
- `import_from_yaml(yaml_path)` → parse YAML, upsert into DuckDB, return count
- `export_to_yaml(output_dir)` → DuckDB → YAML files, return count

### 1.4 YAML as Seed/Import Format

YAML files in `docs/metrics/` serve as:
- **Starter pack** — 10-15 generic SaaS metrics shipped with the project
- **Import source** — `da metrics import docs/metrics/` loads into DuckDB
- **Export target** — `da metrics export` dumps DuckDB → YAML (sharing, backup, version control)
- **Migration** — on first run after upgrade: detect YAML without DuckDB records → auto-import

Format remains compatible with the internal repo (same fields as `total_revenue.yml`).

### 1.5 Migration Script (`scripts/migrate_metrics_to_duckdb.py`)

1. Scans `docs/metrics/*/*.yml` via glob
2. Parses YAML, maps fields to DuckDB columns using this mapping:

   | YAML field | DuckDB column | Notes |
   |---|---|---|
   | `name` | `name` | direct |
   | `display_name` | `display_name` | direct |
   | `category` | `category` | direct |
   | `table` | `table_name` | **renamed** — YAML uses `table`, DuckDB uses `table_name` |
   | `tables` | `tables` | direct (for JOIN metrics) |
   | `sql_by_*` | `sql_variants` | all `sql_by_X` keys collected into JSON dict `{"by_X": "..."}` |
   | all other fields | same name | direct mapping |

   The `id` is computed as `"{category}/{name}"`.

3. INSERT OR REPLACE into `metric_definitions`
4. Idempotent — safe to run repeatedly

Auto-runs during schema migration v3→v4 if YAML files exist. Also callable standalone: `python scripts/migrate_metrics_to_duckdb.py`

### 1.6 Metrics Index (`docs/metrics/metrics.yml`)

Master index for the YAML starter pack. After `da metrics import`, DuckDB becomes the source of truth. The YAML index is only used during import to define categories and discover files — it is NOT read at runtime.

```yaml
version: "2.0"
categories:
  - name: revenue
    folder: revenue/
    metrics: [total_revenue, mrr, arr, churn_rate]
  - name: product_usage
    folder: product_usage/
    metrics: [active_users, feature_adoption]
  - name: sales
    folder: sales/
    metrics: [new_customers, upsell_expansion, pipeline_value]
  - name: operations
    folder: operations/
    metrics: [support_resolution_time, infrastructure_cost]
```

### 1.7 Starter Pack Metrics (10-15 generic)

Ported and generalized from internal repo, adapted for generic SaaS data:

| Category | Metric | Internal source |
|---|---|---|
| **Revenue** | `total_revenue` (exists), `mrr`, `arr`, `churn_rate` | mrr.yml, new_arr.yml |
| **Product Usage** | `active_users`, `feature_adoption`, `usage_vs_limit` | usage_value.yml, usage_vs_limit.yml |
| **Sales** | `new_customers`, `upsell_expansion`, `pipeline_value` | upsell_expansion.yml, closed_won.yml |
| **Operations** | `support_resolution_time`, `infrastructure_cost` | resolution_time.yml, infra_cost.yml |

SQL queries are **generic templates** referencing typical tables (`orders`, `subscriptions`, `users`, `tickets`). Users adapt to their schema.

### 1.8 CLI Command `da metrics`

```
da metrics list [--category revenue]     # list from DuckDB
da metrics show revenue/mrr              # detail
da metrics import docs/metrics/          # YAML → DuckDB (single file or directory)
da metrics export [--dir ./export/]      # DuckDB → YAML
da metrics validate                      # verify consistency (tables exist?)
```

Note: `da metrics add --file metric.yml` imports a single YAML file. Interactive wizard deferred to a future iteration.

### 1.9 API Endpoints

```
GET  /api/metrics                              → list categories and metrics
GET  /api/metrics/{metric_id:path}             → metric detail (path param to handle slashes in ID)
POST /api/admin/metrics                        → create/update metric
DELETE /api/admin/metrics/{metric_id:path}     → delete metric
POST /api/admin/metrics/import                 → YAML upload → DuckDB
```

**Deprecation:** The existing `GET /api/catalog/metrics/{metric_path:path}` endpoint (serves raw YAML) will be deprecated. After migration, it redirects to the new DuckDB-backed `GET /api/metrics/{metric_id:path}` endpoint. Remove the old endpoint after one release cycle.

### 1.10 Profiler Integration

`src/profiler.py` has `load_metrics()` (line ~343) that reads YAML files directly. This must be refactored to read from DuckDB instead.

**Specific changes required:**
1. `load_metrics(metrics_yml_path)` at profiler lines ~1154 and ~1248 must be replaced with calls to `MetricRepository(conn).find_by_table()` or a new `get_table_map()` method
2. The DuckDB connection must be threaded through to `run_profiler()` and `profile_single_table()` entry points
3. The `metrics_map` dict structure (`{table_name: [metric_name, ...]}`) must remain the same for compatibility with the rest of the profiler
4. If DuckDB has no metrics yet (empty table), fall back gracefully to empty map (no error)

### 1.11 CLAUDE.md Instructions

Add section to CLAUDE.md:
> Before computing any business metric: `da metrics show {category}/{name}`, read the SQL and business rules, use the canonical SQL from the metric definition.

---

## 2. Analyst Bootstrap Flow

### 2.1 Two Bootstrap Modes

**Server-side** (already exists in `da setup`):
- `da setup init` → `bootstrap` → `test-connection` → `first-sync` → `verify`
- Sets up instance (instance.yaml, .env, Docker)
- No changes needed.

**Analyst-side** (new — equivalent of internal `bootstrap.yaml`):
- Analyst connects local Claude Code to a remote Agnes instance
- Downloads data, initializes DuckDB, sets up CLAUDE.md
- Uses API instead of SSH/rsync

### 2.2 Flow: `da analyst setup`

New top-level Typer command `da analyst` registered in `cli/main.py` (follows same pattern as existing `da sync`, `da admin`, etc.). Implemented in `cli/commands/analyst.py`.

Use `--force` flag to re-run from scratch (cleans up partial state).

```
Step 1: detect_existing_project
  → looks for ./CLAUDE.md with Agnes identifier string
  → if found: "Project already set up. Want to resync? (da sync)"
  → if not: continue
  → if --force: skip detection, clean up and re-run

Step 2: connect_to_instance
  → asks for instance URL (https://data.acme.com)
  → asks for credentials (email/password or OAuth token)
  → GET /api/health → verify availability
  → POST /auth/token → obtain JWT
  → store token in .env or ~/.agnes/credentials

Step 3: create_workspace
  → creates directory structure:
    ./data/parquet/          ← downloaded data
    ./data/duckdb/           ← local analytics.duckdb
    ./data/metadata/         ← profiles, schema
    ./user/artifacts/        ← analyst work output
    ./user/sessions/         ← Claude Code session logs
    ./.claude/               ← Claude Code config

Step 4: download_schema_and_metrics
  → GET /api/catalog/tables → list of available tables
  → GET /api/metrics → all metrics
  → saves as local JSON/YAML cache in data/metadata/

Step 5: download_data
  → for each table the user has access to:
    GET /api/data/{table_id}/download → parquet
  → Rich progress bar
  → on failure: logs which tables failed, continues with remaining
  → partial state is resumable (re-run skips already-downloaded parquets by checking file existence + size)

Step 6: initialize_duckdb
  → creates local analytics.duckdb
  → CREATE VIEW for each downloaded parquet
  → verify: SELECT count(*) from a few tables

Step 7: generate_claude_md
  → generates CLAUDE.md from template (see 2.3)
  → creates empty .claude/CLAUDE.local.md (matches existing sync.py _upload() path)
  → writes .claude/settings.json

Step 8: verify
  → runs test query
  → prints: "Setup complete. X tables, Y metrics, Z rows."
```

### 2.3 CLAUDE.md Template (`config/claude_md_template.txt`)

Generated template for analysts, adapted from internal repo:

```markdown
# {instance_name} — AI Data Analyst

## Rules
- Before computing any business metric: `da metrics show {category}/{name}`
- For current schema: read `data/metadata/schema.json`
- Do not use DESCRIBE/SHOW COLUMNS — read metadata files
- Save work output to `user/artifacts/`

## Metrics Workflow
1. `da metrics list` → identify relevant metric
2. `da metrics show revenue/mrr` → read SQL and rules
3. Use the SQL from the metric, adapt to the question

## Data Sync
- `da sync` → download current data from server
- Data refreshes every {sync_interval}

## Directory Structure
- `data/` — read-only (downloaded from server)
- `user/` — your workspace
- `.claude/CLAUDE.local.md` — your personal notes (never overwritten, uploaded on sync)
```

Placeholders `{instance_name}`, `{sync_interval}` substituted at generation time from instance config.

### 2.4 Returning-Session Detection

On every `da` CLI invocation:
- Check data age (`data/metadata/last_sync.json`)
- If >24h: suggest `da sync`
- If CLAUDE.md missing: suggest `da analyst setup`

### 2.5 Sync Command — Existing Capabilities

The existing `da sync` already supports these flags (no changes needed):
- `da sync --docs-only` — just metadata and metrics (already implemented)
- `da sync --upload-only` — uploads sessions + `.claude/CLAUDE.local.md` to server (already implemented)

No new flags required for the bootstrap flow.

---

## 3. Metadata Writer

### 3.1 DuckDB Schema — `column_metadata` table

New table in `system.duckdb` (part of v3→v4 migration alongside `metric_definitions`):

```sql
CREATE TABLE column_metadata (
    table_id        VARCHAR NOT NULL,        -- FK → table_registry.id
    column_name     VARCHAR NOT NULL,
    basetype        VARCHAR,                 -- STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP
    description     VARCHAR,
    confidence      VARCHAR DEFAULT 'manual', -- high, medium, low, manual
    source          VARCHAR DEFAULT 'manual', -- 'manual', 'ai_enrichment', 'keboola_import'
    updated_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (table_id, column_name)
);
```

### 3.2 Workflow (3 phases)

**Phase 1: Discover** — profiler or AI agent analyzes columns
```
da admin metadata discover [--table orders]
  → for each column without description:
    sample 500 rows → heuristics for basetype
    if Claude Code agent: generate descriptions
  → saves as "proposal" JSON to ./data/metadata/proposals/ directory
    (same format as internal repo: {project}_metadata_{YYYYMMDD_HHMMSS}.json)
```

**Phase 2: Review** — user reviews proposals
```
da admin metadata review proposals/sales_metadata_20260410.json
  → prints table: column | basetype | description | confidence
  → user can edit or confirm
```

**Phase 3: Apply** — write to DuckDB + optional push to Keboola
```
da admin metadata apply proposals/sales_metadata_20260410.json
  → INSERT/UPDATE into column_metadata in DuckDB
  → --push-to-source: if source_type=keboola, POST to Keboola Storage API
  → --dry-run: just show what would change
```

### 3.3 Push to Keboola Storage API

Ported from `apply_metadata.py`:
- Provider: `"ai-metadata-enrichment"`
- Keys: `KBC.datatype.basetype`, `KBC.description`
- Endpoint: `POST {stack_url}/v2/storage/tables/{table_id}/metadata`
- Token and stack_url from `config/instance.yaml` / env vars (not hardcoded JSON)

Only works for tables with `source_type = 'keboola'` in `table_registry`. For BigQuery/CSV/Jira, metadata is stored locally in DuckDB only.

### 3.4 API Endpoints

```
GET  /api/admin/metadata/{table_id}           → column metadata for table
POST /api/admin/metadata/{table_id}           → save metadata (JSON body)
POST /api/admin/metadata/{table_id}/push      → push to source system
```

### 3.5 Integration

- **Profiler**: `src/profiler.py` enriches `profiles.json` with `column_metadata` from DuckDB
- **Catalog API**: `GET /api/catalog` returns metadata alongside profiles
- **Claude Code agent**: reads metadata via `da admin metadata show {table}` or from `profiles.json`

---

## Implementation Summary

### New Files

| Component | Files |
|---|---|
| **Metrics** | `src/repositories/metrics.py`, `cli/commands/metrics.py`, `app/api/metrics.py`, `scripts/migrate_metrics_to_duckdb.py`, 10-15 YAML in `docs/metrics/` |
| **Bootstrap** | `cli/commands/analyst.py`, `config/claude_md_template.txt` |
| **Metadata** | `src/repositories/column_metadata.py`, `app/api/metadata.py` (metadata commands added as subcommands of `da admin`) |

### Modified Files

| File | Changes |
|---|---|
| `src/db.py` | SCHEMA_VERSION=4, `metric_definitions` + `column_metadata` tables, v3→v4 migration |
| `src/profiler.py` | Read metrics + column_metadata from DuckDB instead of YAML scan |
| `app/main.py` | Register metrics + metadata routers |
| `app/api/catalog.py` | Deprecate `GET /api/catalog/metrics/` → redirect to new metrics API |
| `cli/main.py` | Register `metrics` + `analyst` top-level Typer commands |
| `cli/commands/sync.py` | No changes needed (flags already exist) |
| `CLAUDE.md` | Metrics workflow instructions |

### Schema Migration v3→v4

Single migration creating both tables. Auto-imports existing YAML metrics if found. Idempotent.

### Implementation Order

1. Schema v4 + metrics (framework + starter pack + CLI + API)
2. Bootstrap flow (analyst setup + CLAUDE.md template)
3. Metadata writer (discover + apply + Keboola push)

### Test Coverage

Each component gets its own test file following existing patterns:
- `tests/test_metrics.py` — repository CRUD, YAML import/export, API endpoints
- `tests/test_analyst_bootstrap.py` — setup flow (mocked API calls)
- `tests/test_column_metadata.py` — repository CRUD, proposal format, Keboola push (mocked)
