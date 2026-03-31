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
