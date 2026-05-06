"""Tests for the Keboola materialize_query path.

Surface contract: takes ``bucket`` + ``source_table`` (+ optional
``source_query`` JSON filter spec), exports via Storage API, writes a
parquet, returns the same {table_id, path, rows, bytes, md5} shape the
BQ branch returns. We mock `KeboolaStorageClient` so tests don't hit
the network — the real Storage API client is exercised in
tests/test_keboola_storage_api.py.
"""
import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from connectors.keboola import extractor as kbe


def _seed_csv(dest: Path, header: str, rows: list[str]) -> None:
    """Write a tiny CSV the materialize path will convert to parquet."""
    dest.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def fake_storage_client(tmp_path):
    """Return a MagicMock KeboolaStorageClient whose `export_table_to_csv`
    drops a small CSV at the requested dest path. Used as the
    `storage_client=` arg to materialize_query — bypasses real HTTP."""
    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(dest, "id,name", ["1,alpha", "2,beta"])
        return {"job_id": 100, "file_id": 200, "rows": 2, "bytes": dest.stat().st_size}

    client = MagicMock()
    client.export_table_to_csv.side_effect = fake_export
    return client


def test_materialize_query_writes_parquet_and_returns_metadata(
    tmp_path, fake_storage_client
):
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="example_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client,
        output_dir=output_dir,
    )

    parquet_path = output_dir / "example_subset.parquet"
    assert parquet_path.exists()
    assert result["table_id"] == "example_subset"
    assert result["path"] == str(parquet_path)
    assert result["rows"] == 2
    assert result["bytes"] > 0
    expected_md5 = hashlib.md5(parquet_path.read_bytes()).hexdigest()
    assert result["md5"] == expected_md5

    # Storage client was called with the bucket-qualified table id.
    call_args = fake_storage_client.export_table_to_csv.call_args
    assert call_args.args[0] == "in.c-sales.orders"


def test_materialize_query_zero_rows_emits_empty_parquet(tmp_path, caplog):
    """Storage API succeeded but the filter matched 0 rows. We log a
    warning and write an empty parquet so the orchestrator doesn't choke
    on a missing file."""
    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        # Do NOT create the CSV — simulates "no rows matched".
        return {"job_id": 1, "file_id": 2, "rows": 0, "bytes": 0}

    client = MagicMock()
    client.export_table_to_csv.side_effect = fake_export

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with caplog.at_level("WARNING"):
        result = kbe.materialize_query(
            table_id="empty_subset",
            bucket="in.c-test", source_table="empty",
            source_query=None,
            storage_client=client,
            output_dir=output_dir,
        )

    assert result["rows"] == 0
    assert (output_dir / "empty_subset.parquet").exists()
    assert "no data" in caplog.text.lower() or "0 rows" in caplog.text


def test_materialize_query_rejects_unsafe_table_id(tmp_path, fake_storage_client):
    """Defense: table_id is interpolated into the parquet filename. SQL/
    path-traversal-unsafe values must be rejected up-front."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="table_id"):
        kbe.materialize_query(
            table_id="../../etc/passwd",
            bucket="in.c-test", source_table="t",
            source_query=None,
            storage_client=fake_storage_client,
            output_dir=output_dir,
        )


def test_materialize_query_invalid_source_query_json_raises(tmp_path, fake_storage_client):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="not valid JSON"):
        kbe.materialize_query(
            table_id="bad_filter",
            bucket="in.c-test", source_table="t",
            source_query="this is not json",
            storage_client=fake_storage_client,
            output_dir=output_dir,
        )


def test_materialize_query_passes_filter_spec_to_export(tmp_path):
    """source_query JSON is parsed into ExportFilter and forwarded to the
    Storage API client. Verifies the dispatch shape — the actual
    filter→params conversion is covered in test_keboola_storage_api.py."""
    received_filter = {}

    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        received_filter["filter"] = export_filter
        _seed_csv(dest, "id", ["1"])
        return {"job_id": 1, "file_id": 2, "rows": 1, "bytes": dest.stat().st_size}

    client = MagicMock()
    client.export_table_to_csv.side_effect = fake_export

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="filtered",
        bucket="in.c-sales", source_table="orders",
        source_query='{"where_filters": [{"column": "status", "operator": "eq", "values": ["open"]}], "columns": ["id"]}',
        storage_client=client,
        output_dir=output_dir,
    )

    f = received_filter["filter"]
    assert f.where_filters == [
        {"column": "status", "operator": "eq", "values": ["open"]}
    ]
    assert f.columns == ["id"]


def test_keboola_materialize_atomic_write_on_failure(tmp_path):
    """If the CSV→parquet conversion fails, no partial file is left at the
    final .parquet path AND the .parquet.tmp staging file is cleaned up."""
    def fake_export_with_garbled_csv(table_id, dest, *, export_filter=None, export_timeout=None):
        # Write something that DuckDB read_csv accepts as 0 rows / valid
        # parquet target — then we simulate the conversion error by
        # patching duckdb.connect.execute to raise.
        _seed_csv(dest, "id,name", ["1,alpha"])
        return {"job_id": 1, "file_id": 2, "rows": 1, "bytes": dest.stat().st_size}

    client = MagicMock()
    client.export_table_to_csv.side_effect = fake_export_with_garbled_csv

    output_dir = tmp_path / "data"
    output_dir.mkdir()

    # DuckDBPyConnection.execute is read-only, so we wrap with a thin
    # tracing/failing proxy and patch the module-level `connect`.
    real_connect = duckdb.connect

    class FailingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "FORMAT PARQUET" in sql:
                raise RuntimeError("simulated mid-COPY failure")
            return self._inner.execute(sql, *a, **kw)

        def close(self):
            self._inner.close()

    def patched_connect(*args, **kwargs):
        return FailingConn(real_connect(*args, **kwargs))

    with patch("connectors.keboola.extractor.duckdb.connect", side_effect=patched_connect):
        with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
            kbe.materialize_query(
                table_id="atomic_test",
                bucket="in.c-test", source_table="t",
                source_query=None,
                storage_client=client,
                output_dir=output_dir,
            )

    # Final parquet must NOT exist (we never reached os.replace).
    final_path = output_dir / "atomic_test.parquet"
    assert not final_path.exists(), (
        f"Partial parquet left at final path {final_path} — orchestrator "
        f"rebuild would pick this up and serve corrupt data."
    )
    # tmp file also cleaned up.
    tmp_marker = output_dir / "atomic_test.parquet.tmp"
    assert not tmp_marker.exists(), f"Stale .parquet.tmp left at {tmp_marker}"


def test_keboola_materialize_uses_tmp_path_during_copy(tmp_path, fake_storage_client):
    """Atomic-write contract: parquet first lands at <id>.parquet.tmp, then
    is os.replaced into <id>.parquet on success. Verified by patching
    os.replace to capture the (src, dst) pair."""
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    captured = {}
    real_replace = os.replace

    def trace_replace(src, dst):
        captured["src"] = str(src)
        captured["dst"] = str(dst)
        real_replace(src, dst)

    with patch.object(kbe.os, "replace", side_effect=trace_replace):
        result = kbe.materialize_query(
            table_id="tmp_path_test",
            bucket="in.c-test", source_table="t",
            source_query=None,
            storage_client=fake_storage_client,
            output_dir=output_dir,
        )

    assert captured["src"].endswith(".parquet.tmp"), captured
    assert captured["dst"].endswith(".parquet") and not captured["dst"].endswith(".tmp")

    assert (output_dir / "tmp_path_test.parquet").exists()
    assert not (output_dir / "tmp_path_test.parquet.tmp").exists()
    assert result["path"].endswith(".parquet")
    assert not result["path"].endswith(".tmp")
