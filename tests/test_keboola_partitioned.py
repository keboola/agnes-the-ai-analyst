"""Unit tests for partitioned sync helpers."""

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# ───────────────────────────── partition_key_for ──────────────────────────────


def test_partition_key_day():
    from connectors.keboola.partitioned import partition_key_for

    assert partition_key_for(date(2026, 5, 7), "day") == "2026_05_07"


def test_partition_key_month():
    from connectors.keboola.partitioned import partition_key_for

    assert partition_key_for(date(2026, 5, 7), "month") == "2026_05"


def test_partition_key_year():
    from connectors.keboola.partitioned import partition_key_for

    assert partition_key_for(date(2026, 5, 7), "year") == "2026"


def test_partition_key_accepts_datetime():
    from connectors.keboola.partitioned import partition_key_for

    assert partition_key_for(datetime(2026, 5, 7, 12, 30), "day") == "2026_05_07"


def test_partition_key_accepts_pandas_timestamp():
    from connectors.keboola.partitioned import partition_key_for

    assert partition_key_for(pd.Timestamp("2026-05-07"), "month") == "2026_05"


def test_invalid_granularity_raises():
    from connectors.keboola.partitioned import partition_key_for, InvalidPartitionConfigError

    with pytest.raises(InvalidPartitionConfigError, match="granularity"):
        partition_key_for(date(2026, 5, 7), "hour")


# ───────────────────────────── merge_partition ────────────────────────────────


def test_merge_partition_inserts_new_rows(tmp_path):
    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("date", pa.date32()),
            pa.field("v", pa.int64()),
        ]
    )
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"id": 1, "date": date(2026, 5, 1), "v": 10},
            ],
            schema=schema,
        ),
        pq_path,
        compression="snappy",
    )

    delta_df = pd.DataFrame([{"id": 2, "date": "2026-05-15", "v": 20}])
    merge_partition(
        partition_path=pq_path,
        delta_df=delta_df,
        primary_key=["id"],
        pyarrow_schema=schema,
        date_columns=["date"],
    )

    rows = sorted(pq.read_table(pq_path).to_pylist(), key=lambda r: r["id"])
    assert len(rows) == 2
    assert rows[1]["v"] == 20


def test_merge_partition_replaces_by_pk(tmp_path):
    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("date", pa.date32()),
            pa.field("v", pa.int64()),
        ]
    )
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"id": 1, "date": date(2026, 5, 1), "v": 10},
            ],
            schema=schema,
        ),
        pq_path,
        compression="snappy",
    )
    delta_df = pd.DataFrame([{"id": 1, "date": "2026-05-01", "v": 999}])
    merge_partition(
        partition_path=pq_path,
        delta_df=delta_df,
        primary_key=["id"],
        pyarrow_schema=schema,
        date_columns=["date"],
    )
    rows = pq.read_table(pq_path).to_pylist()
    assert len(rows) == 1
    assert rows[0]["v"] == 999


def test_merge_partition_creates_new_file_when_missing(tmp_path):
    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("date", pa.date32()),
        ]
    )
    pq_path = tmp_path / "2026_06.parquet"
    assert not pq_path.exists()

    delta_df = pd.DataFrame([{"id": 1, "date": "2026-06-01"}])
    merge_partition(
        partition_path=pq_path,
        delta_df=delta_df,
        primary_key=["id"],
        pyarrow_schema=schema,
        date_columns=["date"],
    )
    assert pq_path.exists()
    assert pq.read_table(pq_path).num_rows == 1


# ─────────────────────── atomic publish (#1359) ────────────────────────────
#
# merge_partition and connectors/keboola/incremental.py::merge_parquet were a
# verbatim-identical 9-line temp+os.replace block: a SHARED (non-per-process)
# temp name and no chmod — the exact #1274 / #203 defects
# `src.parquet_publish.atomic_publish` exists to prevent. Modeled on
# tests/test_parquet_publish.py / tests/test_jira_atomic_parquet_writes.py:
# `_boom` writes the footerless bytes a killed `pq.write_table` leaves AT
# WHATEVER PATH THE WRITER CHOSE, then raises — a stub that raises without
# touching the filesystem first (the ORIGINAL version of this test) would
# pass even against code that isn't atomic at all.

FOOTERLESS = b"PAR1" + b"\x00" * 64


def _boom(table, where, **kw):
    Path(where).write_bytes(FOOTERLESS)
    raise OSError("disk full mid-write")


def test_merge_partition_atomic_on_failure(tmp_path, monkeypatch):
    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1}], schema=schema), pq_path, compression="snappy")
    original = pq_path.read_bytes()

    monkeypatch.setattr("pyarrow.parquet.write_table", _boom)

    delta_df = pd.DataFrame([{"id": 2}])
    with pytest.raises(OSError):
        merge_partition(
            partition_path=pq_path,
            delta_df=delta_df,
            primary_key=["id"],
            pyarrow_schema=schema,
            date_columns=[],
        )
    assert pq_path.read_bytes() == original


def test_merge_partition_killed_write_leaves_no_temp_behind(tmp_path, monkeypatch):
    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1}], schema=schema), pq_path, compression="snappy")

    monkeypatch.setattr("pyarrow.parquet.write_table", _boom)

    delta_df = pd.DataFrame([{"id": 2}])
    with pytest.raises(OSError):
        merge_partition(
            partition_path=pq_path,
            delta_df=delta_df,
            primary_key=["id"],
            pyarrow_schema=schema,
            date_columns=[],
        )
    assert list(tmp_path.glob("*.tmp")) == []


def test_merge_partition_published_mode_is_0644_under_restrictive_umask(tmp_path):
    import os

    from connectors.keboola.partitioned import merge_partition

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1}], schema=schema), pq_path, compression="snappy")

    delta_df = pd.DataFrame([{"id": 2}])
    previous = os.umask(0o077)
    try:
        merge_partition(
            partition_path=pq_path,
            delta_df=delta_df,
            primary_key=["id"],
            pyarrow_schema=schema,
            date_columns=[],
        )
    finally:
        os.umask(previous)

    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_merge_partition_temp_is_per_process_and_never_matches_the_parquet_glob(tmp_path, monkeypatch):
    import os

    from connectors.keboola import partitioned as partitioned_mod

    schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "2026_05.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1}], schema=schema), pq_path, compression="snappy")

    seen: list[Path] = []
    real = partitioned_mod.pq.write_table

    def _record(table, where, **kw):
        seen.append(Path(where))
        return real(table, where, **kw)

    monkeypatch.setattr(partitioned_mod.pq, "write_table", _record)
    delta_df = pd.DataFrame([{"id": 2}])
    partitioned_mod.merge_partition(
        partition_path=pq_path,
        delta_df=delta_df,
        primary_key=["id"],
        pyarrow_schema=schema,
        date_columns=[],
    )

    assert seen, "write_table was never called"
    assert seen[0].name != "2026_05.parquet", "wrote straight onto the live path"
    assert str(os.getpid()) in seen[0].name
    assert [p.name for p in tmp_path.glob("*.parquet")] == ["2026_05.parquet"]
    assert not list(tmp_path.glob("*.parquet.*"))


# ───────────────────────────── process_csv_to_partitions ──────────────────────


def test_process_csv_to_partitions_groups_by_month(tmp_path):
    from connectors.keboola.partitioned import process_csv_to_partitions

    csv_path = tmp_path / "delta.csv"
    csv_path.write_text("id,date\n1,2026-05-01\n2,2026-05-15\n3,2026-06-02\n4,2026-06-20\n")
    groups = process_csv_to_partitions(
        csv_path=csv_path,
        partition_by="date",
        granularity="month",
        dtypes={"id": "Int64"},
    )
    assert set(groups.keys()) == {"2026_05", "2026_06"}
    assert len(groups["2026_05"]) == 2
    assert len(groups["2026_06"]) == 2


def test_process_csv_to_partitions_groups_by_day(tmp_path):
    from connectors.keboola.partitioned import process_csv_to_partitions

    csv_path = tmp_path / "delta.csv"
    csv_path.write_text("id,date\n1,2026-05-01\n2,2026-05-01\n3,2026-05-02\n")
    groups = process_csv_to_partitions(
        csv_path=csv_path,
        partition_by="date",
        granularity="day",
        dtypes={"id": "Int64"},
    )
    assert set(groups.keys()) == {"2026_05_01", "2026_05_02"}
    assert len(groups["2026_05_01"]) == 2


def test_process_csv_to_partitions_skips_unparseable(tmp_path, caplog):
    from connectors.keboola.partitioned import process_csv_to_partitions

    csv_path = tmp_path / "delta.csv"
    csv_path.write_text("id,date\n1,2026-05-01\n2,not-a-date\n3,0000-00-00\n")
    import logging

    with caplog.at_level(logging.WARNING):
        groups = process_csv_to_partitions(
            csv_path=csv_path,
            partition_by="date",
            granularity="month",
            dtypes={"id": "Int64"},
        )
    assert set(groups.keys()) == {"2026_05"}
    assert "2 rows with unparseable" in caplog.text


def test_process_csv_to_partitions_empty_csv(tmp_path):
    from connectors.keboola.partitioned import process_csv_to_partitions

    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("id,date\n")
    groups = process_csv_to_partitions(
        csv_path=csv_path,
        partition_by="date",
        granularity="month",
        dtypes={},
    )
    assert groups == {}


def test_process_csv_to_partitions_missing_partition_column_raises(tmp_path):
    from connectors.keboola.partitioned import process_csv_to_partitions, InvalidPartitionConfigError

    csv_path = tmp_path / "delta.csv"
    csv_path.write_text("id\n1\n")
    with pytest.raises(InvalidPartitionConfigError, match="partition_by column"):
        process_csv_to_partitions(
            csv_path=csv_path,
            partition_by="date",
            granularity="month",
            dtypes={},
        )


# ───────────────────────────── compute_chunk_windows ──────────────────────────


def test_compute_chunk_windows_with_max_history():
    from connectors.keboola.partitioned import compute_chunk_windows

    now = datetime(2026, 5, 7, tzinfo=timezone.utc)
    windows = compute_chunk_windows(
        now=now,
        chunk_days=30,
        max_history_days=90,
        overlap_days=1,
    )
    # 90 / 30 = 3 chunks, walking backwards from now
    assert len(windows) == 3
    sinces = [w[0] for w in windows]
    assert sinces == sorted(sinces, reverse=True)


def test_compute_chunk_windows_unbounded_caps_at_safety():
    from connectors.keboola.partitioned import (
        compute_chunk_windows,
        INITIAL_LOAD_MAX_CHUNKS_SAFETY,
    )

    now = datetime(2026, 5, 7, tzinfo=timezone.utc)
    windows = compute_chunk_windows(
        now=now,
        chunk_days=30,
        max_history_days=None,
        overlap_days=1,
    )
    assert len(windows) == INITIAL_LOAD_MAX_CHUNKS_SAFETY


def test_compute_chunk_windows_zero_history_returns_empty():
    from connectors.keboola.partitioned import compute_chunk_windows

    now = datetime(2026, 5, 7, tzinfo=timezone.utc)
    windows = compute_chunk_windows(
        now=now,
        chunk_days=30,
        max_history_days=0,
        overlap_days=1,
    )
    assert windows == []
