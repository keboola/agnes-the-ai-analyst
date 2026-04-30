# Connectors — How to add a new data source

## Existing Connectors
- **Keboola** (`connectors/keboola/extractor.py`) — DuckDB Keboola extension, batch pull
- **BigQuery** (`connectors/bigquery/extractor.py`) — DuckDB BQ extension, remote-only
- **Jira** (`connectors/jira/`) — Webhook + incremental parquet transform

## extract.duckdb Contract

Every connector produces the same output:
```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

The `_meta` table must have columns:
- `table_name VARCHAR` — view name
- `description VARCHAR`
- `rows BIGINT`
- `size_bytes BIGINT`
- `extracted_at TIMESTAMP`
- `query_mode VARCHAR` — 'local' (data here) or 'remote' (query on demand)

## Adding a New Connector

1. Create `connectors/<name>/extractor.py`:
   ```python
   import duckdb
   from pathlib import Path

   def run(output_dir: str, table_configs: list[dict], **kwargs):
       output = Path(output_dir)
       data_dir = output / "data"
       data_dir.mkdir(parents=True, exist_ok=True)

       conn = duckdb.connect(str(output / "extract.duckdb"))
       # Create _meta table
       # For each table: COPY TO parquet, create view, insert _meta row
       conn.close()
   ```

2. Register tables in DuckDB `table_registry` via admin API or migration script.
   Set `source_type` to your connector name.

3. Add required env vars to `.env` and `config/.env.template`.

4. The SyncOrchestrator (`src/orchestrator.py`) will auto-discover your extract.duckdb.

## Configuration
- Instance-level config: `config/instance.yaml` (connection details)
- Table definitions: DuckDB `table_registry` table
- Credentials: environment variables

## BigQuery: pick a mode

| Need | Mode | Why |
|------|------|-----|
| Latency under 100 ms, table fits on disk | `materialized` | Local parquet, no BQ roundtrip |
| Table too large for analyst's disk, occasional ad-hoc query | `remote` | DuckDB BQ extension, no download |
| Table too large for disk AND analyst hits it constantly | `materialized` with aggregation/filter | Scheduled COPY of a slice |
| One-off subquery joined with local data | (no registry row) | Use `da query --register-bq …` for ad-hoc |

Cost: `materialized` runs once per `sync_schedule` regardless of how many analysts query it; `remote` runs once per analyst-query. The break-even is roughly query frequency × bytes scanned vs. one COPY × bytes scanned.

Guardrail: `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB) blocks the COPY when BQ's dry-run estimate exceeds the cap. Set it explicitly per environment in `instance.yaml`. The default 10 GiB cap applies when the knob is missing OR set to YAML `null`. To explicitly disable the guardrail, use `max_bytes_per_materialize: 0`.

Register a materialized table:

```bash
da admin register-table orders_90d \
    --source-type bigquery \
    --query-mode materialized \
    --query @docs/queries/orders_90d.sql \
    --schedule "every 6h"
```

`--query` also accepts inline SQL.
