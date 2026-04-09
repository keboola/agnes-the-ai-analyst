# Scripts

Utility and migration scripts for Agnes AI Data Analyst.

## Active Scripts

| Script | Purpose |
|--------|---------|
| `generate_sample_data.py` | Generate sample data for development/demo |
| `duckdb_manager.py` | DuckDB database management utilities |
| `init.sh` | Initial server setup (install deps, create dirs) |

## Migration Scripts (one-time use)

| Script | Purpose |
|--------|---------|
| `migrate_json_to_duckdb.py` | Migrate v1 JSON state files to DuckDB |
| `migrate_parquets_to_extracts.py` | Migrate v1 parquet layout to extract.duckdb |
| `migrate_registry_to_duckdb.py` | Migrate v1 table registry to DuckDB |
