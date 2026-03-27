# Connectors — How to add a new data source

## Existing Connectors
- **Keboola** (`connectors/keboola/`) — Keboola Storage API
- **BigQuery** (`connectors/bigquery/`) — Google BigQuery
- **Jira** (`connectors/jira/`) — Jira webhook + API

## Adding a New Connector

1. Create `connectors/<name>/adapter.py` implementing the `DataSource` ABC:
   ```python
   from src.data_sync import DataSource

   class MyDataSource(DataSource):
       def sync_table(self, table_config, sync_state): ...
       def discover_tables(self): ...
       def get_column_metadata(self, table_id): ...
       def get_source_name(self): ...
   ```

2. The factory in `src/data_sync.py:create_data_source()` auto-discovers connectors.
   Set `DATA_SOURCE=<name>` in instance.yaml or .env.

3. Add required env vars to `.env` and `config/.env.template`.

4. Add tests to `tests/test_<name>_adapter.py`.

## Configuration
Each connector reads credentials from environment variables.
Table definitions are in `docs/data_description.md` (YAML blocks).
