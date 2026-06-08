# Playbook: new data-source connector

The orchestrator is **filesystem-driven** — there is no registration step. Write
`$DATA_DIR/extracts/<name>/extract.duckdb` and `SyncOrchestrator.rebuild()`
(`src/orchestrator.py:364`) discovers it, ATTACHes it, and creates master views.

## Files to create

```
connectors/<name>/__init__.py      # empty / package docstring
connectors/<name>/extractor.py     # writes extract.duckdb (+ data/*.parquet for local)
```

Do **not** modify `src/orchestrator.py` — the scan is path-driven.

## The `_meta` table (required)

Every extract.duckdb must contain `_meta`, one row per table. Exact shape
(`connectors/keboola/extractor.py:398`):

```sql
CREATE TABLE _meta (
    table_name   VARCHAR NOT NULL,            -- becomes the master view name; must match ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    description  VARCHAR,
    rows         BIGINT,                       -- 0 for remote
    size_bytes   BIGINT,                       -- 0 for remote
    extracted_at TIMESTAMP,                     -- datetime.now(timezone.utc)
    query_mode   VARCHAR DEFAULT 'local'        -- 'local' | 'remote' | 'materialized'
)
```

The orchestrator skips any `_meta` row whose `table_name` lacks a matching
view/table object in the extract (`src/orchestrator.py:459`).

## query_mode

- **local** — write `data/<table_name>.parquet` and a view
  `CREATE OR REPLACE VIEW "<table_name>" AS SELECT * FROM read_parquet(...)`.
- **remote** — the view references an external ATTACH alias; requires
  `_remote_attach` (below).
- **materialized** — written by the scheduled sync pass, not the extractor; the
  extractor skips these rows.

## `_remote_attach` (remote mode only)

Shape (`connectors/keboola/extractor.py:411`):

```sql
CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)
```

`token_env` is the env-var holding the token (`''` for an extension-specific auth
path, e.g. BigQuery's GCE metadata server). **Gotcha:** the `extension` must be in
`_COMMUNITY_EXTENSIONS` in `src/orchestrator_security.py:24` (currently
`{"keboola", "bigquery"}`) or the ATTACH is silently refused at rebuild.

## Steps

1. `connectors/<name>/__init__.py` (empty).
2. `connectors/<name>/extractor.py`: open `extract.duckdb.tmp`, create `_meta`
   (verbatim DDL), per table write parquet + view + insert `_meta` row; for remote
   add `_remote_attach` (and register the extension). Atomic `shutil.move` the tmp
   over `extract.duckdb` (`connectors/keboola/extractor.py:753`).
3. Register tables in `table_registry` (`source_type='<name>'`) via the admin API
   / `TableRegistryRepository` — read by the sync trigger, not by `rebuild()`.
4. TDD: a test that runs the extractor against a fixture and asserts the
   extract.duckdb `_meta` shape + that `rebuild()` creates the master views.

## Anchors

- `_meta` DDL: `connectors/keboola/extractor.py:398`
- `_remote_attach` + extension allowlist: `connectors/keboola/extractor.py:411`, `src/orchestrator_security.py:24`
- discovery + ATTACH: `src/orchestrator.py:364`
