"""Parquet I/O helpers for the Keboola legacy SDK extraction path.

Ports the typed-schema parts of internal repo's `src/parquet_manager.py`
(`csv_to_parquet`, `apply_schema_to_table`, `convert_date_columns_to_date32`,
`_convert_column`) so the OSS extractor's legacy fallback preserves
column types from Keboola Storage metadata instead of flattening to
VARCHAR via `read_csv(all_varchar=true)`.

The DuckDB Keboola extension already returns typed columns (the extension
queries Storage's typed views), so the extension path doesn't need this.
This module only matters for the SDK fallback — which is what runs when
the extension errors on alias tables (keboola/duckdb-extension#17), and
for any feature that forces the SDK path (incremental, where_filters).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


# ───────────────────────────── DATE32 conversion ──────────────────────────────


def convert_date_columns_to_date32(
    table: pa.Table, date_columns: List[str]
) -> pa.Table:
    """Cast the listed columns to PyArrow `date32`.

    String columns are parsed via pandas with `errors='coerce'` so invalid
    inputs (`'0000-00-00'`, `'not-a-date'`) become NULL while the column
    type stays `date32` — invalid rows lose their value, the column keeps
    its type. All-null columns produce typed-null arrays. Columns not
    present in the table are silently ignored: the caller passes the
    union of all known DATE columns from Keboola metadata, but the export
    may have been column-projected.
    """
    if not date_columns:
        return table

    date_cols_set = {c for c in date_columns if c in table.column_names}
    if not date_cols_set:
        # Edge case: every requested date column is absent. Returning the
        # original table preserves identity (callers pass `... is table`
        # in tests) — emulate the no-op branch above.
        return table

    new_columns = []
    new_fields = []

    for i, field in enumerate(table.schema):
        if field.name not in date_cols_set:
            new_columns.append(table.column(i))
            new_fields.append(field)
            continue

        col = table.column(i)
        target = pa.date32()

        if col.null_count == len(col):
            new_columns.append(pa.nulls(len(col), type=target))
            new_fields.append(pa.field(field.name, target))
            continue

        if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
            series = col.to_pandas()
            parsed = pd.to_datetime(series, errors="coerce", format="mixed")
            invalid_count = int(parsed.isna().sum() - series.isna().sum())
            if invalid_count > 0:
                invalid_mask = series.notna() & parsed.isna()
                examples = series[invalid_mask].head(3).tolist()
                logger.warning(
                    "Column %r: %d invalid date values converted to NULL. "
                    "Examples: %s",
                    field.name, invalid_count, examples,
                )
            new_columns.append(pa.array(parsed.dt.date, type=target))
            new_fields.append(pa.field(field.name, target))
        else:
            try:
                new_columns.append(col.cast(target))
                new_fields.append(pa.field(field.name, target))
            except Exception as e:
                logger.warning(
                    "Column %r: failed to cast %s to date32, keeping original. Error: %s",
                    field.name, col.type, e,
                )
                new_columns.append(col)
                new_fields.append(field)

    return pa.Table.from_arrays(
        new_columns,
        schema=pa.schema(new_fields, metadata=table.schema.metadata),
    )


# ───────────────────────────── schema enforcement ─────────────────────────────


def apply_schema_to_table(
    table: pa.Table, target_schema: pa.Schema
) -> pa.Table:
    """Apply `target_schema` to `table`, handling type mismatches gracefully.

    - Columns not in `target_schema` keep their inferred type.
    - Null-type columns are replaced with typed-null arrays of the target type
      (DuckDB schema-mismatches when reading null-type columns vs typed parquet).
    - Matching types are kept as-is.
    - Mismatches are first attempted via `safe=True` cast; on failure, two
      pandas-backed fallbacks run: string → timestamp via `pd.to_datetime(utc=True)`
      then strip tz, and string → numeric via `pd.to_numeric(errors='coerce')`.
    - Anything still uncastable keeps the original column with a warning.
    """
    if len(target_schema) == 0:
        return table

    target_types = {f.name: f.type for f in target_schema}
    new_columns = []
    new_fields = []

    for i, field in enumerate(table.schema):
        col = table.column(i)
        if field.name not in target_types:
            new_columns.append(col)
            new_fields.append(field)
            continue

        target = target_types[field.name]

        if pa.types.is_null(col.type):
            new_columns.append(pa.nulls(len(col), type=target))
            new_fields.append(pa.field(field.name, target))
            continue

        if col.type == target:
            new_columns.append(col)
            new_fields.append(pa.field(field.name, target))
            continue

        try:
            new_columns.append(col.cast(target, safe=True))
            new_fields.append(pa.field(field.name, target))
            continue
        except Exception as cast_err:
            casted = _try_pandas_fallback(col, field.name, target, cast_err)
            if casted is not None:
                new_columns.append(casted)
                new_fields.append(pa.field(field.name, target))
            else:
                new_columns.append(col)
                new_fields.append(field)

    return pa.Table.from_arrays(
        new_columns,
        schema=pa.schema(new_fields, metadata=table.schema.metadata),
    )


def _try_pandas_fallback(
    col: pa.ChunkedArray,
    name: str,
    target: pa.DataType,
    cast_err: Exception,
) -> Optional[pa.Array]:
    """Try pandas-backed casts that PyArrow's safe cast can't handle.

    Returns a typed Array on success, None on failure. None signals the
    caller to keep the original column (with a warning logged here).
    """
    is_string_src = pa.types.is_string(col.type) or pa.types.is_large_string(col.type)

    if is_string_src and pa.types.is_timestamp(target):
        try:
            series = col.to_pandas()
            parsed = pd.to_datetime(series, errors="coerce", utc=True)
            naive = parsed.dt.tz_convert(None)
            return pa.Array.from_pandas(naive, type=target)
        except Exception as e:
            logger.warning(
                "Column %r: cannot cast %s to %s, keeping original. Error: %s",
                name, col.type, target, e,
            )
            return None

    if is_string_src and (pa.types.is_floating(target) or pa.types.is_integer(target)):
        try:
            series = col.to_pandas()
            converted = pd.to_numeric(series, errors="coerce")
            return pa.Array.from_pandas(converted, type=target)
        except Exception as e:
            logger.warning(
                "Column %r: cannot cast %s to %s, keeping original. Error: %s",
                name, col.type, target, e,
            )
            return None

    logger.warning(
        "Column %r: cannot cast %s to %s, keeping original. Error: %s",
        name, col.type, target, cast_err,
    )
    return None


# ───────────────────────────── per-column conversion ──────────────────────────


_BOOL_MAP = {
    "true": True, "false": False, "True": True, "False": False,
    "TRUE": True, "FALSE": False, "1": True, "0": False,
    "yes": True, "no": False, "Yes": True, "No": False,
    "YES": True, "NO": False,
}


def _convert_column(series: pd.Series, dtype: str, col_name: str = "") -> pd.Series:
    """Convert a pandas Series to `dtype`.

    Empty strings become NA for non-string targets so nullable Int64/float64
    columns don't reject them. Numeric/boolean conversions log invalid
    values via `errors='coerce'` semantics. Examples (up to 3) are
    surfaced in the warning so admins can spot patterns like 'Non-Manager'
    showing up in a numeric column.
    """
    if dtype != "object":
        series = series.replace("", pd.NA)

    if dtype in ("Int64", "float64", "Float64"):
        non_null_before = int(series.notna().sum())
        converted = pd.to_numeric(series, errors="coerce")
        invalid = non_null_before - int(converted.notna().sum())
        if invalid > 0:
            mask = series.notna() & converted.isna()
            logger.warning(
                "Column %r: %d invalid numeric values → NULL. Examples: %s",
                col_name, invalid, series[mask].head(3).tolist(),
            )
        return converted.astype(dtype)

    if dtype == "boolean":
        non_na = series.dropna()
        unknown = non_na[~non_na.isin(_BOOL_MAP.keys())]
        if len(unknown) > 0:
            logger.warning(
                "Column %r: %d unknown boolean values → NULL. Examples: %s",
                col_name, len(unknown), unknown.head(3).tolist(),
            )
        return series.map(_BOOL_MAP).astype(dtype)

    return series.astype(dtype)


# ───────────────────────────── CSV → Parquet ──────────────────────────────────


def csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    dtypes: Optional[Dict[str, str]] = None,
    date_columns: Optional[List[str]] = None,
    pyarrow_schema: Optional[pa.Schema] = None,
    table_id: Optional[str] = None,
) -> Dict[str, int]:
    """Convert a Keboola CSV export to a typed Parquet file.

    Loads with `dtype=str` (no pandas type guessing), then casts per
    `dtypes` (pandas dtype map from `KeboolaClient.get_pandas_dtypes`),
    converts the listed `date_columns` to `date32`, optionally applies an
    explicit `pyarrow_schema` last (handles all-null columns and
    string-with-tz timestamps), and writes snappy-compressed.

    `table_id` is embedded in parquet metadata for traceability.
    """
    csv_path = Path(csv_path)
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(csv_path, dtype=str)

    if dtypes:
        for col, dtype in dtypes.items():
            if col not in df.columns or "datetime" in dtype:
                continue
            try:
                df[col] = _convert_column(df[col], dtype, col_name=col)
            except Exception as e:
                logger.warning("Failed to apply dtype %s to column %r: %s", dtype, col, e)

    table = pa.Table.from_pandas(df, preserve_index=False)

    if date_columns:
        table = convert_date_columns_to_date32(table, date_columns)

    if pyarrow_schema is not None:
        table = apply_schema_to_table(table, pyarrow_schema)

    if table_id:
        existing = table.schema.metadata or {}
        merged = dict(existing)
        merged[b"table_id"] = table_id.encode()
        table = table.replace_schema_metadata(merged)

    pq.write_table(table, parquet_path, compression="snappy")

    return {
        "rows": table.num_rows,
        "columns": table.num_columns,
        "parquet_size_bytes": parquet_path.stat().st_size,
    }
