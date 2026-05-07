"""Tests for the Keboola materialize_query path.

Surface contract: takes ``bucket`` + ``source_table`` (+ optional
``source_query`` JSON filter spec), exports via Storage API, writes a
parquet, returns the same {table_id, path, rows, bytes, md5} shape the
BQ branch returns. We mock `KeboolaStorageClient` so tests don't hit
the network — the real Storage API client is exercised in
tests/test_keboola_storage_api.py.

The default code path is now **parquet** (Storage API serves Snowflake
UNLOAD output directly; the extractor renames into place — no CSV
intermediate, no DuckDB COPY of full file). Tests cover both the
default parquet path and the legacy CSV opt-in (via
``source_query='{"file_type":"csv"}'``).
"""
import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from connectors.keboola import extractor as kbe


def _write_parquet(dest: Path, n_rows: int = 2) -> None:
    """Drop a tiny real parquet at ``dest`` so the materialize path can
    read it back to compute row_count + MD5 — same shape Snowflake
    UNLOAD would produce."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe = str(dest).replace("'", "''")
    conn = duckdb.connect()
    try:
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {','.join('(' + str(i) + ')' for i in range(n_rows))}) AS t(id)) "
            f"TO '{safe}' (FORMAT PARQUET)"
        )
    finally:
        conn.close()


def _seed_csv(dest: Path, header: str, rows: list[str]) -> None:
    """Write a tiny CSV the legacy CSV materialize path will convert to parquet."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def fake_storage_client_parquet():
    """Mock for the **default** parquet path. ``prepare_export`` returns a
    file_info marking a single (non-sliced) file. ``download_file``
    writes a real 2-row parquet at the requested dest."""
    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100,
            "file_id": 200,
            "rows": 2,
            "file_info": {"id": 200, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        _write_parquet(Path(dest_path), n_rows=2)
        return Path(dest_path)

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download
    return client


@pytest.fixture
def fake_storage_client_csv():
    """Mock for the legacy CSV opt-in path. ``export_table`` writes a
    small CSV at dest. Used for tests that pin
    ``source_query='{"file_type":"csv"}'``."""
    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(Path(dest), "id,name", ["1,alpha", "2,beta"])
        return {"job_id": 100, "file_id": 200, "rows": 2,
                "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.export_table.side_effect = fake_export
    return client


# ---- default parquet path --------------------------------------------------

def test_materialize_query_writes_parquet_and_returns_metadata(
    tmp_path, fake_storage_client_parquet
):
    """Default path: no source_query → file_type=parquet, single file."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="example_subset",
        bucket="in.c-sales",
        source_table="orders",
        source_query=None,
        storage_client=fake_storage_client_parquet,
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

    # Default file_type should be parquet — verify by inspecting the
    # ExportFilter passed to prepare_export.
    call_args = fake_storage_client_parquet.prepare_export.call_args
    assert call_args.args[0] == "in.c-sales.orders"
    assert call_args.kwargs["export_filter"].file_type == "parquet"


def test_materialize_query_parquet_sliced_merges_via_duckdb(tmp_path):
    """Sliced parquet output: each slice is itself a complete parquet file
    (Snowflake UNLOAD MAX_FILE_SIZE behavior). The extractor must use
    ``download_file_slices`` to keep them as separate files, then
    DuckDB-COPY across ``read_parquet([slice1, slice2])`` to merge —
    naive concat would corrupt the per-slice footer."""
    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 100, "file_id": 200, "rows": 4,
            "file_info": {"id": 200, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def fake_download_slices(file_info, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        s1, s2 = dest_dir / "slice-00000", dest_dir / "slice-00001"
        _write_parquet(s1, n_rows=2)
        _write_parquet(s2, n_rows=2)
        return [s1, s2]

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = fake_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="big_table",
        bucket="in.c-x", source_table="t",
        source_query=None,
        storage_client=client,
        output_dir=output_dir,
    )

    # Final parquet contains all 4 rows from both slices.
    final = output_dir / "big_table.parquet"
    assert final.exists()
    n = duckdb.connect().execute(
        f"SELECT COUNT(*) FROM read_parquet('{str(final).replace(chr(39), chr(39)*2)}')"
    ).fetchone()[0]
    assert n == 4
    assert result["rows"] == 4

    # Slices were not concatenated raw (would leave 2 footers in one file
    # and break DuckDB on read).
    client.download_file_slices.assert_called_once()


def test_materialize_query_parquet_zero_rows_emits_empty_parquet(tmp_path, caplog):
    """Storage API parquet succeeded but the filter matched 0 rows (file
    is empty/missing). We log a warning and emit an empty placeholder."""
    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1, "file_id": 2, "rows": 0,
            "file_info": {"id": 2, "url": "https://fake/x", "isSliced": False},
            "file_type": "parquet",
        }

    def fake_download(file_info, dest_path):
        # Don't create the file — simulates no-rows result.
        return Path(dest_path)

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file.side_effect = fake_download

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


def test_materialize_query_admin_can_pin_file_type_csv(tmp_path, fake_storage_client_csv):
    """Admin can opt out of parquet via ``source_query='{"file_type":"csv"}'``
    — falls back to CSV → DuckDB-COPY → parquet."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = kbe.materialize_query(
        table_id="legacy_csv",
        bucket="in.c-x", source_table="t",
        source_query='{"file_type": "csv"}',
        storage_client=fake_storage_client_csv,
        output_dir=output_dir,
    )

    assert (output_dir / "legacy_csv.parquet").exists()
    assert result["rows"] == 2

    # Storage client called with file_type=csv on the ExportFilter.
    call = fake_storage_client_csv.export_table.call_args
    assert call.args[0] == "in.c-x.t"
    assert call.kwargs["export_filter"].file_type == "csv"


# ---- tempdir cleanup on failure --------------------------------------------

def test_materialize_query_sliced_parquet_tempdir_cleaned_on_exception(tmp_path):
    """When a sliced parquet download raises mid-flight (e.g. OSError 28
    'No space left'), the per-call tempdir at /tmp/kbc-export-<id>-*
    that was already populated with downloaded slices must not survive.

    Regression: an earlier worker death mid-write left a 12 GiB stale
    slice tree on the boot disk because TemporaryDirectory's default
    cleanup path itself raised under disk-full state, masking the
    original exception AND leaving the dir behind. The fix uses
    ``ignore_cleanup_errors=True`` so cleanup is best-effort but always
    fires — the dir is empty (or at least mostly) after the function
    returns."""
    captured_tmpdir: dict[str, Path] = {}

    def fake_prepare(table_id, *, export_filter=None, export_timeout=None):
        return {
            "job_id": 1, "file_id": 2, "rows": 1,
            "file_info": {"id": 2, "url": "https://fake/manifest", "isSliced": True},
            "file_type": "parquet",
        }

    def boom_download_slices(file_info, dest_dir):
        # Capture the tempdir the extractor created (parent of dest_dir).
        captured_tmpdir["path"] = Path(dest_dir).parent
        # Simulate a real download writing partial state, then disk full.
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        (Path(dest_dir) / "slice-00000").write_bytes(b"PAR1...partial")
        raise OSError(28, "No space left on device")

    client = MagicMock()
    client.prepare_export.side_effect = fake_prepare
    client.download_file_slices.side_effect = boom_download_slices

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(OSError, match="No space left"):
        kbe.materialize_query(
            table_id="will_fail_sliced",
            bucket="in.c-test", source_table="t",
            source_query=None,
            storage_client=client,
            output_dir=output_dir,
        )

    # The tempdir that held the partial slice must be gone (or at least
    # not the half-populated state that leaked previously).
    assert "path" in captured_tmpdir, "download_file_slices was not invoked"
    leftover = captured_tmpdir["path"]
    assert not leftover.exists(), (
        f"tempdir {leftover} must be cleaned on exception "
        f"(otherwise leaks under disk-full conditions)"
    )
    # Final parquet must NOT exist.
    assert not (output_dir / "will_fail_sliced.parquet").exists()


# ---- generic guards (file_type-agnostic) -----------------------------------

def test_materialize_query_rejects_unsafe_table_id(tmp_path, fake_storage_client_parquet):
    """Defense: table_id is interpolated into the parquet filename. SQL/
    path-traversal-unsafe values must be rejected up-front."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="table_id"):
        kbe.materialize_query(
            table_id="../../etc/passwd",
            bucket="in.c-test", source_table="t",
            source_query=None,
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )


def test_materialize_query_invalid_source_query_json_raises(tmp_path, fake_storage_client_parquet):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="not valid JSON"):
        kbe.materialize_query(
            table_id="bad_filter",
            bucket="in.c-test", source_table="t",
            source_query="this is not json",
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )


def test_materialize_query_passes_filter_spec_to_export(tmp_path, fake_storage_client_parquet):
    """source_query JSON is parsed into ExportFilter and forwarded to the
    Storage API client. Verifies the dispatch shape — the actual
    filter→params conversion is covered in test_keboola_storage_api.py."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    kbe.materialize_query(
        table_id="filtered",
        bucket="in.c-sales", source_table="orders",
        source_query=(
            '{"where_filters": [{"column": "status", "operator": "eq", '
            '"values": ["open"]}], "columns": ["id"]}'
        ),
        storage_client=fake_storage_client_parquet,
        output_dir=output_dir,
    )

    f = fake_storage_client_parquet.prepare_export.call_args.kwargs["export_filter"]
    assert f.where_filters == [
        {"column": "status", "operator": "eq", "values": ["open"]}
    ]
    assert f.columns == ["id"]
    # No explicit file_type → defaults to parquet.
    assert f.file_type == "parquet"


# ---- atomic write contract -------------------------------------------------

def test_keboola_materialize_atomic_write_on_failure(tmp_path):
    """If the CSV→parquet conversion fails (legacy CSV opt-in), no
    partial file is left at the final .parquet path AND the .parquet.tmp
    staging file is cleaned up."""
    def fake_export(table_id, dest, *, export_filter=None, export_timeout=None):
        _seed_csv(Path(dest), "id,name", ["1,alpha"])
        return {"job_id": 1, "file_id": 2, "rows": 1,
                "bytes": Path(dest).stat().st_size, "file_type": "csv"}

    client = MagicMock()
    client.export_table.side_effect = fake_export

    output_dir = tmp_path / "data"
    output_dir.mkdir()

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
                source_query='{"file_type": "csv"}',
                storage_client=client,
                output_dir=output_dir,
            )

    final_path = output_dir / "atomic_test.parquet"
    assert not final_path.exists(), (
        f"Partial parquet left at final path {final_path} — orchestrator "
        f"rebuild would pick this up and serve corrupt data."
    )
    tmp_marker = output_dir / "atomic_test.parquet.tmp"
    assert not tmp_marker.exists(), f"Stale .parquet.tmp left at {tmp_marker}"


def test_keboola_materialize_uses_tmp_path_during_copy(tmp_path, fake_storage_client_parquet):
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
            storage_client=fake_storage_client_parquet,
            output_dir=output_dir,
        )

    assert captured["src"].endswith(".parquet.tmp"), captured
    assert captured["dst"].endswith(".parquet") and not captured["dst"].endswith(".tmp")

    assert (output_dir / "tmp_path_test.parquet").exists()
    assert not (output_dir / "tmp_path_test.parquet.tmp").exists()
    assert result["path"].endswith(".parquet")
    assert not result["path"].endswith(".tmp")
