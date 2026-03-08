# Plan: Fix Parquet Type Preservation & DuckDB Schema Mismatch

## Context

Issues #185, #186, #187 report that DuckDB views fail for partitioned tables when parquet files have schema mismatches. The root cause is twofold:
1. **DuckDB** reads partitioned parquet files without `union_by_name=true`, so if a column is `null` type in one partition and `VARCHAR` in another, it crashes.
2. **Parquet writing** uses `pa.Table.from_pandas()` without explicit schema — columns with all NULLs get inferred as `null` type instead of proper type (STRING, DATE, etc.).

**Note:** #187 is a duplicate of #186.

## Changes

### 1. DuckDB fix — `scripts/duckdb_manager.py` (line 235)

Add `union_by_name=true` to `read_parquet()` for partitioned views:

```python
# Before:
sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{glob_pattern}')"
# After:
sql = f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{glob_pattern}', union_by_name=true)"
```

This is an immediate fix — existing partition files with `null`-typed columns will be readable without re-syncing.

### 2. PyArrow type mapping — `src/keboola_client.py` (after line 48)

Add `KEBOOLA_TO_PYARROW_TYPES` dict alongside existing `KEBOOLA_TO_PANDAS_TYPES`:

```python
KEBOOLA_TO_PYARROW_TYPES = {
    "STRING": pa.string(), "VARCHAR": pa.string(), "TEXT": pa.string(),
    "INTEGER": pa.int64(), "BIGINT": pa.int64(),
    "NUMERIC": pa.float64(), "DECIMAL": pa.float64(), "FLOAT": pa.float64(), "DOUBLE": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "DATE": pa.date32(),
    "TIMESTAMP": pa.timestamp("us"), "TIMESTAMP_NTZ": pa.timestamp("us"), "TIMESTAMP_TZ": pa.timestamp("us", tz="UTC"),
}
```

### 3. Refactor provider cascade + new `get_pyarrow_schema()` — `src/keboola_client.py`

**3a.** Extract duplicated provider cascade logic from `get_pandas_dtypes()`, `get_date_columns()` into a shared `_resolve_keboola_type(col_meta_list) -> str` helper. This logic is repeated 3x — the new method would be the 4th copy without refactoring.

**3b.** Add `get_pyarrow_schema(table_id) -> Optional[pa.Schema]` method to `KeboolaClient`:
- Builds `pa.Schema` from Keboola metadata using `_resolve_keboola_type()` + `KEBOOLA_TO_PYARROW_TYPES`
- Returns `None` with WARNING log if metadata unavailable (graceful fallback)
- Refactor `get_pandas_dtypes()` and `get_date_columns()` to also use `_resolve_keboola_type()`

### 4. New helper `apply_schema_to_table()` — `src/parquet_manager.py` (after line 103)

Function that casts a `pa.Table` to match a target `pa.Schema`:
- Matches columns by name (not position)
- For `null`-type columns: creates typed null array via `pa.nulls(len, type=target_type)`
- For other mismatches: uses `col.cast(target_type, safe=True)` — if cast fails, keeps original type and logs warning (no data is changed or lost)
- Columns not in schema keep their inferred type
- Logs warnings on cast failures, doesn't crash

### 5. Integrate schema into all parquet write paths

Add `pyarrow_schema` parameter to these methods and apply `apply_schema_to_table()` after `pa.Table.from_pandas()` and `convert_date_columns_to_date32()`:

| Method | File | Line | Current issue |
|--------|------|------|---------------|
| `csv_to_parquet()` | `parquet_manager.py` | 274 | No explicit schema |
| `merge_parquet()` | `parquet_manager.py` | 493 | No explicit schema |
| `_process_csv_to_partitions()` | `data_sync.py` | 848, 854 | No explicit schema |
| `_deduplicate_partitions()` | `data_sync.py` | 890 | Uses `df.to_parquet()` — bypasses entire PyArrow pipeline |

**`_deduplicate_partitions()` fix (line 890):** Replace `df.to_parquet()` with full PyArrow pipeline:
```python
table = pa.Table.from_pandas(df, preserve_index=False)
if date_columns:
    table = convert_date_columns_to_date32(table, date_columns)
if pyarrow_schema:
    table = apply_schema_to_table(table, pyarrow_schema)
pq.write_table(table, partition_path, compression="snappy")
```

### 6. Update all callers to fetch and pass `pyarrow_schema`

Each caller already fetches `dtypes` and `date_columns`. Add one line after them:

```python
pyarrow_schema = self.keboola_client.get_pyarrow_schema(table_config.id)
```

| Caller method | File:Line | Passes to |
|---------------|-----------|-----------|
| `_full_refresh()` | `data_sync.py:318-328` | `csv_to_parquet()` |
| `_incremental_single_file_sync()` | `data_sync.py:455-508` | `merge_parquet()`, `csv_to_parquet()` |
| `_incremental_partitioned_sync()` | `data_sync.py:600-612` | `_process_csv_to_partitions()`, `_deduplicate_partitions()` |
| `_chunked_initial_load()` | `data_sync.py:673-766` | `_process_csv_to_partitions()`, `_deduplicate_partitions()` |
| `_partitioned_sync()` | `data_sync.py:989-1001` | `_process_csv_to_partitions()`, `_deduplicate_partitions()` |

## Tests — `data/tests/test_parquet_types.py` (local only, gitignored)

Test file lives in `data/tests/` which is gitignored (`data/` is in `.gitignore`). Tests are for **local verification only** — they don't go to the server or CI.

All test data is created via pytest's `tmp_path` fixture (auto-cleaned) or in `data/tests/tmp/`. No test artifacts persist after the run.

10 test cases, all local (no Keboola API calls):

**Core tests:**
1. **`test_union_by_name_resolves_schema_mismatch`** — Two parquet files with conflicting types, DuckDB reads with `union_by_name=true`
2. **`test_apply_schema_fixes_null_type_columns`** — `apply_schema_to_table()` converts null-type columns to proper types
3. **`test_parquet_preserves_types_with_all_null_columns`** — End-to-end: CSV with all-NULL column → Parquet → verify correct type
4. **`test_deduplicate_preserves_date32_types`** — After dedup, DATE32 columns retain their type
5. **`test_keboola_to_pyarrow_mapping_completeness`** — Every key in `KEBOOLA_TO_PANDAS_TYPES` exists in `KEBOOLA_TO_PYARROW_TYPES`
6. **`test_get_pyarrow_schema_from_metadata`** — Unit test with mocked Keboola metadata → verify correct schema

**Additional tests (from Gemini review):**
7. **`test_apply_schema_handles_cast_error_gracefully`** — Column with uncastable values (e.g. "abc" → int64) doesn't crash, logs warning, keeps original type
8. **`test_schema_with_extra_csv_column`** — CSV has column not in metadata → column preserved with inferred type
9. **`test_get_pyarrow_schema_handles_all_types`** — All types from `KEBOOLA_TO_PYARROW_TYPES` mapping verified
10. **`test_deduplicate_with_mixed_types_and_nulls`** — Partition with all-null + mixed-data columns → correct schema after dedup

## Files modified

- `scripts/duckdb_manager.py` — 1 line change (union_by_name)
- `src/keboola_client.py` — new `KEBOOLA_TO_PYARROW_TYPES` mapping + `_resolve_keboola_type()` helper + `get_pyarrow_schema()` method + refactor existing methods
- `src/parquet_manager.py` — new `apply_schema_to_table()` function + `pyarrow_schema` param on `csv_to_parquet()` and `merge_parquet()`
- `src/data_sync.py` — `pyarrow_schema` param on `_process_csv_to_partitions()` and `_deduplicate_partitions()` + update all 5 callers
- `data/tests/test_parquet_types.py` — new file, 10 tests (**gitignored**, local verification only)

## Verification

1. Run `pytest data/tests/test_parquet_types.py -v` — all 10 tests pass
2. Run `pytest tests/` — existing CI tests still pass (no regressions)
3. Existing parquet files don't need re-sync — the `union_by_name=true` fix handles them immediately
4. On next sync cycle, partitions get re-written with correct schema

## Cleanup after verification

After all tests pass and code changes are committed:
```bash
rm -rf data/tests/
```
Test file and all test artifacts live in `data/` (gitignored) — nothing gets committed or deployed to server.
