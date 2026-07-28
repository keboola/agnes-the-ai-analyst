# Core Refactoring — DuckDB-Centric Extract Architecture

**Date:** 2026-03-30
**Status:** Draft v2

## 1. Problem

Current sync core is 5,900 lines with heavy dependencies (pandas, pyarrow, kbcstorage). Fragile markdown config parser. Adding a connector requires 500-1700 lines of Python. Tightly coupled — connector downloads, type-casts, merges, partitions, and writes to disk all in one place.

## 2. Core idea

Every data source produces the same thing: **a folder with `extract.duckdb` + `data/`**. The orchestrator doesn't care how the data got there — it just ATTACHes the DuckDB file.

```
/data/extracts/{source_name}/
├── extract.duckdb          ← MUST exist. Contains _meta table + views/tables on data.
└── data/                   ← Data files the views point to (parquet, csv, whatever).
```

That's it. That's the entire contract.

## 3. extract.duckdb contract

Every `extract.duckdb` MUST contain:

**`_meta` table** — describes what's inside:
```sql
CREATE TABLE _meta (
    table_name VARCHAR NOT NULL,
    description VARCHAR,
    rows BIGINT,
    size_bytes BIGINT,
    extracted_at TIMESTAMP,
    query_mode VARCHAR DEFAULT 'local'   -- 'local' = data is here, 'remote' = query on demand
);
```

**Views or tables** for each entry in `_meta` — how they store data is their business (parquet, csv, in-memory, remote ATTACH — doesn't matter).

## 4. Three types of sources

### Batch pull (Keboola, Postgres, CSV)

Scheduler or manual trigger runs extractor → rewrites entire output folder.

```
Scheduler (every 15m)
  → python -m connectors.keboola.extract
  → output: /data/extracts/keboola/extract.duckdb + data/*.parquet
  → orchestrator.rebuild()
```

One instance typically has **one primary batch source** (configured in `instance.yaml`). The extractor reads `table_registry` for which tables to pull and how (sync_strategy, schedule).

### Remote attach (BigQuery)

No data download. DuckDB BigQuery community extension ATTACHes directly to BQ. Queries go to BigQuery on-demand.

```
/data/extracts/bigquery/
├── extract.duckdb          ← ATTACH to BQ + views + _meta (query_mode='remote')
└── (no data/ directory)
```

```sql
INSTALL bigquery FROM community; LOAD bigquery;
ATTACH 'project=my_gcp_project' AS bq (TYPE bigquery, READ_ONLY);
CREATE VIEW orders AS SELECT * FROM bq.dataset.orders;
INSERT INTO _meta VALUES ('orders', 'Order data', 0, 0, now(), 'remote');
```

Extractor (`connectors/bigquery/extractor.py`, ~50 lines) runs once at init or when table_registry changes. It creates `extract.duckdb` with views that delegate to BQ — no parquets, no downloads. Orchestrator ATTACHes it like any other source.

Replaces: `adapter.py` (665 lines) + `client.py` (644 lines) + `remote_query.py` (~300 lines).

### Real-time push (Jira webhooks)

External system sends events → webhook handler updates output folder incrementally.

```
Jira sends webhook → POST /webhooks/jira
  → handler processes event
  → appends/updates parquet in /data/extracts/jira/data/
  → updates extract.duckdb views + _meta
```

No scheduler needed — data arrives when it arrives. Output folder is updated in-place, not rewritten.

### All three produce the same output

The orchestrator doesn't know or care which type produced the folder or whether data is local parquets or remote BQ views. It just ATTACHes `extract.duckdb`.

## 5. Orchestrator

```python
class SyncOrchestrator:
    def rebuild(self):
        """Scan /data/extracts/*, ATTACH each, create master views."""
        master = duckdb.connect("/data/analytics.duckdb")

        for ext_dir in sorted(Path("/data/extracts").iterdir()):
            db = ext_dir / "extract.duckdb"
            if not db.exists():
                continue

            name = ext_dir.name
            master.execute(f"ATTACH '{db}' AS {name} (READ_ONLY)")

            # Read _meta to know what's available
            meta = master.execute(f"SELECT table_name, rows, query_mode FROM {name}._meta").fetchall()

            # Create flat views in master
            for table_name, rows, query_mode in meta:
                master.execute(f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM {name}.{table_name}")
                self.state.update_sync(table_name, rows=rows)

        master.close()
```

~30 lines. Replaces 734-line DataSyncManager.

## 6. Keboola extractor

```python
# connectors/keboola/extractor.py (~60 lines)

def run(output_dir: str, table_configs: list[dict]):
    """Extract tables from Keboola into output_dir."""
    data_dir = Path(output_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(f"{output_dir}/extract.duckdb")
    conn.execute("INSTALL keboola FROM community; LOAD keboola;")
    conn.execute(f"ATTACH '{url}' AS kbc (TYPE keboola, TOKEN '{token}')")

    # Create _meta
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'local'
    )""")

    now = datetime.now(timezone.utc)
    for tc in table_configs:
        if tc["query_mode"] == "remote":
            # Register in _meta but don't download
            conn.execute(f"INSERT INTO _meta VALUES ('{tc['name']}', '', 0, 0, '{now}', 'remote')")
            continue

        pq_path = str(data_dir / f"{tc['name']}.parquet")
        conn.execute(f"""COPY (SELECT * FROM kbc."{tc['bucket']}".{tc['source_table']})
                        TO '{pq_path}' (FORMAT PARQUET)""")

        rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
        size = os.path.getsize(pq_path)

        conn.execute(f"CREATE OR REPLACE VIEW {tc['name']} AS SELECT * FROM read_parquet('{pq_path}')")
        conn.execute(f"INSERT INTO _meta VALUES ('{tc['name']}', '{tc.get('description','')}', {rows}, {size}, '{now}', 'local')")

    conn.execute("DETACH kbc")
    conn.close()

if __name__ == "__main__":
    # Standalone: reads config from table_registry, runs extraction
    configs = load_table_configs()
    run("/data/extracts/keboola", configs)
```

Replaces 1,700 lines (adapter.py + client.py).

## 7. BigQuery extractor

```python
# connectors/bigquery/extractor.py (~50 lines)

def init_extract(output_dir: str, project_id: str, table_configs: list[dict]):
    """Create extract.duckdb with remote views into BigQuery."""
    conn = duckdb.connect(f"{output_dir}/extract.duckdb")
    conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
    conn.execute(f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)")

    # Create _meta
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'remote'
    )""")

    now = datetime.now(timezone.utc)
    for tc in table_configs:
        dataset = tc['bucket']  # BigQuery dataset
        source = tc['source_table']
        conn.execute(f'CREATE OR REPLACE VIEW {tc["name"]} AS SELECT * FROM bq."{dataset}"."{source}"')
        conn.execute(f"INSERT INTO _meta VALUES ('{tc['name']}', '{tc.get('description','')}', 0, 0, '{now}', 'remote')")

    conn.execute("DETACH bq")
    conn.close()

if __name__ == "__main__":
    configs = load_table_configs(source_type="bigquery")
    init_extract("/data/extracts/bigquery", project_id, configs)
```

No `data/` directory. All queries go directly to BigQuery via DuckDB extension. Replaces 1,600 lines (adapter.py + client.py + remote_query.py).

Authentication: DuckDB BigQuery extension uses Application Default Credentials (ADC) or `GOOGLE_APPLICATION_CREDENTIALS` env var — same as the current `google-cloud-bigquery` Python client.

## 8. Config: table_registry

`table_registry` in `system.duckdb` (already exists, extend with source columns):

```sql
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,

    -- Source
    source_type VARCHAR NOT NULL,     -- 'keboola', 'bigquery', 'jira', 'postgres'
    bucket VARCHAR,                   -- Keboola bucket or schema
    source_table VARCHAR,             -- table name in source

    -- Sync behavior
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',
    sync_schedule VARCHAR,
    profile_after_sync BOOLEAN DEFAULT true,

    -- Metadata
    primary_key VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    registered_at TIMESTAMP DEFAULT current_timestamp
);
```

Instance-level source config stays in `instance.yaml`:
```yaml
data_source: keboola
keboola:
  url: https://connection.us-east4.gcp.keboola.com
  token_env: KEBOOLA_STORAGE_TOKEN
```

Table list goes in `table_registry`. Import from existing `data_description.md` via one-time migration script.

## 9. How it runs

```
instance.yaml → which source (keboola)
table_registry → which tables + how (full_refresh, schedule)

Scheduler:
  Every 15 min:
    1. Read table_registry for tables due to sync
    2. Run extractor: python -m connectors.keboola.extract
    3. Extractor writes /data/extracts/keboola/
    4. orchestrator.rebuild() → ATTACH → master views

API trigger:
  POST /api/sync/trigger
    → same as scheduler step 2-4

CLI:
  da sync (on analyst machine)
    → calls GET /api/sync/manifest
    → downloads parquets from /api/data/{table}/download
    → creates local analytics.duckdb with views
```

## 10. Adding a new source

**If DuckDB has extension for it (most cases):**

1. Add tables to `table_registry` (via admin API or CLI)
2. Write extractor script: `connectors/{name}/extractor.py` (~30-60 lines)
   - `INSTALL extension; LOAD extension; ATTACH source; COPY TO parquet`
3. Add to scheduler config
4. Done

**If no DuckDB extension (REST API, custom):**

1. Same as above but extractor fetches data via HTTP/SDK
2. Writes result to DuckDB via `read_json_auto` or `conn.register()`
3. Same output format: `extract.duckdb` + `data/`

**Jira-style webhook:**

1. Add webhook endpoint to FastAPI
2. Handler updates `/data/extracts/jira/` incrementally
3. Same output format — orchestrator picks it up on next rebuild

## 11. What gets deleted

| File | Lines | Replaced by |
|------|-------|-------------|
| `src/config.py` | 653 | `table_registry` in DuckDB |
| `src/parquet_manager.py` | 755 | DuckDB `COPY TO` |
| `src/data_sync.py` (most) | ~600 | SyncOrchestrator (~30 lines) |
| `src/remote_query.py` | ~300 | DuckDB BigQuery ATTACH (queries go directly via extension) |
| `connectors/keboola/adapter.py` | 820 | extractor.py (~60 lines) |
| `connectors/bigquery/adapter.py` | 665 | extractor.py (~50 lines, remote-only via DuckDB BQ extension) |
| `connectors/bigquery/client.py` | 644 | DuckDB BigQuery extension (ADC auth, direct ATTACH) |
| **Total removed** | **~4,400** | **~200 new** |

Kept as legacy (not deleted):
- `connectors/keboola/client.py` — fallback if DuckDB Keboola extension unavailable
- `connectors/jira/` — webhook pattern, adapted to write extract.duckdb
- `src/profiler.py` — already DuckDB, unchanged

## 12. What stays unchanged

- `src/repositories/` — DuckDB-backed, used by API
- `src/db.py` — system DB schema
- `src/profiler.py` — already uses DuckDB
- `connectors/llm/`, `connectors/openmetadata/` — unrelated
- `app/` (FastAPI), `cli/`, `webapp/` — call orchestrator instead of DataSyncManager

## 13. Client side (analyst) — no change

```
da sync → downloads parquets from server API → creates local analytics.duckdb with views
```

Analyst doesn't know or care about extractors. Same flow as today.

## 14. Incremental sync (future)

Current: full refresh only. Extractor interface is ready for incremental:
- `table_registry` has `sync_strategy` field
- Extractor can check last sync time from `_meta.extracted_at`
- When Keboola DuckDB extension adds `changedSince` (issue #10), extractor uses it
- Until then: full refresh, which is fast enough for most tables via extension

## 15. Tested (2026-03-30)

Keboola DuckDB extension with real token:
- `ATTACH` + `SELECT *` + `COPY TO parquet`: works (1.5s for 15 rows)
- Extension: v0.1.0, requires DuckDB 1.5.1+
- Issues filed: keboola/duckdb-extension#6 through #11
