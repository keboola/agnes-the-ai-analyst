"""Pure-function tests for incremental sync helpers."""
import csv as _csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# ───────────────────────────── compute_changed_since ──────────────────────────


def test_subsequent_sync_uses_last_sync_minus_window():
    from connectors.keboola.incremental import compute_changed_since

    last_sync = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = compute_changed_since(
        last_sync=last_sync, window_days=7, max_history_days=None,
        now=last_sync + timedelta(days=1),
    )
    assert result == "2026-04-24T12:00:00+00:00"


def test_subsequent_sync_default_window_is_seven():
    from connectors.keboola.incremental import compute_changed_since

    last_sync = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = compute_changed_since(
        last_sync=last_sync, window_days=None, max_history_days=None, now=last_sync,
    )
    assert result == "2026-04-24T00:00:00+00:00"


def test_first_sync_no_max_history_returns_none():
    from connectors.keboola.incremental import compute_changed_since

    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = compute_changed_since(
        last_sync=None, window_days=7, max_history_days=None, now=now,
    )
    assert result is None


def test_first_sync_with_max_history_caps_to_now_minus_max():
    from connectors.keboola.incremental import compute_changed_since

    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = compute_changed_since(
        last_sync=None, window_days=7, max_history_days=180, now=now,
    )
    expected = (now - timedelta(days=180)).isoformat()
    assert result == expected


def test_negative_window_raises():
    from connectors.keboola.incremental import compute_changed_since

    last_sync = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="window_days"):
        compute_changed_since(
            last_sync=last_sync, window_days=-1, max_history_days=None, now=last_sync,
        )


# ───────────────────────────── merge_parquet ──────────────────────────────────


def _seed_parquet(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="snappy")


def _seed_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        if not rows:
            return
        writer = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_merge_inserts_new_rows(tmp_path):
    from connectors.keboola.incremental import merge_parquet

    schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1, "v": 10}, {"id": 2, "v": 20}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "3", "v": "30"}])

    merge_parquet(
        existing_parquet=pq_path,
        new_csv=csv_path,
        primary_key=["id"],
        dtypes={"id": "Int64", "v": "Int64"},
        date_columns=[],
        pyarrow_schema=schema,
    )

    out = pq.read_table(pq_path).to_pylist()
    assert sorted(o["id"] for o in out) == [1, 2, 3]


def test_merge_updates_existing_row_keeping_last(tmp_path):
    from connectors.keboola.incremental import merge_parquet

    schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1, "v": 10}, {"id": 2, "v": 20}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "2", "v": "999"}, {"id": "3", "v": "30"}])

    merge_parquet(
        existing_parquet=pq_path,
        new_csv=csv_path,
        primary_key=["id"],
        dtypes={"id": "Int64", "v": "Int64"},
        date_columns=[],
        pyarrow_schema=schema,
    )

    out = sorted(pq.read_table(pq_path).to_pylist(), key=lambda r: r["id"])
    assert out == [{"id": 1, "v": 10}, {"id": 2, "v": 999}, {"id": 3, "v": 30}]


def test_merge_composite_primary_key(tmp_path):
    from connectors.keboola.incremental import merge_parquet

    schema = pa.schema([
        pa.field("a", pa.int64()), pa.field("b", pa.int64()), pa.field("v", pa.int64()),
    ])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"a": 1, "b": 1, "v": 1}, {"a": 1, "b": 2, "v": 2}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"a": "1", "b": "2", "v": "999"}])

    merge_parquet(
        existing_parquet=pq_path,
        new_csv=csv_path,
        primary_key=["a", "b"],
        dtypes={"a": "Int64", "b": "Int64", "v": "Int64"},
        date_columns=[],
        pyarrow_schema=schema,
    )

    out = sorted(pq.read_table(pq_path).to_pylist(), key=lambda r: (r["a"], r["b"]))
    assert out == [{"a": 1, "b": 1, "v": 1}, {"a": 1, "b": 2, "v": 999}]


def test_merge_without_primary_key_appends_without_dedup(tmp_path, caplog):
    """Per legacy behavior, missing PK = pure append. Operator's responsibility."""
    from connectors.keboola.incremental import merge_parquet

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1}, {"id": 2}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "2"}, {"id": "3"}])

    import logging
    with caplog.at_level(logging.WARNING):
        merge_parquet(
            existing_parquet=pq_path,
            new_csv=csv_path,
            primary_key=[],
            dtypes={"id": "Int64"},
            date_columns=[],
            pyarrow_schema=schema,
        )

    out = pq.read_table(pq_path).to_pylist()
    assert len(out) == 4  # 2 existing + 2 new, includes duplicate id=2
    assert "no primary_key" in caplog.text.lower()


def test_merge_atomic_on_failure(tmp_path, monkeypatch):
    """If write fails mid-flight, the existing parquet must remain intact."""
    from connectors.keboola.incremental import merge_parquet

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1}], schema)
    original_bytes = pq_path.read_bytes()

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "2"}])

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr("pyarrow.parquet.write_table", boom)

    with pytest.raises(RuntimeError, match="disk full"):
        merge_parquet(
            existing_parquet=pq_path,
            new_csv=csv_path,
            primary_key=["id"],
            dtypes={"id": "Int64"},
            date_columns=[],
            pyarrow_schema=schema,
        )

    assert pq_path.read_bytes() == original_bytes


def test_merge_pk_dtype_conversion_failure_raises_hard(tmp_path, monkeypatch):
    """Devin Review finding 0004 regression guard.

    If `_convert_column` fails for a primary_key column, the merge must raise
    rather than warn-and-continue. The pre-fix behavior left the PK column as
    object/string in the delta while existing_df has it typed (e.g. int64),
    producing a mixed-type column after concat that silently broke
    `drop_duplicates` (int 1 != str '1' under Python equality)."""
    from connectors.keboola import incremental as _incremental

    schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1, "v": 10}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "2", "v": "20"}])

    def boom(series, dtype, col_name=""):
        raise ValueError(f"synthetic conversion failure on {col_name!r}")
    monkeypatch.setattr(_incremental, "_convert_column", boom)

    with pytest.raises(RuntimeError, match="PK column 'id' dtype conversion failed"):
        _incremental.merge_parquet(
            existing_parquet=pq_path,
            new_csv=csv_path,
            primary_key=["id"],
            dtypes={"id": "Int64", "v": "Int64"},
            date_columns=[],
            pyarrow_schema=schema,
        )


def test_merge_non_pk_dtype_conversion_failure_warns_and_continues(tmp_path, monkeypatch, caplog):
    """Inverse of the PK-fail guard: a non-PK dtype conversion failure is
    soft-handled (logged warning, delta column stays as string). Locks the
    asymmetric policy in `merge_parquet` — PK failures are load-bearing for
    dedup correctness, non-PK failures degrade gracefully (pyarrow_schema=None
    here mirrors the path used when Keboola Storage metadata is unavailable)."""
    from connectors.keboola import incremental as _incremental

    schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.string())])
    pq_path = tmp_path / "t.parquet"
    _seed_parquet(pq_path, [{"id": 1, "v": "10"}], schema)

    csv_path = tmp_path / "delta.csv"
    _seed_csv(csv_path, [{"id": "2", "v": "20"}])

    real_convert = _incremental._convert_column

    def selective_boom(series, dtype, col_name=""):
        if col_name == "v":
            raise ValueError("synthetic non-pk conversion failure")
        return real_convert(series, dtype, col_name=col_name)
    monkeypatch.setattr(_incremental, "_convert_column", selective_boom)

    import logging
    with caplog.at_level(logging.WARNING):
        _incremental.merge_parquet(
            existing_parquet=pq_path,
            new_csv=csv_path,
            primary_key=["id"],
            dtypes={"id": "Int64", "v": "Int64"},
            date_columns=[],
            pyarrow_schema=None,
        )

    assert "failed to apply dtype" in caplog.text.lower()
    out = sorted(pq.read_table(pq_path).to_pylist(), key=lambda r: r["id"])
    assert [r["id"] for r in out] == [1, 2]
