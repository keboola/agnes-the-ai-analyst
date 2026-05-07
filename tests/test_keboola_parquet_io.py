"""Tests for parquet_io helpers — typed parquet schema enforcement.

Ports the typed-schema parts of internal repo's `src/parquet_manager.py`
into the OSS Keboola legacy SDK extraction path. Three pure-function
helpers: convert_date_columns_to_date32, apply_schema_to_table, csv_to_parquet.
"""
import csv as _csv
import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# ───────────────────────────── convert_date_columns_to_date32 ─────────────────


def test_string_dates_become_date32():
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({
        "id": pa.array([1, 2, 3], type=pa.int64()),
        "created_on": pa.array(["2025-01-15", "2025-02-20", "2025-03-30"]),
    })
    result = convert_date_columns_to_date32(table, ["created_on"])
    assert result.schema.field("created_on").type == pa.date32()
    assert result.schema.field("id").type == pa.int64()


def test_invalid_date_becomes_null_keeping_date32(caplog):
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({
        "created_on": pa.array(["2025-01-15", "0000-00-00", "not-a-date"]),
    })
    with caplog.at_level(logging.WARNING):
        result = convert_date_columns_to_date32(table, ["created_on"])

    assert result.schema.field("created_on").type == pa.date32()
    col = result.column("created_on").to_pylist()
    assert col[0].isoformat() == "2025-01-15"
    assert col[1] is None
    assert col[2] is None
    assert "2 invalid date values" in caplog.text
    assert "0000-00-00" in caplog.text


def test_all_null_column_gets_typed_nulls():
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({
        "created_on": pa.array([None, None, None], type=pa.string()),
    })
    result = convert_date_columns_to_date32(table, ["created_on"])
    assert result.schema.field("created_on").type == pa.date32()
    assert result.column("created_on").null_count == 3


def test_already_timestamp_column_casts_to_date32():
    import datetime
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({
        "created_on": pa.array(
            [datetime.datetime(2025, 1, 15, 12, 30)],
            type=pa.timestamp("us"),
        ),
    })
    result = convert_date_columns_to_date32(table, ["created_on"])
    assert result.schema.field("created_on").type == pa.date32()
    assert result.column("created_on").to_pylist()[0].isoformat() == "2025-01-15"


def test_no_date_columns_listed_returns_unchanged():
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({"x": pa.array([1, 2, 3])})
    result = convert_date_columns_to_date32(table, [])
    assert result is table


def test_date_column_not_in_table_silently_ignored():
    from connectors.keboola.parquet_io import convert_date_columns_to_date32

    table = pa.table({"x": pa.array([1, 2])})
    result = convert_date_columns_to_date32(table, ["nonexistent"])
    assert result.schema.field("x").type == pa.int64()


# ───────────────────────────── apply_schema_to_table ──────────────────────────


def test_null_type_column_gets_target_type():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({"x": pa.nulls(3)})
    target = pa.schema([pa.field("x", pa.int64())])
    result = apply_schema_to_table(table, target)
    assert result.schema.field("x").type == pa.int64()
    assert result.column("x").null_count == 3


def test_string_to_timestamp_with_utc_suffix():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({
        "ts": pa.array(["2022-01-12T16:17:35.000Z", "2022-01-13T08:00:00.000Z"]),
    })
    target = pa.schema([pa.field("ts", pa.timestamp("us"))])
    result = apply_schema_to_table(table, target)
    assert result.schema.field("ts").type == pa.timestamp("us")
    out = result.column("ts").to_pylist()
    assert out[0].isoformat() == "2022-01-12T16:17:35"


def test_string_to_int_invalid_becomes_null():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({
        "amount": pa.array(["100", "200", "Non-Manager", "300"]),
    })
    target = pa.schema([pa.field("amount", pa.int64())])
    result = apply_schema_to_table(table, target)
    assert result.schema.field("amount").type == pa.int64()
    out = result.column("amount").to_pylist()
    assert out == [100, 200, None, 300]


def test_matching_type_kept_as_is():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({"x": pa.array([1, 2, 3], type=pa.int64())})
    target = pa.schema([pa.field("x", pa.int64())])
    result = apply_schema_to_table(table, target)
    assert result.column("x").to_pylist() == [1, 2, 3]


def test_column_not_in_target_kept_with_inferred_type():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({
        "x": pa.array([1, 2], type=pa.int64()),
        "extra": pa.array(["a", "b"]),
    })
    target = pa.schema([pa.field("x", pa.int64())])
    result = apply_schema_to_table(table, target)
    assert "extra" in result.column_names
    assert result.schema.field("extra").type == pa.string()


def test_uncastable_keeps_original_with_warning(caplog):
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({"x": pa.array(["abc", "def"])})
    target = pa.schema([pa.field("x", pa.bool_())])
    with caplog.at_level(logging.WARNING):
        result = apply_schema_to_table(table, target)
    assert result.schema.field("x").type == pa.string()
    assert "cannot cast" in caplog.text


def test_empty_target_schema_returns_table_unchanged():
    from connectors.keboola.parquet_io import apply_schema_to_table

    table = pa.table({"x": pa.array([1, 2])})
    result = apply_schema_to_table(table, pa.schema([]))
    assert result is table


# ───────────────────────────── csv_to_parquet ─────────────────────────────────


def _write_csv(tmp_path: Path, rows: list[dict]) -> Path:
    csv_path = tmp_path / "in.csv"
    with csv_path.open("w", newline="") as f:
        if not rows:
            return csv_path
        writer = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def test_int_column_typed_via_dtypes(tmp_path):
    from connectors.keboola.parquet_io import csv_to_parquet

    csv_path = _write_csv(tmp_path, [
        {"id": "1", "amount": "100"},
        {"id": "2", "amount": "200"},
        {"id": "3", "amount": ""},
    ])
    pq_path = tmp_path / "out.parquet"
    csv_to_parquet(
        csv_path=csv_path,
        parquet_path=pq_path,
        dtypes={"id": "Int64", "amount": "Int64"},
    )
    table = pq.read_table(pq_path)
    assert table.schema.field("amount").type == pa.int64()
    assert table.column("amount").to_pylist() == [100, 200, None]


def test_date_column_typed_via_date32(tmp_path):
    from connectors.keboola.parquet_io import csv_to_parquet

    csv_path = _write_csv(tmp_path, [
        {"id": "1", "created_on": "2025-01-15"},
        {"id": "2", "created_on": "0000-00-00"},
    ])
    pq_path = tmp_path / "out.parquet"
    csv_to_parquet(
        csv_path=csv_path,
        parquet_path=pq_path,
        dtypes={"id": "Int64"},
        date_columns=["created_on"],
    )
    table = pq.read_table(pq_path)
    assert table.schema.field("created_on").type == pa.date32()
    out = table.column("created_on").to_pylist()
    assert out[0].isoformat() == "2025-01-15"
    assert out[1] is None


def test_pyarrow_schema_overrides_inferred(tmp_path):
    from connectors.keboola.parquet_io import csv_to_parquet

    csv_path = _write_csv(tmp_path, [
        {"flag": "true"},
        {"flag": "false"},
        {"flag": ""},
    ])
    pq_path = tmp_path / "out.parquet"
    schema = pa.schema([pa.field("flag", pa.bool_())])
    csv_to_parquet(
        csv_path=csv_path,
        parquet_path=pq_path,
        dtypes={"flag": "boolean"},
        pyarrow_schema=schema,
    )
    table = pq.read_table(pq_path)
    assert table.schema.field("flag").type == pa.bool_()
    assert table.column("flag").to_pylist() == [True, False, None]


def test_missing_dtype_column_falls_through_as_string(tmp_path):
    from connectors.keboola.parquet_io import csv_to_parquet

    csv_path = _write_csv(tmp_path, [{"x": "abc", "y": "1"}])
    pq_path = tmp_path / "out.parquet"
    csv_to_parquet(
        csv_path=csv_path,
        parquet_path=pq_path,
        dtypes={"y": "Int64"},
    )
    table = pq.read_table(pq_path)
    # pyarrow may use string or large_string for object columns from pandas
    assert pa.types.is_string(table.schema.field("x").type) or pa.types.is_large_string(table.schema.field("x").type)
    assert table.schema.field("y").type == pa.int64()


def test_empty_csv_writes_empty_parquet(tmp_path):
    from connectors.keboola.parquet_io import csv_to_parquet

    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("id,amount\n")
    pq_path = tmp_path / "out.parquet"
    csv_to_parquet(
        csv_path=csv_path,
        parquet_path=pq_path,
        dtypes={"id": "Int64", "amount": "Int64"},
    )
    table = pq.read_table(pq_path)
    assert table.num_rows == 0
    assert table.schema.field("id").type == pa.int64()
