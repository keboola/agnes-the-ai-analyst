# Core Refactoring — DuckDB-Centric Extract Architecture

**Date:** 2026-03-30
**Status:** Draft

## 1. Problem

The current data sync core is 5,900 lines of tightly coupled code:
- `src/config.py` (653 lines) — parses YAML from markdown files
- `src/data_sync.py` (734 lines) — god object: orchestration + schema gen + profiling + systemctl restart
- `src/parquet_manager.py` (755 lines) — CSV→pandas→PyArrow→parquet conversion
- `connectors/keboola/adapter.py` (820 lines) — download + type cast + merge + partition + write
- `connectors/keboola/client.py` (877 lines) — Keboola REST API wrapper
- `connectors/bigquery/adapter.py` (665 lines) — similar pattern for BigQuery

Heavy dependencies: pandas, pyarrow, kbcstorage, google-cloud-bigquery, google-cloud-bigquery-storage.

Fragile: permission issues, incremental merge bugs, markdown parser edge cases. Adding a new connector requires 500-1700 lines of Python.

## 2. Solution

Replace the entire sync pipeline with DuckDB as the universal data bus. DuckDB extensions (keboola, bigquery, postgres, etc.) handle extraction. Each extractor produces a self-contained output folder with parquets + a DuckDB file with views. No pandas, no PyArrow, no custom CSV parsing.

Adding a new connector = 1 SQL config row in `table_registry`, not a new Python module.

## 3. Architecture

### 3.1 Server-side: Extractors produce self-contained output folders

```
/data/
├── extracts/
│   ├── keboola/                          ← KeboolaExtractor output
│   │   ├── parquet/
│   │   │   ├── orders.parquet
│   │   │   └── customers.parquet
│   │   └── extract.duckdb               ← views pointing to ./parquet/*
│   │
│   ├── bigquery/                         ← BigQueryExtractor output
│   │   ├── parquet/
│   │   │   └── deal_traffic.parquet
│   │   └── extract.duckdb
│   │
│   └── jira/                             ← JiraExtractor output
│       ├── parquet/
│       │   └── tickets.parquet
│       └── extract.duckdb
│
├── analytics.duckdb                      ← Master: ATTACHes all extract DBs + flat views
│
└── state/
    └── system.duckdb                     ← Users, sync_state, knowledge (existing, unchanged)
```

Each extractor writes into its own `output_dir`:
- `parquet/` — data files
- `extract.duckdb` — views pointing to `./parquet/*.parquet` (relative paths)

**Path resolution:** DuckDB resolves relative paths from the process CWD, not the .duckdb file location. Extractors must use absolute paths in views, or the orchestrator must set CWD before opening the DuckDB. Recommendation: use absolute paths (`/data/extracts/keboola/parquet/orders.parquet`) for robustness.

Master `analytics.duckdb` ATTACHes all extractor DBs:
```sql
ATTACH '/data/extracts/keboola/extract.duckdb' AS keboola (READ_ONLY);
ATTACH '/data/extracts/bigquery/extract.duckdb' AS bigquery (READ_ONLY);
-- Flat views for convenience:
CREATE OR REPLACE VIEW orders AS SELECT * FROM keboola.orders;
CREATE OR REPLACE VIEW deal_traffic AS SELECT * FROM bigquery.deal_traffic;
```

### 3.2 Extractor interface

```python
class ExtractResult:
    output_dir: str           # path to extractor output folder
    tables: list[dict]        # [{name, rows, hash, size_bytes}]

class DataExtractor(ABC):
    @abstractmethod
    def extract(self, table_configs: list, output_dir: str) -> ExtractResult:
        """Extract data into output_dir/parquet/ and create output_dir/extract.duckdb with views."""

    def extract_incremental(self, table_configs, output_dir, since: datetime) -> ExtractResult:
        """Incremental extract. Default: falls back to full extract."""
        return self.extract(table_configs, output_dir)
```

### 3.3 Keboola extractor implementation

```python
class KeboolaExtractor(DataExtractor):
    def __init__(self, token: str, url: str):
        self.token = token
        self.url = url

    def extract(self, table_configs, output_dir) -> ExtractResult:
        parquet_dir = f"{output_dir}/parquet"
        os.makedirs(parquet_dir, exist_ok=True)

        conn = duckdb.connect(f"{output_dir}/extract.duckdb")
        conn.execute("INSTALL keboola FROM community; LOAD keboola;")
        conn.execute(f"ATTACH '{self.url}' AS kbc (TYPE keboola, TOKEN '{self.token}')")

        tables = []
        for tc in table_configs:
            if tc.query_mode == "remote":
                continue

            pq_path = f"{parquet_dir}/{tc.name}.parquet"
            conn.execute(f"""
                COPY (SELECT * FROM kbc."{tc.bucket}".{tc.source_table})
                TO '{pq_path}' (FORMAT PARQUET)
            """)
            rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]

            # Create view with relative path
            conn.execute(f"""
                CREATE VIEW {tc.name} AS
                SELECT * FROM read_parquet('./parquet/{tc.name}.parquet')
            """)

            tables.append({"name": tc.name, "rows": rows, ...})

        conn.execute("DETACH kbc")
        conn.close()
        return ExtractResult(output_dir=output_dir, tables=tables)
```

~50 lines. Replaces 1,700 lines (adapter.py + client.py).

### 3.4 Adding a new connector: config, not code

For most data sources, DuckDB has a native extension. New connector = SQL config in `table_registry`:

```sql
INSERT INTO table_registry (id, name, source_type, extension_install, attach_sql, select_sql) VALUES
('pg_users', 'Users', 'postgres',
 'INSTALL postgres; LOAD postgres;',
 $$ATTACH 'postgresql://user:pass@host/db' AS src (TYPE postgres)$$,
 'SELECT * FROM src.public.users');
```

The generic `DuckDBExtractor` reads these configs and executes them:

```python
class DuckDBExtractor(DataExtractor):
    """Universal extractor — driven by SQL config from table_registry."""

    def extract(self, table_configs, output_dir) -> ExtractResult:
        parquet_dir = f"{output_dir}/parquet"
        os.makedirs(parquet_dir, exist_ok=True)
        conn = duckdb.connect(f"{output_dir}/extract.duckdb")

        # Group by source_type for one ATTACH per source
        by_source = defaultdict(list)
        for tc in table_configs:
            by_source[tc.source_type].append(tc)

        tables = []
        for source_type, configs in by_source.items():
            tc0 = configs[0]
            if tc0.extension_install:
                conn.execute(tc0.extension_install)
            conn.execute(tc0.attach_sql)

            for tc in configs:
                if tc.query_mode == "remote":
                    continue
                pq_path = f"{parquet_dir}/{tc.name}.parquet"
                conn.execute(f"COPY ({tc.select_sql}) TO '{pq_path}' (FORMAT PARQUET)")
                rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
                conn.execute(f"CREATE VIEW {tc.name} AS SELECT * FROM read_parquet('./parquet/{tc.name}.parquet')")
                tables.append({"name": tc.name, "rows": rows, ...})

        conn.close()
        return ExtractResult(output_dir=output_dir, tables=tables)
```

Supported via DuckDB extensions (no custom code):
- Keboola (`keboola` extension)
- BigQuery (`bigquery` extension)
- PostgreSQL (`postgres` — built-in)
- MySQL (`mysql` — built-in)
- SQLite (`sqlite` — built-in)
- S3/GCS Parquet (`httpfs` — built-in)
- CSV/JSON files (`read_csv_auto`, `read_json_auto` — built-in)

Sources without DuckDB extension (REST APIs, custom formats) get a Python extractor implementing `DataExtractor`.

### 3.5 Orchestrator

```python
class SyncOrchestrator:
    def sync(self, source_type: str = None):
        """Run extractors, rebuild master analytics.duckdb, update state."""

        # 1. Get table configs from registry
        configs = self.registry.list_by_source(source_type)

        # 2. Group by extractor
        by_extractor = group_by_source_type(configs)

        # 3. Run each extractor into its output folder
        for ext_name, ext_configs in by_extractor.items():
            output_dir = f"/data/extracts/{ext_name}"
            extractor = self.get_extractor(ext_name)
            result = extractor.extract(ext_configs, output_dir)

            # Update sync state per table
            for t in result.tables:
                self.state.update_sync(t["name"], rows=t["rows"], hash=t["hash"])

        # 4. Rebuild master analytics.duckdb
        self.rebuild_master_db()

    def rebuild_master_db(self):
        """ATTACH all extractor DBs, create flat views."""
        conn = duckdb.connect("/data/analytics.duckdb")

        for ext_dir in Path("/data/extracts").iterdir():
            ext_db = ext_dir / "extract.duckdb"
            if ext_db.exists():
                name = ext_dir.name
                conn.execute(f"ATTACH '{ext_db}' AS {name} (READ_ONLY)")

                # Create flat views (no prefix)
                views = conn.execute(f"""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_catalog = '{name}' AND table_type = 'VIEW'
                """).fetchall()
                for (view_name,) in views:
                    conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM {name}.{view_name}")

        conn.close()
```

~60 lines. Replaces 734-line DataSyncManager.

### 3.6 Config: table_registry replaces data_description.md

Extended `table_registry` schema (in `system.duckdb`):

```sql
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,

    -- Source config
    source_type VARCHAR NOT NULL,          -- 'keboola', 'bigquery', 'postgres', 'csv'
    bucket VARCHAR,                        -- Keboola bucket (e.g., 'in.c-crm')
    source_table VARCHAR,                  -- Table name in source
    extension_install VARCHAR,             -- 'INSTALL keboola FROM community; LOAD keboola;'
    attach_sql VARCHAR,                    -- 'ATTACH ''url'' AS src (TYPE keboola, TOKEN ''...'')'
    select_sql VARCHAR,                    -- 'SELECT * FROM src."bucket".table'

    -- Sync config
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',    -- 'local', 'remote'
    sync_schedule VARCHAR,                 -- 'every 15m', 'daily 05:00'
    profile_after_sync BOOLEAN DEFAULT true,

    -- Metadata
    folder VARCHAR,
    primary_key VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    registered_at TIMESTAMP DEFAULT current_timestamp
);
```

This replaces the entire `config.py` (653 lines) and `data_description.md` parser.

Import tool: `scripts/import_data_description.py` reads existing `data_description.md` and inserts into `table_registry`. One-time migration.

### 3.7 Client-side (analyst): unchanged

```
~/data-analyst/
├── server/
│   └── parquet/                  ← downloaded via da sync (per-user filtered)
│       ├── orders.parquet
│       └── customers.parquet
│
└── user/
    └── duckdb/
        └── analytics.duckdb     ← CLI creates views on local parquets
```

`da sync` downloads parquets from server API (filtered by permissions), creates local `analytics.duckdb` with views. Exactly as it works now. No change for analysts.

### 3.8 Remote tables

Tables with `query_mode = "remote"` are never downloaded. On the server, they stay accessible via the ATTACHed extractor DuckDB. Remote queries go through the API:

```
POST /api/query {"sql": "SELECT ... FROM deal_traffic WHERE ..."}
→ Server executes against analytics.duckdb
→ Which ATTACHes bigquery/extract.duckdb
→ Which ATTACHes BigQuery via extension
→ Query pushed down to BigQuery backend
```

For the analyst's CLI:
```bash
da query "SELECT country, sum(visitors) FROM deal_traffic WHERE date > '2025-03-01' GROUP BY country" --remote
```

### 3.9 Incremental sync (future)

Current design: full refresh only. When Keboola DuckDB extension adds `changedSince` support (issue keboola/duckdb-extension#10):

```python
def extract_incremental(self, table_configs, output_dir, since):
    # Extension will support changedSince filter
    conn.execute(f"""
        COPY (SELECT * FROM kbc."{tc.bucket}".{tc.source_table}
              WHERE _kbc_changed_since > '{since}')
        TO '{pq_path}' (FORMAT PARQUET)
    """)
    # Merge with existing parquet
    conn.execute(f"""
        CREATE VIEW {tc.name} AS
        SELECT * FROM read_parquet(['./parquet/{tc.name}.parquet', '{pq_path}'])
    """)
```

The extractor interface already has `extract_incremental()` with fallback to full refresh.

## 4. What gets deleted

| File | Lines | Why |
|------|-------|-----|
| `src/config.py` | 653 | Replaced by `table_registry` in DuckDB |
| `src/parquet_manager.py` | 755 | DuckDB `COPY TO` replaces all conversion |
| `src/data_sync.py` (most) | ~600 | New SyncOrchestrator ~60 lines |
| `connectors/keboola/adapter.py` | 820 | New KeboolaExtractor ~50 lines |
| `connectors/bigquery/adapter.py` | 665 | New BigQueryExtractor ~40 lines |
| **Total removed** | **~3500** | |
| **Total new** | **~300** | |

Kept as legacy fallback (not deleted):
- `connectors/keboola/client.py` — REST API wrapper, used if extension unavailable
- `src/profiler.py` — already uses DuckDB, unchanged
- `scripts/duckdb_manager.py` — legacy, superseded by extractor pattern

## 5. What stays unchanged

| Component | Why |
|-----------|-----|
| `src/repositories/` | Already DuckDB-backed, used by API |
| `src/db.py` | System DB schema management |
| `src/profiler.py` | Already uses DuckDB |
| `connectors/jira/` | Webhook pattern, different from extract |
| `connectors/llm/` | LLM abstraction, unrelated |
| `connectors/openmetadata/` | Catalog enrichment, unrelated |
| `app/` (FastAPI) | Calls orchestrator instead of DataSyncManager |
| `cli/` | Downloads parquets from API, unchanged |
| `webapp/` | Legacy Flask, unchanged |

## 6. Dependencies removed

| Package | Why not needed |
|---------|---------------|
| pandas | DuckDB handles CSV/type casting natively |
| pyarrow | DuckDB `COPY TO PARQUET` replaces all Parquet I/O |
| kbcstorage | Keboola DuckDB extension replaces REST API |
| google-cloud-bigquery | BigQuery DuckDB extension replaces client |
| google-cloud-bigquery-storage | Same |
| tqdm | Optional, not critical |

## 7. New dependency

| Package | Version | Why |
|---------|---------|-----|
| duckdb | >= 1.5.1 | Required for Keboola extension |

**Risk:** DuckDB 1.5.1 is not yet on PyPI stable (available via uv lock from source). Expected to be stable soon.

**Mitigation:** Legacy `connectors/keboola/client.py` stays as fallback. If extension is unavailable, `KeboolaAPIExtractor` uses old REST API + `duckdb.read_csv_auto()` instead of pandas.

## 8. Migration plan

1. Extend `table_registry` schema with source config columns
2. Write `scripts/import_data_description.py` — imports existing `data_description.md` into `table_registry`
3. Implement `DataExtractor` ABC + `KeboolaExtractor` + `DuckDBExtractor`
4. Implement `SyncOrchestrator` with `rebuild_master_db()`
5. Wire `app/api/sync.py` to use new orchestrator
6. Test with real Keboola token (project from demo notebooks)
7. Verify `da sync` still produces identical local structure
8. Keep old code as legacy (don't delete until validated in production)

## 9. Testing

- Unit: extractor returns correct ExtractResult, views resolve, parquets readable
- Integration: real Keboola token → extract → parquet → views → query
- E2E: server Docker → da sync → offline query → correct results
- Regression: existing 156 tests must still pass (they don't touch old sync core)

## 10. Verified by testing (2026-03-30)

Keboola DuckDB extension tested with real token:
- `ATTACH` + `SELECT *` + `COPY TO parquet` works (1.5s for 15 rows)
- Filter pushdown: `=`, `>`, `<` supported but all columns are VARCHAR from Keboola
- `_timestamp` not exposed (no incremental via extension)
- `keboola_pull()` API doesn't match docs (issue #11 filed)
- Full refresh is the only reliable sync strategy for now
- Issues filed: keboola/duckdb-extension#6 through #11
