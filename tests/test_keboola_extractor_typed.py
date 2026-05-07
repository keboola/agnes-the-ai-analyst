"""Integration test: _extract_via_legacy produces typed parquet."""
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def test_legacy_path_writes_typed_parquet(tmp_path, monkeypatch):
    """When KeboolaClient.get_pyarrow_schema returns a schema, the parquet
    file must use those types — not VARCHAR."""
    from connectors.keboola.extractor import _extract_via_legacy

    csv_payload = "id,amount,created_on\n1,100,2025-01-15\n2,200,0000-00-00\n"

    def fake_export(self, table_id, output_path, **kwargs):
        Path(output_path).write_text(csv_payload)
        return {"exported_rows": 2}

    fake_schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("amount", pa.int64()),
        pa.field("created_on", pa.date32()),
    ])

    from connectors.keboola.client import KeboolaClient
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: fake_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {
        "id": "Int64", "amount": "Int64",
    })
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: ["created_on"])

    pq_path = tmp_path / "company.parquet"
    tc = {"name": "company", "bucket": "in.c-crm", "source_table": "company"}
    _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.int64()
    assert table.schema.field("created_on").type == pa.date32()
    out = table.to_pylist()
    assert out[0]["amount"] == 100
    assert out[1]["created_on"] is None  # 0000-00-00 → NULL


def test_legacy_path_falls_back_to_string_when_schema_unavailable(tmp_path, monkeypatch, caplog):
    """If get_pyarrow_schema raises (Storage API metadata unreachable), the
    legacy path must still produce a parquet — just with string types —
    rather than crash. The warning is logged for visibility."""
    from connectors.keboola.extractor import _extract_via_legacy

    csv_payload = "id,amount\n1,100\n2,200\n"

    def fake_export(self, table_id, output_path, **kwargs):
        Path(output_path).write_text(csv_payload)
        return {"exported_rows": 2}

    def boom_schema(self, table_id):
        raise RuntimeError("Storage API down")

    from connectors.keboola.client import KeboolaClient
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", boom_schema)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: [])

    pq_path = tmp_path / "company.parquet"
    tc = {"name": "company", "bucket": "in.c-crm", "source_table": "company"}

    import logging
    with caplog.at_level(logging.WARNING):
        _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.num_rows == 2
    assert "schema unavailable" in caplog.text.lower()


def test_legacy_path_with_no_metadata_returns_none_schema(tmp_path, monkeypatch):
    """When metadata API returns None (graceful — no exception, just no data),
    the legacy path skips schema enforcement and writes string-typed parquet."""
    from connectors.keboola.extractor import _extract_via_legacy

    csv_payload = "id,name\n1,foo\n"

    def fake_export(self, table_id, output_path, **kwargs):
        Path(output_path).write_text(csv_payload)
        return {"exported_rows": 1}

    from connectors.keboola.client import KeboolaClient
    monkeypatch.setattr(KeboolaClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(KeboolaClient, "export_table", fake_export)
    monkeypatch.setattr(KeboolaClient, "get_pyarrow_schema", lambda self, tid: None)
    monkeypatch.setattr(KeboolaClient, "get_pandas_dtypes", lambda self, tid: {})
    monkeypatch.setattr(KeboolaClient, "get_date_columns", lambda self, tid: [])

    pq_path = tmp_path / "out.parquet"
    tc = {"name": "company", "bucket": "in.c-crm", "source_table": "company"}
    _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    table = pq.read_table(pq_path)
    assert table.num_rows == 1
