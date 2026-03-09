# Scripts

Helper scripts for working with AI Data Analyst project.

These scripts are synced from the server into `server/scripts/` on the analyst's machine.

## Available Scripts

### `setup_views.sh`

Initialize or refresh DuckDB views on Parquet files.

```bash
bash server/scripts/setup_views.sh
```

### `sync_data.sh`

Synchronize data from server, upload user files, and refresh DuckDB.

```bash
# Recommended: update scripts first, then sync
rsync -avz data-analyst:server/scripts/ ./server/scripts/   # Linux/macOS
scp -r data-analyst:server/scripts/* ./server/scripts/      # Windows fallback
bash server/scripts/sync_data.sh

# Other options:
bash server/scripts/sync_data.sh --dry-run  # Preview what would be synced (no changes)
bash server/scripts/sync_data.sh --push     # Only upload user/ to server
```

**What sync does:**
1. **Self-update check** - detects if sync_data.sh changed, asks to re-run if so
2. Downloads `server/docs/`, `server/scripts/`, `server/metadata/` from server
3. Updates `CLAUDE.md` from latest template
4. Downloads `server/parquet/` data files (with `--delete` to remove old files)
5. Uploads `user/` directory to server (backup, no `--delete`)
6. Syncs Python environment to server
7. **Validates DuckDB** - if corrupted, deletes and recreates from parquets
8. Reinitializes DuckDB views (`CREATE OR REPLACE VIEW` for all tables)

**Self-update mechanism:**
The script checks its own checksum before and after syncing scripts. If it detects it was updated, it exits with a message asking you to run sync again. This ensures you always run the latest sync logic.

**DuckDB corruption recovery:**
If DuckDB file is corrupted (e.g., interrupted sync), it's automatically detected and recreated. All data is safe in parquet files - DuckDB only contains VIEW definitions.

## Typical Workflow

1. **First time setup**: Follow bootstrap.yaml instructions
2. **Before analysis**: Sync latest data
   ```bash
   bash server/scripts/sync_data.sh
   ```
4. **Analyze**: Use DuckDB database at `user/duckdb/analytics.duckdb`
