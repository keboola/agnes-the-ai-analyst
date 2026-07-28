"""End-to-end incremental sync with mocked Storage SDK."""
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# Optional kbcstorage dep — skip cleanly on installs that don't ship it.
# See tests/test_keboola_extractor_typed.py for the same pattern.
pytest.importorskip("kbcstorage")


def test_first_sync_writes_parquet(tmp_path, monkeypatch):
    from connectors.keboola.incremental import extract_incremental
    from connectors.keboola.client import KeboolaClient

    csv_payload = "id,v\n1,10\n2,20\n"

    def fake_export(self, table_id, output_path, changed_since=None, **kw):
        Path(output_path).write_text(csv_payload)
        return {"exported_rows": 2}

    fake_schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes",
                        lambda self, tid: {"id": "Int64", "v": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: [])

    pq_path = tmp_path / "data" / "company.parquet"

    result = extract_incremental(
        table_config={
            "id": "in.c-crm.company", "name": "company",
            "bucket": "in.c-crm", "source_table": "company",
            "primary_key": ["id"], "incremental_window_days": 1,
            "max_history_days": None,
        },
        parquet_path=pq_path,
        last_sync=None,
        keboola_url="https://kbc.example",
        keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    assert result["rows"] == 2
    assert result["delta_rows"] == 2
    assert result["changed_since_used"] is None
    table = pq.read_table(pq_path)
    assert table.schema.field("id").type == pa.int64()


def test_subsequent_sync_merges_into_existing(tmp_path, monkeypatch):
    from connectors.keboola.incremental import extract_incremental
    from connectors.keboola.client import KeboolaClient

    fake_schema = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])
    pq_path = tmp_path / "data" / "company.parquet"
    pq_path.parent.mkdir(parents=True)
    pa_table = pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "v": pa.array([10, 20], pa.int64()),
    })
    pq.write_table(pa_table, pq_path, compression="snappy")

    delta_payload = "id,v\n2,999\n3,30\n"
    captured = {}

    def fake_export(self, table_id, output_path, changed_since=None, **kw):
        captured["changed_since"] = changed_since
        Path(output_path).write_text(delta_payload)
        return {"exported_rows": 2}

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes",
                        lambda self, tid: {"id": "Int64", "v": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: [])

    last_sync = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    result = extract_incremental(
        table_config={
            "id": "in.c-crm.company", "name": "company",
            "bucket": "in.c-crm", "source_table": "company",
            "primary_key": ["id"], "incremental_window_days": 1,
            "max_history_days": None,
        },
        parquet_path=pq_path,
        last_sync=last_sync,
        keboola_url="https://kbc.example", keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    # changedSince = last_sync - 1 day window
    assert captured["changed_since"] == "2026-05-05T12:00:00+00:00"

    out = sorted(pq.read_table(pq_path).to_pylist(), key=lambda r: r["id"])
    assert out == [{"id": 1, "v": 10}, {"id": 2, "v": 999}, {"id": 3, "v": 30}]
    assert result["rows"] == 3
    assert result["delta_rows"] == 2


def test_zero_changes_is_noop(tmp_path, monkeypatch):
    from connectors.keboola.incremental import extract_incremental
    from connectors.keboola.client import KeboolaClient

    fake_schema = pa.schema([pa.field("id", pa.int64())])
    pq_path = tmp_path / "data" / "company.parquet"
    pq_path.parent.mkdir(parents=True)
    pa_table = pa.table({"id": pa.array([1, 2], pa.int64())})
    pq.write_table(pa_table, pq_path, compression="snappy")
    original_mtime = pq_path.stat().st_mtime
    original_bytes = pq_path.read_bytes()

    def fake_export(self, table_id, output_path, **kw):
        # Empty CSV — header only
        Path(output_path).write_text("id\n")
        return {"exported_rows": 0}

    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {"id": "Int64"})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: [])

    result = extract_incremental(
        table_config={
            "id": "in.c-crm.company", "name": "company",
            "bucket": "in.c-crm", "source_table": "company",
            "primary_key": ["id"], "incremental_window_days": 1,
        },
        parquet_path=pq_path,
        last_sync=datetime(2026, 5, 6, tzinfo=timezone.utc),
        keboola_url="https://kbc.example", keboola_token="tok",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )

    assert result["rows"] == 2
    assert result["delta_rows"] == 0
    assert pq_path.read_bytes() == original_bytes  # untouched
