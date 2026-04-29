"""Tests for da sync command."""

import hashlib
import json
import pytest
from unittest.mock import patch, MagicMock, call

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    return r


# Hash of the fake parquet payload below — matches what sync.py would compute.
_FAKE_PARQUET_BYTES = b"PAR1" + b"\x00" * 32 + b"PAR1"
_FAKE_PARQUET_MD5 = hashlib.md5(_FAKE_PARQUET_BYTES).hexdigest()

MANIFEST = {
    "tables": {
        # Hashes match _FAKE_PARQUET_BYTES so happy-path tests pass the
        # manifest-hash integrity check.
        "orders": {"hash": _FAKE_PARQUET_MD5, "rows": 100, "size_bytes": 2048},
        "customers": {"hash": _FAKE_PARQUET_MD5, "rows": 50, "size_bytes": 1024},
    }
}


def _fake_stream_download(path, target, *args, **kwargs):
    """Drop-in replacement for cli.commands.sync.stream_download that writes
    the well-known fake parquet to the target path."""
    with open(target, "wb") as f:
        f.write(_FAKE_PARQUET_BYTES)


class TestSyncHappyPath:
    def test_sync_downloads_all_tables(self, tmp_config):
        """Sync with no local state downloads all tables."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=_fake_stream_download) as mock_dl:
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 2
        assert "Downloaded: 2" in result.output

    def test_sync_specific_table(self, tmp_config):
        """--table flag limits download to one table."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=_fake_stream_download) as mock_dl:
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync", "--table", "orders"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 1
        call_path = mock_dl.call_args[0][0]
        assert "orders" in call_path

    def test_sync_json_output(self, tmp_config):
        """--json flag produces valid JSON output (rich spinner may precede JSON)."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=_fake_stream_download):
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync", "--json"])
        assert result.exit_code == 0
        # Rich Progress may output a spinner line before the JSON block
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert "downloaded" in data
        assert "errors" in data

    def test_sync_upload_only(self, tmp_config):
        """--upload-only skips download and calls upload."""
        with patch("cli.commands.sync.api_post", return_value=_resp(200)):
            result = runner.invoke(app, ["sync", "--upload-only"])
        assert result.exit_code == 0
        assert "session" in result.output.lower() or "upload" in result.output.lower()


class TestSyncErrors:
    def test_sync_manifest_failure(self, tmp_config):
        """Manifest fetch failure exits with error."""
        r = _resp(500)
        r.raise_for_status.side_effect = Exception("Server error")
        with patch("cli.commands.sync.api_get", return_value=r):
            result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "Failed to fetch manifest" in result.output

    def test_sync_download_error_recorded(self, tmp_config):
        """Download error is recorded in results but does not abort sync."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=Exception("timeout")):
                result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "Errors" in result.output

    def test_sync_skips_unchanged_tables(self, tmp_config, monkeypatch):
        """Tables with matching hashes are not re-downloaded."""
        state = {
            "tables": {
                "orders": {"hash": _FAKE_PARQUET_MD5},
                "customers": {"hash": _FAKE_PARQUET_MD5},
            }
        }
        with patch("cli.commands.sync.get_sync_state", return_value=state):
            with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
                with patch("cli.commands.sync.stream_download") as mock_dl:
                    result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        # Nothing to download — both hashes match
        assert mock_dl.call_count == 0
        assert "Downloaded: 0" in result.output


class TestFmtBytes:
    """_fmt_bytes must label magnitudes correctly — the fallback unit has
    to match the final loop exit, not be a fixed label."""

    def test_small_and_medium_sizes(self):
        from cli.commands.sync import _fmt_bytes
        assert _fmt_bytes(0) == "0 B"
        assert _fmt_bytes(512) == "512 B"
        assert _fmt_bytes(2048) == "2.0 KiB"
        assert _fmt_bytes(2 * 1024**2) == "2.0 MiB"
        assert _fmt_bytes(5 * 1024**3) == "5.0 GiB"
        assert _fmt_bytes(3 * 1024**4) == "3.0 TiB"

    def test_pib_and_eib_are_labelled_correctly(self):
        """Off-by-unit regression: 1 PiB must render as '1.0 PiB', not '1024.0 PiB'."""
        from cli.commands.sync import _fmt_bytes
        assert _fmt_bytes(1024**5) == "1.0 PiB"
        assert _fmt_bytes(2 * 1024**5) == "2.0 PiB"
        # Fallback unit at the very top.
        assert _fmt_bytes(1024**6) == "1.0 EiB"


class TestSyncDurability:
    """Durability & integrity layer: hash check, PAR1 fallback, broken-rebuild recovery."""

    def _write(self, tmp_config, tid: str, body: bytes) -> None:
        (tmp_config / "local" / "server" / "parquet").mkdir(parents=True, exist_ok=True)
        (tmp_config / "local" / "server" / "parquet" / f"{tid}.parquet").write_bytes(body)

    def test_hash_mismatch_recorded_as_error(self, tmp_config):
        """If manifest hash is present and does not match the downloaded bytes,
        the file must be discarded and the error recorded."""
        def bad_stream(path, target, *a, **kw):
            with open(target, "wb") as f:
                f.write(b"PAR1" + b"\xaa" * 50 + b"PAR1")  # valid PAR1, wrong hash

        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=bad_stream):
                with patch("cli.commands.sync._rebuild_duckdb_views") as mock_rebuild:
                    result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "Downloaded: 0" in result.output
        assert "Errors: 2" in result.output
        assert "hash mismatch" in result.output
        assert mock_rebuild.call_count == 0

    def test_par1_fallback_when_manifest_hash_missing(self, tmp_config):
        """Legacy manifests without `hash` must fall back to the PAR1 structural check."""
        manifest_no_hash = {"tables": {"orders": {"hash": "", "rows": 10, "size_bytes": 16}}}

        def html_stream(path, target, *a, **kw):
            with open(target, "wb") as f:
                f.write(b"<html>oops</html>")

        with patch("cli.commands.sync.api_get", return_value=_resp(200, manifest_no_hash)):
            with patch("cli.commands.sync.stream_download", side_effect=html_stream):
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync"])
        assert "PAR1" in result.output  # fallback message appears
        assert "Downloaded: 0" in result.output

    def test_rebuild_skips_broken_parquet_without_aborting(self, tmp_config):
        """Pre-existing broken parquet must not kill the whole rebuild."""
        self._write(tmp_config, "broken", b"not-parquet-at-all")
        self._write(tmp_config, "also_bad", b"PAR1" + b"\x00" * 10 + b"PAR1")

        from cli.commands.sync import _rebuild_duckdb_views
        local_dir = tmp_config / "local"
        parquet_dir = local_dir / "server" / "parquet"
        # Must not raise — both files are garbage but the function recovers.
        _rebuild_duckdb_views(local_dir, parquet_dir)


class TestStreamDownloadAtomicAndRetry:
    """stream_download: atomic tmp→rename, retries on transient errors, no retry on 4xx."""

    def test_atomic_write_via_tmp_then_rename(self, tmp_path, monkeypatch):
        """Target file must not exist before os.replace runs; writes go to .tmp first."""
        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DA_SERVER_URL", "http://localhost:9999")

        target = tmp_path / "x.parquet"
        observed_paths: list[str] = []

        class FakeStream:
            def __init__(self, chunks):
                self._chunks = chunks
            def raise_for_status(self): pass
            def iter_bytes(self, chunk_size=65536):
                # Observe target path at the moment of writing.
                observed_paths.append(str(target) + " exists=" + str(target.exists()))
                yield from self._chunks
            def __enter__(self): return self
            def __exit__(self, *a): pass

        class FakeClient:
            def __init__(self, *a, **kw): pass
            def stream(self, method, path): return FakeStream([b"PAR1", b"\x00" * 10, b"PAR1"])
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import cli.client as client_mod
        monkeypatch.setattr(client_mod, "get_client", lambda timeout=30.0: FakeClient())
        client_mod.stream_download("/ignored", str(target))
        assert target.exists()
        assert not (tmp_path / "x.parquet.tmp").exists()
        # The target did NOT exist while iter_bytes was pumping — only the .tmp did.
        assert all("exists=False" in p for p in observed_paths)

    def test_retries_on_transient_error(self, tmp_path, monkeypatch):
        """Transient network errors (ConnectError) trigger retry; eventual success is transparent."""
        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DA_SERVER_URL", "http://localhost:9999")
        monkeypatch.setenv("DA_STREAM_RETRIES", "3")

        target = tmp_path / "x.parquet"
        calls = {"n": 0}

        import httpx
        class FakeStream:
            def raise_for_status(self): pass
            def iter_bytes(self, chunk_size=65536):
                yield b"PAR1" + b"\x00" * 4 + b"PAR1"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        class FakeClient:
            def stream(self, method, path):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise httpx.ConnectError("flap")
                return FakeStream()
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import cli.client as client_mod
        monkeypatch.setattr(client_mod, "get_client", lambda timeout=30.0: FakeClient())
        # Speed up test — drop sleep to zero.
        monkeypatch.setattr(client_mod, "_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

        client_mod.stream_download("/ignored", str(target))
        assert calls["n"] == 3  # 2 failures + 1 success
        assert target.exists()

    def test_no_retry_on_4xx(self, tmp_path, monkeypatch):
        """4xx (auth, 404) must surface immediately — retries are for transient issues only."""
        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DA_SERVER_URL", "http://localhost:9999")

        import httpx
        calls = {"n": 0}

        class FakeResponse:
            status_code = 404
            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=MagicMock(status_code=404)
                )
            def iter_bytes(self, chunk_size=65536):
                return iter([])
            def __enter__(self): return self
            def __exit__(self, *a): pass

        class FakeClient:
            def stream(self, method, path):
                calls["n"] += 1
                return FakeResponse()
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import cli.client as client_mod
        monkeypatch.setattr(client_mod, "get_client", lambda timeout=30.0: FakeClient())
        monkeypatch.setattr(client_mod, "_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

        with pytest.raises(httpx.HTTPStatusError):
            client_mod.stream_download("/ignored", str(tmp_path / "x.parquet"))
        assert calls["n"] == 1  # no retry on 4xx


class TestSyncDryRun:
    def test_dry_run_skips_download_and_state_writes(self, tmp_config):
        """--dry-run must not call stream_download, save_sync_state, or _rebuild_duckdb_views."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download") as mock_dl:
                with patch("cli.commands.sync.save_sync_state") as mock_save:
                    with patch("cli.commands.sync._rebuild_duckdb_views") as mock_rebuild:
                        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 0
        assert mock_save.call_count == 0
        assert mock_rebuild.call_count == 0
        assert "Dry run" in result.output
        # Table ids from the MANIFEST fixture must show up in the plan.
        assert "orders" in result.output
        assert "customers" in result.output

    def test_dry_run_json_output_shape(self, tmp_config):
        """--dry-run --json emits a parseable plan with dry_run=True and a summary."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download"):
                result = runner.invoke(app, ["sync", "--dry-run", "--json"])
        assert result.exit_code == 0
        json_start = result.output.find("{")
        assert json_start >= 0
        # Rich Progress may emit additional lines after the JSON block, so use
        # raw_decode to stop at the object boundary.
        data, _ = json.JSONDecoder().raw_decode(result.output[json_start:])
        assert data["dry_run"] is True
        assert data["summary"]["tables_to_download"] == 2
        assert data["summary"]["bytes_total"] == 2048 + 1024
        tables = [row["table"] for row in data["would_download"]]
        assert set(tables) == {"orders", "customers"}

    def test_dry_run_respects_table_filter(self, tmp_config):
        """--dry-run --table X only lists that one table in the plan."""
        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download") as mock_dl:
                result = runner.invoke(app, ["sync", "--dry-run", "--table", "orders"])
        assert result.exit_code == 0
        assert mock_dl.call_count == 0
        assert "orders" in result.output
        assert "customers" not in result.output

    def test_dry_run_upload_only_does_not_hit_api(self, tmp_config):
        """--upload-only --dry-run must not call api_post."""
        with patch("cli.commands.sync.api_post") as mock_post:
            result = runner.invoke(app, ["sync", "--upload-only", "--dry-run"])
        assert result.exit_code == 0
        assert mock_post.call_count == 0
        assert "Dry run" in result.output or "would upload" in result.output.lower()


class TestSyncRespectsQueryMode:
    """`da sync` must skip query_mode='remote' tables — they have no parquet on the server."""

    def test_sync_skips_remote_query_mode_tables(self, tmp_config):
        """Mix of local + remote tables: only local downloaded, remote skipped with stderr summary."""
        manifest = {
            "tables": {
                "orders": {"hash": _FAKE_PARQUET_MD5, "query_mode": "local", "source_type": "keboola"},
                "bq_view": {"hash": "", "query_mode": "remote", "source_type": "bigquery"},
                "bq_table": {"hash": "", "query_mode": "remote", "source_type": "bigquery"},
            },
            "assets": {},
            "server_time": "2026-04-27T00:00:00Z",
        }

        called_downloads = []

        def fake_stream_download(path, target, *args, **kwargs):
            called_downloads.append(path)
            with open(target, "wb") as f:
                f.write(_FAKE_PARQUET_BYTES)

        with patch("cli.commands.sync.api_get", return_value=_resp(200, manifest)):
            with patch("cli.commands.sync.stream_download", side_effect=fake_stream_download):
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync"])

        # Only 'orders' should be downloaded
        downloaded_ids = [p.split("/")[-2] for p in called_downloads]
        assert "orders" in downloaded_ids, f"local 'orders' must be downloaded; got {called_downloads}"
        assert "bq_view" not in downloaded_ids, f"remote 'bq_view' must not be downloaded; got {called_downloads}"
        assert "bq_table" not in downloaded_ids, f"remote 'bq_table' must not be downloaded; got {called_downloads}"

        # Stderr/output should mention skipped remote tables
        out = result.output or ""
        assert "skip" in out.lower() or "remote" in out.lower(), \
            f"expected stderr summary mentioning skipped/remote tables; got: {out!r}"

        # Summary count separates unchanged vs remote-mode (Fix 1)
        assert "Skipped (remote-mode)" in out, \
            f"expected separate remote-mode summary line; got: {out!r}"

    def test_sync_json_includes_skipped_remote(self, tmp_config):
        """--json output must include skipped_remote list for programmatic consumers (Fix 2)."""
        manifest = {
            "tables": {
                "orders": {"hash": _FAKE_PARQUET_MD5, "query_mode": "local", "source_type": "keboola"},
                "bq_view": {"hash": "", "query_mode": "remote", "source_type": "bigquery"},
            },
            "assets": {},
            "server_time": "2026-04-27T00:00:00Z",
        }

        with patch("cli.commands.sync.api_get", return_value=_resp(200, manifest)):
            with patch("cli.commands.sync.stream_download", side_effect=_fake_stream_download):
                with patch("cli.commands.sync._rebuild_duckdb_views"):
                    result = runner.invoke(app, ["sync", "--json"])
        assert result.exit_code == 0, f"sync --json failed: {result.output}"

        # Rich Progress may emit a spinner line before the JSON block; match the
        # pattern used by test_sync_json_output above.
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data, _ = json.JSONDecoder().raw_decode(output[json_start:])

        assert "skipped_remote" in data, f"--json output missing skipped_remote key: {data}"
        assert "bq_view" in data["skipped_remote"], \
            f"expected bq_view in skipped_remote; got: {data['skipped_remote']}"
