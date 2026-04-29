# Data Sources

## Overview

AI Data Analyst uses a connector system where each connector produces an `extract.duckdb` following a standard contract. The SyncOrchestrator auto-discovers and ATTACHes these into the master `analytics.duckdb`.

Configure the data source type in `config/instance.yaml`:

```yaml
data_source:
  type: "keboola"  # Options: keboola, bigquery
```

Table definitions are stored in the DuckDB `table_registry` table (not in config files). Register tables via the admin API, CLI, or web UI.

## Query Modes

Each table has a `query_mode` that determines how data is accessed:

- **`local`**: Data is downloaded to parquet files on the Agnes server. Suitable for tables that fit in local storage.
- **`remote`**: Data stays in the external source; DuckDB extension ATTACHes at query time. Suitable for large tables where only query results are transferred.

## Keboola Connector

Syncs tables from Keboola Storage API using the DuckDB Keboola extension.

### Requirements

- Keboola Storage API token with read access
- DuckDB Keboola extension (auto-installed)

### Configuration

In `.env`:
```
KEBOOLA_STORAGE_TOKEN=your-token-here
KEBOOLA_STACK_URL=https://connection.your-region.keboola.com
KEBOOLA_PROJECT_ID=12345
```

Or configure via the admin UI (`/admin/tables`) or CLI:
```bash
da admin register-table --source-type keboola --bucket "in.c-crm" --table "company" --query-mode local
```

### How it works

1. The extractor (`connectors/keboola/extractor.py`) uses the DuckDB Keboola extension to download data
2. Produces `extract.duckdb` with `_meta` table + parquet files in `/data/extracts/keboola/data/`
3. The SyncOrchestrator ATTACHes `extract.duckdb` into `analytics.duckdb` and creates views

### Identifier validation

All Keboola table names, bucket names, and source table identifiers are validated against `_SAFE_QUOTED_IDENTIFIER` regex before use. Invalid identifiers are skipped with error logging.

## BigQuery Connector

Queries BigQuery tables on-demand using the DuckDB BigQuery extension (remote attach).

### Requirements

- Google Cloud project with BigQuery access
- Application Default Credentials (ADC) configured

### Configuration

In `config/instance.yaml`:
```yaml
bigquery:
  project_id: "your-gcp-project"
```

Or via the admin UI or CLI:
```bash
da admin register-table --source-type bigquery --bucket "dataset" --table "table" --query-mode remote
```

### Authentication

Uses Application Default Credentials (ADC) — the standard Google auth fallback chain:
1. `GOOGLE_APPLICATION_CREDENTIALS` env var (service account key JSON)
2. gcloud user credentials (`gcloud auth application-default login`)
3. GCE metadata server (automatic on Compute Engine)

No explicit key file configuration needed — ADC handles it.

### How it works

1. The extractor (`connectors/bigquery/extractor.py`) creates `extract.duckdb` with remote views
2. `_remote_attach` table tells the orchestrator how to ATTACH the BigQuery extension at query time
3. Queries go directly to BigQuery — no data is downloaded to local storage
4. Identifier validation (`validate_identifier`, `validate_quoted_identifier`) protects against injection

### Hybrid Queries

For queries that JOIN local data with BigQuery results:

```bash
da query --sql "SELECT o.*, t.views FROM orders o JOIN traffic t ON o.date = t.date" \
         --register-bq "traffic=SELECT date, SUM(views) as views FROM dataset.web GROUP BY 1"
```

## Jira Connector

Real-time webhook-based connector that updates parquet files incrementally.

### How it works

1. Jira webhooks hit `/api/jira/webhook` endpoint
2. The connector (`connectors/jira/`) processes webhook events and updates parquet files
3. Produces `extract.duckdb` with `_meta` table + incremental parquet data

## Writing a Custom Connector

Create a new connector in `connectors/<name>/extractor.py` that produces the `extract.duckdb` contract:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

### Required: `_meta` table

```sql
CREATE TABLE _meta (
    table_name   VARCHAR,
    description  VARCHAR,
    rows         INTEGER,
    size_bytes   INTEGER,
    extracted_at TIMESTAMP,
    query_mode   VARCHAR   -- 'local' or 'remote'
);
```

### Optional: `_remote_attach` table (for remote sources)

```sql
CREATE TABLE _remote_attach (
    alias     VARCHAR,  -- DuckDB alias used in views
    extension VARCHAR,  -- Extension name
    url       VARCHAR,  -- Connection URL
    token_env VARCHAR   -- Env-var name holding the auth token (NOT the token itself)
);
```

### Identifier validation

Import shared validators from `src/identifier_validation.py`:

```python
from src.identifier_validation import validate_identifier, validate_quoted_identifier
```

Use `validate_identifier()` for strict names (alphanumeric + underscore) and `validate_quoted_identifier()` for names that may contain dots/hyphens (e.g., Keboola-style `in.c-crm.orders`).

The SyncOrchestrator auto-discovers connectors by scanning `/data/extracts/*/extract.duckdb` — no registration step needed beyond producing the correct output format.

See `connectors/keboola/` for a complete batch-pull reference implementation, or `connectors/bigquery/` for a remote-attach example.
