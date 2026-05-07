"""End-to-end partitioned sync with mocked Storage SDK."""
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def test_first_sync_chunked_writes_per_partition_files(tmp_path, monkeypatch):
    """Two-chunk history: latest chunk has rows, previous has rows, then 2 empty
    chunks stop the loop."""
    from connectors.keboola.partitioned import extract_partitioned
    from connectors.keboola.client import KeboolaClient

    chunk_payloads = iter([
        # most recent → oldest
        "id,date\n1,2026-05-01\n2,2026-05-15\n",
        "id,date\n3,2026-04-10\n",
        "id,date\n",  # empty 1
        "id,date\n",  # empty 2 — stop
    ])

    def fake_export(self, table_id, output_path, changed_since=None, changed_until=None, **kw):
        body = next(chunk_payloads)
        Path(output_path).write_text(body)
        rows = max(0, len(body.strip().split("\n")) - 1)
        return {"exported_rows": rows}

    fake_schema = pa.schema([
        pa.field("id", pa.int64()), pa.field("date", pa.date32()),
    ])

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {"id": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: ["date"])

    out_dir = tmp_path / "data" / "sales"
    result = extract_partitioned(
        table_config={
            "id": "in.c-sales.orders", "name": "orders",
            "bucket": "in.c-sales", "source_table": "orders",
            "primary_key": ["id"], "partition_by": "date",
            "partition_granularity": "month",
            "incremental_window_days": 1,
            "max_history_days": None,
            "initial_load_chunk_days": 30,
        },
        output_dir=out_dir,
        last_sync=None,
        keboola_url="https://kbc.example", keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    files = sorted(p.name for p in out_dir.glob("*.parquet"))
    assert files == ["2026_04.parquet", "2026_05.parquet"]
    assert result["rows"] == 3


def test_incremental_partitioned_merges_only_affected(tmp_path, monkeypatch):
    """Existing partitions for 2026_04 and 2026_05. Delta touches only 2026_05.
    2026_04's bytes must be unchanged."""
    from connectors.keboola.partitioned import extract_partitioned
    from connectors.keboola.client import KeboolaClient

    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("date", pa.date32()),
        pa.field("v", pa.int64()),
    ])
    out_dir = tmp_path / "data" / "sales"
    out_dir.mkdir(parents=True)

    # Seed 2026_04 (untouched by this delta)
    apr = out_dir / "2026_04.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"id": 100, "date": date(2026, 4, 1), "v": 1},
    ], schema=schema), apr, compression="snappy")
    apr_bytes_before = apr.read_bytes()

    # Seed 2026_05
    may = out_dir / "2026_05.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"id": 1, "date": date(2026, 5, 1), "v": 10},
    ], schema=schema), may, compression="snappy")

    delta_payload = "id,date,v\n1,2026-05-01,999\n2,2026-05-15,20\n"

    def fake_export(self, table_id, output_path, **kw):
        Path(output_path).write_text(delta_payload)
        return {"exported_rows": 2}

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes",
                        lambda self, tid: {"id": "Int64", "v": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: ["date"])

    extract_partitioned(
        table_config={
            "id": "in.c-sales.orders", "name": "orders",
            "bucket": "in.c-sales", "source_table": "orders",
            "primary_key": ["id"], "partition_by": "date",
            "partition_granularity": "month",
            "incremental_window_days": 1, "max_history_days": None,
            "initial_load_chunk_days": 30,
        },
        output_dir=out_dir,
        last_sync=datetime(2026, 5, 6, tzinfo=timezone.utc),
        keboola_url="https://kbc.example", keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    # 2026_04 bytes-identical (no read, no write)
    assert apr.read_bytes() == apr_bytes_before
    # 2026_05 has the updated v=999 row and the new id=2 row
    rows = sorted(pq.read_table(may).to_pylist(), key=lambda r: r["id"])
    assert len(rows) == 2
    assert rows[0]["v"] == 999
    assert rows[1]["id"] == 2


def test_zero_delta_is_noop_for_partitioned(tmp_path, monkeypatch):
    from connectors.keboola.partitioned import extract_partitioned
    from connectors.keboola.client import KeboolaClient

    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("date", pa.date32()),
    ])
    out_dir = tmp_path / "data" / "orders"
    out_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([
        {"id": 1, "date": date(2026, 5, 1)},
    ], schema=schema), out_dir / "2026_05.parquet", compression="snappy")

    def fake_export(self, table_id, output_path, **kw):
        Path(output_path).write_text("id,date\n")
        return {"exported_rows": 0}

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {"id": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: ["date"])

    result = extract_partitioned(
        table_config={
            "id": "in.c-sales.orders", "name": "orders",
            "bucket": "in.c-sales", "source_table": "orders",
            "primary_key": ["id"], "partition_by": "date",
            "partition_granularity": "month",
            "incremental_window_days": 1,
        },
        output_dir=out_dir,
        last_sync=datetime(2026, 5, 6, tzinfo=timezone.utc),
        keboola_url="https://kbc.example", keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    assert result["delta_rows"] == 0
    assert result["partitions_touched"] == 0
    assert result["rows"] == 1


def test_missing_partition_by_raises(tmp_path):
    from connectors.keboola.partitioned import extract_partitioned, InvalidPartitionConfigError

    out_dir = tmp_path / "data" / "x"
    with pytest.raises(InvalidPartitionConfigError, match="partition_by"):
        extract_partitioned(
            table_config={
                "id": "in.c-x.y", "name": "y",
                "bucket": "in.c-x", "source_table": "y",
                "partition_granularity": "month",
            },
            output_dir=out_dir,
            last_sync=None,
            keboola_url="https://kbc.example", keboola_token="tok",
            now=datetime(2026, 5, 7, tzinfo=timezone.utc),
        )
