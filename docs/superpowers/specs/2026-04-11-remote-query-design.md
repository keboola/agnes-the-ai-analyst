# Remote Query — Design Spec

**Date:** 2026-04-11
**Status:** Approved
**Scope:** Fix extension re-attach + two-phase remote query engine

## Context

BigQuery remote views created by the orchestrator don't work at query time because `get_analytics_db_readonly()` opens a fresh connection without re-loading the BigQuery extension. Additionally, the platform lacks the ability to run hybrid queries that JOIN local Parquet data with on-demand BigQuery subquery results.

The `padak/tmp_oss` v1 repo has `src/remote_query.py` with a two-phase protocol. The existing `scripts/duckdb_manager.py` in this repo already has `register_bq_table()` and `_create_bq_client()` helper functions. The `table_registry` already supports `query_mode` values: `local`, `remote`, `hybrid`.

**Primary user:** Claude Code agent running `da query` locally, or API consumers via `POST /api/query/hybrid`.

---

## Part 1: Fix Extension Re-attach

### Problem

`get_analytics_db_readonly()` in `src/db.py` opens analytics.duckdb in read-only mode and ATTACHes extract.duckdb files, but does NOT re-load extensions referenced in `_remote_attach` tables. BigQuery remote views fail with "Catalog Error: bq not found".

### Solution

After ATTACHing extract.duckdb files in `get_analytics_db_readonly()`, scan each for a `_remote_attach` table. For each record, re-load the extension and re-attach the remote source.

**Important: DuckDB read-only LOAD behavior.** The `read_only=True` flag on `duckdb.connect()` blocks writes to the DB file, but `LOAD` writes to the extension cache in `~/.duckdb/extensions/` (separate from the DB file). This should work, but MUST be empirically verified as the first implementation step. If LOAD fails in read-only mode, the workaround is to open the analytics DB WITHOUT `read_only=True` but still use read-only SQL patterns (no INSERT/UPDATE/DELETE), or to call `LOAD` on a separate in-memory connection first (DuckDB extension cache is process-wide).

Steps for each `_remote_attach` record:
1. `LOAD {extension}` — loads pre-installed extension from disk
2. Read token from `os.environ[token_env]` if `token_env` is non-empty
3. `ATTACH '{url}' AS {alias} (TYPE {extension}, READ_ONLY)` — with TOKEN if needed

If LOAD or ATTACH fails, log a warning and continue — local views still work.

### Changes

**File:** `src/db.py` — `get_analytics_db_readonly()` function

Add ~25 lines after the existing extract.duckdb ATTACH loop. Read `_remote_attach` table from each attached extract DB, collect unique (alias, extension, url, token_env) tuples, and re-attach.

Pattern follows `src/orchestrator.py:_attach_remote_extensions()` but simplified (no INSTALL — orchestrator pre-installs during rebuild).

**Concurrency note:** If the orchestrator runs `_atomic_swap_db()` while a read-only connection is open, the existing connection holds a file descriptor to the old inode (Unix semantics). This is safe — the old data remains accessible until the connection is closed.

---

## Part 2: Two-Phase Remote Query Engine

### Architecture

New module `src/remote_query.py` with a `RemoteQueryEngine` class:

```python
class RemoteQueryEngine:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        """Takes an existing DuckDB connection (analytics.duckdb with local views)."""

    def register_bq(self, alias: str, bq_sql: str) -> dict:
        """Execute BQ subquery, register result as in-memory DuckDB view.
        Returns {alias, rows, columns, memory_mb}.
        Raises RemoteQueryError on safety limit violation."""

    def execute(self, sql: str) -> dict:
        """Execute final DuckDB query against local + registered BQ views.
        Returns {columns: [...], rows: [...], row_count: int, truncated: bool}."""
```

### Two-Phase Flow

1. **Phase 1 — BQ Registration:** For each `register_bq(alias, bq_sql)` call:
   - COUNT(*) pre-check via Python BQ client → reject if >max_bq_registration_rows
   - Memory estimate: ~50 bytes/cell × rows × cols → reject if >max_memory_mb. Note: this is approximate. After query completes, use `arrow_table.nbytes` for accurate reporting in `bq_stats`.
   - Execute BQ query → `job.to_arrow()` → `conn.register(alias, arrow_table)`
   - Uses `scripts/duckdb_manager.py:_create_bq_client()` for BQ client creation (reuse)
   - Does NOT delegate to `register_bq_table()` directly — `RemoteQueryEngine.register_bq()` wraps BQ query execution with its own pre-check logic (COUNT, memory estimate), then calls `conn.register(alias, arrow_table)`. The existing `register_bq_table()` has no pre-check capability and would need signature changes to add one. Wrapping is cleaner than modifying shared code.
   - Gracefully handle missing `google-cloud-bigquery` package: catch `ImportError` and raise `RemoteQueryError(error_type="bq_error", message="google-cloud-bigquery not installed")`

2. **Phase 2 — DuckDB Query:** Execute final SQL against all views (local Parquet + registered BQ Arrow tables). Apply max_result_rows limit.

### Safety Limits

Configurable in `config/instance.yaml` under `remote_query:`:

```yaml
remote_query:
  max_bq_registration_rows: 500000   # max rows from a single BQ subquery (matches existing instance.yaml.example key)
  max_memory_mb: 2048                # max estimated memory for BQ result
  max_result_rows: 100000            # max rows in final result
  timeout_seconds: 300               # BQ query timeout
```

Note: `max_bq_registration_rows` matches the key already documented in `config/instance.yaml.example`.

Defaults are hardcoded in `RemoteQueryEngine` and overridden by instance config.

### Error Handling

Custom `RemoteQueryError` exception with structured error:

```python
class RemoteQueryError(Exception):
    def __init__(self, message: str, error_type: str, details: dict = None):
        # error_type: "row_limit", "memory_limit", "bq_error", "query_error", "timeout"
```

### CLI: `da query` Extension

Extend existing `cli/commands/query.py`:

```
da query --sql "SELECT o.*, t.views FROM orders o JOIN traffic t ON o.date = t.date" \
         --register-bq "traffic=SELECT date, SUM(views) as views FROM dataset.web WHERE date > '2026-01-01' GROUP BY 1"
```

- Multiple `--register-bq` flags allowed (one per BQ alias)
- Format: `"alias=BQ_SQL"` (split on first `=`)
- `--stdin` mode: reads JSON from stdin for complex SQL:
  ```json
  {"register_bq": {"traffic": "SELECT ..."}, "sql": "SELECT ..."}
  ```
- Output formats: `table` (default), `csv`, `json`

**CLI argument handling:** The existing `query_command` has `sql` as a required positional argument. When `--register-bq` is used, `sql` should be provided via `--sql` flag instead (named option, not positional). When `--stdin` is used, both `sql` and `register_bq` come from stdin JSON. Make `sql` an optional positional (`typer.Argument(None)`) and validate that exactly one of (positional sql, --sql flag, --stdin) is provided.

### API: `POST /api/query/hybrid`

```
POST /api/query/hybrid
Authorization: Bearer <admin_token>

{
  "register_bq": {
    "traffic": "SELECT date, SUM(views) FROM dataset.web WHERE date > '2026-01-01' GROUP BY 1"
  },
  "sql": "SELECT o.*, t.views FROM orders o JOIN traffic t ON o.date = t.date",
  "format": "json"
}
```

**Response:**
```json
{
  "columns": ["order_id", "date", "views"],
  "rows": [...],
  "row_count": 1234,
  "truncated": false,
  "bq_stats": {
    "traffic": {"rows": 365, "columns": 2, "memory_mb": 0.03}
  }
}
```

**Auth:** `require_admin` — BQ queries cost money, only admins can trigger them.

**Validation — both `register_bq` SQL and final `sql`:**
- Apply the same SQL blocklist from `app/api/query.py` (blocks LOAD, ATTACH, INSTALL, read_parquet with paths, path traversal patterns, etc.)
- `register_bq` SQL additionally validated as SELECT-only (no INSERT/UPDATE/DELETE/DROP)
- Reuse the existing `_validate_sql()` helper from `app/api/query.py` (extract to shared utility if needed)

**Connection lifecycle:** The API endpoint owns the connection. Pattern:
```python
analytics = get_analytics_db_readonly()
try:
    engine = RemoteQueryEngine(analytics)
    # ... register_bq + execute
finally:
    analytics.close()
```

---

## Implementation Summary

### New Files

| File | Purpose |
|---|---|
| `src/remote_query.py` | `RemoteQueryEngine` class + `RemoteQueryError` |
| `app/api/query_hybrid.py` | `POST /api/query/hybrid` endpoint |
| `tests/test_remote_query.py` | Engine unit tests (mocked BQ client) |

### Modified Files

| File | Changes |
|---|---|
| `src/db.py` | `get_analytics_db_readonly()` — add extension re-attach from `_remote_attach` |
| `cli/commands/query.py` | Add `--register-bq` and `--stdin` flags |
| `app/main.py` | Register hybrid query router |
| `CLAUDE.md` | Document hybrid query usage |

### Implementation Order

1. Fix extension re-attach in `src/db.py` (unblocks remote views)
2. `RemoteQueryEngine` in `src/remote_query.py` (core logic)
3. CLI extension `--register-bq`
4. API endpoint `POST /api/query/hybrid`
5. CLAUDE.md update + integration tests

### Test Coverage

- `tests/test_remote_query.py` — engine tests with mocked BQ client (safety limits, registration, error handling)
- `tests/test_db.py` — extension re-attach test (mock _remote_attach table)
- `tests/test_api.py` — hybrid query endpoint (auth, validation)
- `tests/test_cli.py` — `--register-bq` flag parsing
