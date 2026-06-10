"""KeboolaStorageClient — direct Storage API export-async path.

Replaces the previous DuckDB-extension materialize path (extension scan
broken on linked-bucket projects, see keboola/duckdb-extension#17). Tests
mock the requests.Session at the adapter level so we exercise the real
HTTP shapes (status codes, JSON bodies) without touching the network.
"""
from __future__ import annotations

import gzip
import json
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import connectors.keboola.storage_api as sapi
from connectors.keboola.storage_api import (
    FILE_TYPE_CSV,
    FILE_TYPE_PARQUET,
    ExportFilter,
    KeboolaStorageClient,
    StorageApiError,
    get_temp_root,
    sweep_orphaned_scratch,
)


# ---- ExportFilter ----------------------------------------------------------

class TestExportFilter:
    def test_empty_dict_means_full_table(self):
        f = ExportFilter.from_dict({})
        assert f.to_export_params() == {}

    def test_none_means_full_table(self):
        f = ExportFilter.from_dict(None)
        assert f.to_export_params() == {}

    def test_where_filters_columns_changed_since(self):
        f = ExportFilter.from_dict({
            "where_filters": [
                {"column": "status", "operator": "eq", "values": ["open"]},
            ],
            "columns": ["id", "status"],
            "changed_since": "2026-04-01",
        })
        params = f.to_export_params()
        assert params["whereFilters"] == [
            {"column": "status", "operator": "eq", "values": ["open"]}
        ]
        # Storage API takes columns as comma-joined string, not array — the
        # `kbcstorage` SDK does the same join, so match its wire format.
        assert params["columns"] == "id,status"
        assert params["changedSince"] == "2026-04-01"

    def test_where_filter_missing_keys_raises_with_context(self):
        f = ExportFilter.from_dict({
            "where_filters": [{"column": "x", "operator": "eq"}],  # no values
        })
        with pytest.raises(ValueError, match=r"missing fields.*\['values'\]"):
            f.to_export_params()

    def test_where_filter_values_must_be_list(self):
        f = ExportFilter.from_dict({
            "where_filters": [{"column": "x", "operator": "eq", "values": "open"}],
        })
        with pytest.raises(ValueError, match="values must be a list"):
            f.to_export_params()

    def test_default_file_type_is_csv_and_omits_param(self):
        # Wire-side default is csv — preserve old behavior for callers
        # that never set file_type.
        assert ExportFilter().file_type == FILE_TYPE_CSV
        assert "fileType" not in ExportFilter().to_export_params()

    def test_file_type_parquet_emits_fileType_param(self):
        f = ExportFilter(file_type=FILE_TYPE_PARQUET)
        assert f.to_export_params()["fileType"] == "parquet"

    def test_from_dict_reads_file_type_snake_case(self):
        f = ExportFilter.from_dict({"file_type": "parquet"})
        assert f.file_type == "parquet"
        assert f.to_export_params()["fileType"] == "parquet"

    def test_from_dict_reads_fileType_camel_case_alias(self):
        # Operators copying examples from Apiary docs ship the wire name.
        f = ExportFilter.from_dict({"fileType": "parquet"})
        assert f.file_type == "parquet"

    def test_from_dict_invalid_file_type_raises(self):
        with pytest.raises(ValueError, match="file_type"):
            ExportFilter.from_dict({"file_type": "orc"})


# ---- HTTP client low-level -------------------------------------------------

def _mock_response(status, body):
    """Build a fake `requests.Response`-like object."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestStorageClient:
    def test_init_normalises_trailing_slash(self):
        c = KeboolaStorageClient(url="https://kbc/", token="t")
        assert c.base.endswith("/v2/storage")
        assert "/" * 2 not in c.base.replace("https://", "")

    def test_init_rejects_missing_url_or_token(self):
        with pytest.raises(ValueError):
            KeboolaStorageClient(url="", token="t")
        with pytest.raises(ValueError):
            KeboolaStorageClient(url="https://kbc", token="")

    def test_post_sends_storage_api_token_header(self):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 42})
        c = KeboolaStorageClient(url="https://kbc", token="abc", session=sess)

        c.export_table_async("in.c-x.t", {"columns": "a"})

        sess.post.assert_called_once()
        kwargs = sess.post.call_args.kwargs
        assert kwargs["headers"]["X-StorageApi-Token"] == "abc"

    def test_post_4xx_redacts_token_in_error_message(self):
        # If the API echoes the token (or a proxy injects it), we must not
        # leak it into raised exceptions.
        sess = MagicMock()
        sess.post.return_value = _mock_response(
            403, {"detail": "rejected token=secrettoken123"}
        )
        c = KeboolaStorageClient(url="https://kbc", token="secrettoken123", session=sess)

        with pytest.raises(StorageApiError) as e:
            c.export_table_async("in.c-x.t", {})

        assert "secrettoken123" not in str(e.value)
        assert "<redacted-storage-token>" in str(e.value)


# ---- wait_for_job ----------------------------------------------------------

class TestWaitForJob:
    def test_returns_on_success(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {
            "id": 1, "status": "success", "results": {"file": {"id": 99}},
        })
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        job = c.wait_for_job(1, timeout=5, poll_interval=0.01)
        assert job["status"] == "success"

    def test_raises_on_error_status(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {
            "id": 1, "status": "error", "error": {"message": "bad table"},
        })
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="reported error"):
            c.wait_for_job(1, timeout=5, poll_interval=0.01)

    def test_polls_until_terminal(self):
        # First two responses 'waiting', third 'success'. The client must
        # keep polling instead of giving up.
        sess = MagicMock()
        sess.get.side_effect = [
            _mock_response(200, {"id": 1, "status": "waiting"}),
            _mock_response(200, {"id": 1, "status": "processing"}),
            _mock_response(200, {"id": 1, "status": "success", "results": {"file": {"id": 7}}}),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        job = c.wait_for_job(1, timeout=5, poll_interval=0.01)
        assert job["status"] == "success"
        assert sess.get.call_count == 3

    def test_timeout_raises_with_job_id(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"id": 1, "status": "waiting"})
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="did not finish"):
            c.wait_for_job(1, timeout=0.1, poll_interval=0.05)


# ---- download_file ---------------------------------------------------------

class TestDownloadFile:
    def test_single_file_csv_passthrough(self, tmp_path):
        sess = MagicMock()
        # File detail returns a signed URL for a non-sliced .csv; download
        # streams it directly.
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        single_resp.iter_content.return_value = [b"col1,col2\n", b"a,1\n", b"b,2\n"]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file({
            "url": "https://signed/single.csv",
            "name": "single.csv",
            "isSliced": False,
        }, dest)

        assert dest.exists()
        assert dest.read_bytes() == b"col1,col2\na,1\nb,2\n"

    def test_single_file_gz_is_gunzipped(self, tmp_path):
        gzipped = BytesIO()
        with gzip.GzipFile(fileobj=gzipped, mode="wb") as gz:
            gz.write(b"col1,col2\nx,42\n")
        payload = gzipped.getvalue()

        sess = MagicMock()
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        single_resp.iter_content.return_value = [payload]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file({
            "url": "https://signed/single.csv.gz",
            "name": "single.csv.gz",
            "isSliced": False,
        }, dest)

        assert dest.read_bytes() == b"col1,col2\nx,42\n"

    def test_sliced_concat_in_order(self, tmp_path):
        # isSliced=True: detail.url points at a JSON manifest of slice URLs.
        # Simulate two slices: slice 0 (header + rows), slice 1 (more rows,
        # NO header per Storage API contract). We just concatenate bytes —
        # the contract test is "every slice's bytes appear in dest, in order".
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "https://signed/slice-0"},
                {"url": "https://signed/slice-1"},
            ]
        }
        manifest_resp.raise_for_status = MagicMock()

        slice0 = MagicMock()
        slice0.__enter__ = MagicMock(return_value=slice0)
        slice0.__exit__ = MagicMock(return_value=False)
        slice0.iter_content.return_value = [b"col\n", b"a\n"]
        slice0.raise_for_status = MagicMock()

        slice1 = MagicMock()
        slice1.__enter__ = MagicMock(return_value=slice1)
        slice1.__exit__ = MagicMock(return_value=False)
        slice1.iter_content.return_value = [b"b\n"]
        slice1.raise_for_status = MagicMock()

        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        c.download_file({
            "url": "https://signed/manifest.json",
            "name": "sliced",
            "isSliced": True,
        }, dest)

        assert dest.read_bytes() == b"col\na\nb\n"


# ---- end-to-end export_table_to_csv ---------------------------------------

class TestExportTableToCsv:
    def test_full_pipeline_calls_post_poll_detail_download(self, tmp_path):
        """Smoke: export-async → wait_for_job → file_detail → download.
        Mock the session at the boundary; assert the URL composition and
        order of operations match the contract. The actual bytes-written
        path is covered by TestDownloadFile."""
        sess = MagicMock()

        # 1) POST /tables/X/export-async → {id: 100}
        export_resp = _mock_response(200, {"id": 100})

        # 2) GET /jobs/100 → success with file id 200
        job_resp = _mock_response(200, {
            "id": 100,
            "status": "success",
            "results": {"file": {"id": 200}, "totalRowsCount": 5},
        })

        # 3) GET /files/200?federationToken=1 → single non-sliced URL
        file_resp = _mock_response(200, {
            "url": "https://signed/file.csv",
            "name": "file.csv",
            "isSliced": False,
        })

        # 4) GET https://signed/file.csv (download)
        download_resp = MagicMock()
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)
        download_resp.iter_content.return_value = [b"col\n1\n"]
        download_resp.raise_for_status = MagicMock()

        # session.get is called for: jobs (poll), file detail, download.
        # session.post for the export-async kickoff.
        sess.post.return_value = export_resp
        sess.get.side_effect = [job_resp, file_resp, download_resp]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.csv"
        stats = c.export_table_to_csv(
            "in.c-x.t", dest,
            export_filter=ExportFilter(columns=["col"]),
        )

        assert dest.read_bytes() == b"col\n1\n"
        assert stats["job_id"] == 100
        assert stats["file_id"] == 200
        assert stats["rows"] == 5
        assert stats["bytes"] == len(b"col\n1\n")

        # Assert export-async POST URL composition + body shape
        post_url = sess.post.call_args.args[0]
        assert post_url == "https://kbc/v2/storage/tables/in.c-x.t/export-async"
        post_body = sess.post.call_args.kwargs["data"]
        assert post_body["columns"] == "col"

    def test_missing_job_id_in_response_is_typed_error(self):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {})  # no `id`
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="missing job id"):
            c.export_table_to_csv("in.c-x.t", Path("/tmp/x"))

    def test_missing_file_in_job_results_is_typed_error(self, tmp_path):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 1})
        sess.get.return_value = _mock_response(200, {
            "id": 1, "status": "success", "results": {},  # no `file`
        })
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="no result file"):
            c.export_table_to_csv("in.c-x.t", tmp_path / "x")


# ---- prepare_export + download_file_slices (parquet path) ------------------

class TestParquetPath:
    def test_parquet_request_emits_fileType_in_post_body(self, tmp_path):
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 100})
        sess.get.side_effect = [
            _mock_response(200, {
                "id": 100, "status": "success",
                "results": {"file": {"id": 200}, "totalRowsCount": 3},
            }),
            _mock_response(200, {
                "id": 200, "url": "https://signed/x.parquet",
                "name": "x.parquet", "isSliced": False,
            }),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        prep = c.prepare_export(
            "in.c-x.t",
            export_filter=ExportFilter(file_type=FILE_TYPE_PARQUET),
        )

        assert prep["file_type"] == "parquet"
        assert prep["file_info"]["isSliced"] is False
        assert sess.post.call_args.kwargs["data"]["fileType"] == "parquet"

    def test_export_table_rejects_sliced_parquet(self, tmp_path):
        """Concatenating sliced parquet would corrupt per-slice footers.
        ``export_table`` must fail loud and direct callers at
        ``download_file_slices``."""
        sess = MagicMock()
        sess.post.return_value = _mock_response(200, {"id": 1})
        sess.get.side_effect = [
            _mock_response(200, {
                "id": 1, "status": "success",
                "results": {"file": {"id": 2}},
            }),
            _mock_response(200, {
                "id": 2, "url": "https://signed/manifest.json",
                "name": "x.parquet", "isSliced": True,
            }),
        ]
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)

        with pytest.raises(StorageApiError, match="sliced parquet"):
            c.export_table(
                "in.c-x.t", tmp_path / "x.parquet",
                export_filter=ExportFilter(file_type=FILE_TYPE_PARQUET),
            )

    def test_download_file_slices_returns_per_slice_paths(self, tmp_path):
        sess = MagicMock()

        manifest_resp = MagicMock()
        manifest_resp.json.return_value = {
            "entries": [
                {"url": "https://signed/slice-0"},
                {"url": "https://signed/slice-1"},
            ],
        }
        manifest_resp.raise_for_status = MagicMock()

        def mk_chunk_resp(payload: bytes):
            r = MagicMock()
            r.__enter__ = MagicMock(return_value=r)
            r.__exit__ = MagicMock(return_value=False)
            r.iter_content.return_value = [payload]
            r.raise_for_status = MagicMock()
            return r

        slice0 = mk_chunk_resp(b"PAR1...slice0...")
        slice1 = mk_chunk_resp(b"PAR1...slice1...")
        sess.get.side_effect = [manifest_resp, slice0, slice1]

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        paths = c.download_file_slices(
            {"url": "https://signed/manifest.json", "isSliced": True,
             "name": "x.parquet"},
            tmp_path / "slices",
        )

        assert len(paths) == 2
        assert paths[0].read_bytes() == b"PAR1...slice0..."
        assert paths[1].read_bytes() == b"PAR1...slice1..."
        # Naming preserves manifest order — required for deterministic
        # downstream merge.
        assert paths[0].name < paths[1].name

    def test_download_file_slices_refuses_non_sliced(self):
        c = KeboolaStorageClient(url="https://kbc", token="t",
                                  session=MagicMock())
        with pytest.raises(StorageApiError, match="non-sliced"):
            c.download_file_slices(
                {"url": "https://x", "isSliced": False}, Path("/tmp/x"),
            )

    def test_get_temp_root_unset_returns_none(self, monkeypatch):
        """No env var → None → tempfile falls back to system default
        (typically /tmp). Preserves OSS-pre-fix behaviour for users
        who haven't set AGNES_TEMP_DIR."""
        monkeypatch.delenv("AGNES_TEMP_DIR", raising=False)
        assert get_temp_root() is None

    def test_get_temp_root_creates_dir_when_missing(self, monkeypatch, tmp_path):
        """First-time use: target dir doesn't yet exist; helper mkdirs
        it (non-recursive parents handled by exist_ok). Returns the
        absolute path so tempfile uses it as the parent for staging."""
        target = tmp_path / "agnes-tmp-fresh"
        assert not target.exists()
        monkeypatch.setenv("AGNES_TEMP_DIR", str(target))
        assert get_temp_root() == str(target)
        assert target.is_dir()

    def test_get_temp_root_existing_dir_reused(self, monkeypatch, tmp_path):
        target = tmp_path / "agnes-tmp-existing"
        target.mkdir()
        monkeypatch.setenv("AGNES_TEMP_DIR", str(target))
        assert get_temp_root() == str(target)

    def test_get_temp_root_unwritable_falls_back(self, monkeypatch, tmp_path, caplog):
        """Sandboxes / read-only mounts make the target uncreatable; the
        helper logs a warning and returns None so tempfile falls back
        to the system default rather than blowing up the sync run."""
        # Point at a path under a read-only parent that doesn't exist.
        unwritable = "/nonexistent/forbidden/agnes-tmp"
        monkeypatch.setenv("AGNES_TEMP_DIR", unwritable)
        with caplog.at_level("WARNING"):
            assert get_temp_root() is None
        assert any("AGNES_TEMP_DIR" in r.message for r in caplog.records)

    def test_get_temp_root_empty_string_treated_as_unset(self, monkeypatch):
        # Operator who left ``AGNES_TEMP_DIR=`` (empty) in .env doesn't
        # get an mkdir of "" — same as unset.
        monkeypatch.setenv("AGNES_TEMP_DIR", "")
        assert get_temp_root() is None

    def test_parquet_download_does_not_gunzip_plain_parquet(self, tmp_path):
        """Regression: previous heuristic flagged any unencrypted file as
        gzipped, which would corrupt parquet downloads at gunzip time.
        Verify a `.parquet` file is written through unmodified."""
        sess = MagicMock()
        single_resp = MagicMock()
        single_resp.__enter__ = MagicMock(return_value=single_resp)
        single_resp.__exit__ = MagicMock(return_value=False)
        # Real parquet magic bytes — not valid gzip, would crash gunzip.
        single_resp.iter_content.return_value = [b"PAR1\x00\x00\x00binary"]
        single_resp.raise_for_status = MagicMock()
        sess.get.return_value = single_resp

        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"
        c.download_file({
            "url": "https://signed/x.parquet",
            "name": "x.parquet",
            "isSliced": False,
            "isEncrypted": False,
        }, dest)

        assert dest.read_bytes() == b"PAR1\x00\x00\x00binary"


# ---- sweep_orphaned_scratch ------------------------------------------------

class TestSweepOrphanedScratch:
    """Orphaned ``kbc-export-*`` staging dirs are left behind only when a
    sync worker is hard-killed (SIGKILL/OOM/auto-upgrade container recreate)
    mid-export, so ``TemporaryDirectory.__exit__`` never ran. The sweep
    reclaims them on the next sync; age-gating protects an in-flight export.
    """

    def _mk_dir(self, parent: Path, name: str, age_seconds: float) -> Path:
        d = parent / name
        d.mkdir()
        (d / "slice0.parquet").write_bytes(b"PAR1junk")
        old = time.time() - age_seconds
        import os as _os
        _os.utime(d, (old, old))
        return d

    def test_removes_old_scratch_dirs(self, tmp_path):
        old = self._mk_dir(tmp_path, "kbc-export-foo-abc123", age_seconds=7200)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 1
        assert not old.exists()

    def test_removes_old_slice_dirs(self, tmp_path):
        """`kbc-slice-*` dirs (the sliced-CSV download path in
        `_download_sliced`) orphan on the same hard-kill and are swept too."""
        old = self._mk_dir(tmp_path, "kbc-slice-xyz789", age_seconds=7200)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 1
        assert not old.exists()

    def test_keeps_fresh_scratch_dir(self, tmp_path):
        """A dir younger than the threshold may belong to a concurrent
        in-flight export — never sweep it."""
        fresh = self._mk_dir(tmp_path, "kbc-export-bar-def456", age_seconds=10)
        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 0
        assert fresh.exists()

    def test_ignores_non_scratch_entries(self, tmp_path):
        """Only ``kbc-export-*`` dirs are swept; unrelated files/dirs in the
        temp root (the data disk also holds extracts/, state/, etc.) are
        never touched even when old."""
        keep_dir = self._mk_dir(tmp_path, "extracts", age_seconds=7200)
        keep_file = tmp_path / "kbc-export-not-a-dir.txt"
        keep_file.write_text("x")
        old_file = time.time() - 7200
        import os as _os
        _os.utime(keep_file, (old_file, old_file))

        removed = sweep_orphaned_scratch(root=str(tmp_path), max_age_seconds=3600)
        assert removed == 0
        assert keep_dir.exists()
        assert keep_file.exists()

    def test_none_root_is_noop(self):
        """No temp root configured (AGNES_TEMP_DIR unset) → nothing to sweep."""
        assert sweep_orphaned_scratch(root=None, max_age_seconds=3600) == 0

    def test_missing_root_is_noop(self, tmp_path):
        assert sweep_orphaned_scratch(
            root=str(tmp_path / "does-not-exist"), max_age_seconds=3600
        ) == 0

    def test_max_age_from_env_default(self, tmp_path, monkeypatch):
        """Threshold falls back to AGNES_SCRATCH_MAX_AGE_SEC when not passed."""
        monkeypatch.setenv("AGNES_SCRATCH_MAX_AGE_SEC", "100")
        old = self._mk_dir(tmp_path, "kbc-export-baz-ghi789", age_seconds=200)
        fresh = self._mk_dir(tmp_path, "kbc-export-qux-jkl012", age_seconds=10)
        removed = sweep_orphaned_scratch(root=str(tmp_path))
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()


# ---- get_table_info --------------------------------------------------------

class TestGetTableInfo:
    """`get_table_info` is a thin wrapper around the existing _get path
    so the metadata provider doesn't have to bleed `_get` out of the
    module (#155)."""

    def test_calls_storage_api_with_table_id(self, monkeypatch):
        from connectors.keboola.storage_api import KeboolaStorageClient

        captured = {}

        def fake_get(self, path, **kwargs):
            captured["path"] = path
            return {"rowsCount": 100, "dataSizeBytes": 4096}

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(
            url="https://connection.keboola.com", token="tok"
        )
        info = client.get_table_info("in.c-orders.events")
        assert captured["path"] == "/tables/in.c-orders.events"
        assert info["rowsCount"] == 100
        assert info["dataSizeBytes"] == 4096

    def test_propagates_storage_api_error(self, monkeypatch):
        from connectors.keboola.storage_api import (
            KeboolaStorageClient, StorageApiError,
        )

        def fake_get(self, path, **kwargs):
            raise StorageApiError("404 not found", status=404, body={})

        monkeypatch.setattr(KeboolaStorageClient, "_get", fake_get)

        client = KeboolaStorageClient(url="https://x", token="tok")
        import pytest
        with pytest.raises(StorageApiError):
            client.get_table_info("missing.table")


# ---- _download_single disk-space pre-flight (#431 / #432) ------------------

def _streaming_resp(*, headers, chunks):
    """Build a MagicMock that behaves like a streaming ``requests`` response
    used as a context manager: ``with session.get(...) as r``."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.raise_for_status = MagicMock()
    # Real dict so ``r.headers.get('Content-Length')`` returns the literal
    # value (or None) we control — a bare MagicMock would return a MagicMock
    # and silently fall through the pre-flight via the int() TypeError path.
    resp.headers = dict(headers)
    resp.iter_content = MagicMock(return_value=list(chunks))
    return resp


class TestDownloadDiskPreflight:
    @pytest.mark.parametrize(
        "gunzip_on_read, free, should_raise",
        [
            # expected_bytes = 1e9. non-gunzip needs 1.25x = 1.25e9;
            # gunzip needs 5x = 5e9. free=2e9 clears the 1.25x bar but
            # not the 5x bar -> pins the multiplier branch.
            (False, 2_000_000_000, False),
            (True, 2_000_000_000, True),
            # tiny free always fails, both branches.
            (False, 100, True),
            (True, 100, True),
        ],
    )
    def test_multiplier_branch(self, tmp_path, gunzip_on_read, free, should_raise):
        sess = MagicMock()
        resp = _streaming_resp(
            headers={"Content-Length": "1000000000"},
            chunks=[b"PAR1payload"],
        )
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        fake_usage = MagicMock(return_value=MagicMock(free=free))
        with patch.object(sapi.shutil, "disk_usage", fake_usage):
            if should_raise:
                with pytest.raises(StorageApiError, match="insufficient disk space"):
                    c._download_single(
                        "https://signed/x", dest, gunzip_on_read=gunzip_on_read
                    )
                # The raise must fire BEFORE the write loop.
                resp.iter_content.assert_not_called()
                assert not dest.exists()
            else:
                c._download_single(
                    "https://signed/x", dest, gunzip_on_read=gunzip_on_read
                )
                resp.iter_content.assert_called()
                assert dest.exists()

    def test_raises_storage_api_error_when_free_below_needed(self, tmp_path):
        """Insufficient free space -> StorageApiError raised BEFORE the write
        loop (iter_content never touched)."""
        sess = MagicMock()
        resp = _streaming_resp(
            headers={"Content-Length": "1000000000"},
            chunks=[b"PAR1payload"],
        )
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        with patch.object(
            sapi.shutil, "disk_usage", return_value=MagicMock(free=100)
        ):
            with pytest.raises(StorageApiError, match="insufficient disk space"):
                c._download_single(
                    "https://signed/x", dest, gunzip_on_read=False
                )
        resp.iter_content.assert_not_called()
        assert not dest.exists()

    def test_absent_content_length_falls_through(self, tmp_path):
        """No Content-Length header -> the whole pre-flight block is skipped:
        no exception, the file is written, and shutil.disk_usage is never
        called."""
        sess = MagicMock()
        resp = _streaming_resp(headers={}, chunks=[b"PAR1data"])
        sess.get.return_value = resp
        c = KeboolaStorageClient(url="https://kbc", token="t", session=sess)
        dest = tmp_path / "out.parquet"

        fake_usage = MagicMock()
        with patch.object(sapi.shutil, "disk_usage", fake_usage):
            c._download_single("https://signed/x", dest, gunzip_on_read=False)

        assert dest.exists()
        assert dest.read_bytes() == b"PAR1data"
        fake_usage.assert_not_called()
