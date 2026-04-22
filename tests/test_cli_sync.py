"""Tests for da sync command."""

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


MANIFEST = {
    "tables": {
        "orders": {"hash": "abc123", "rows": 100, "size_bytes": 2048},
        "customers": {"hash": "def456", "rows": 50, "size_bytes": 1024},
    }
}


# Minimal byte-sequence that passes the PAR1 magic-byte check in cli.commands.sync.
# Enough for tests to simulate a "well-formed" download without building a real parquet.
_FAKE_PARQUET_BYTES = b"PAR1" + b"\x00" * 32 + b"PAR1"


def _fake_stream_download(path, target, *args, **kwargs):
    """Drop-in replacement for cli.commands.sync.stream_download that writes
    a minimally valid parquet shell (PAR1 head + PAR1 tail) to the target."""
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
                "orders": {"hash": "abc123"},
                "customers": {"hash": "def456"},
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


class TestSyncParquetValidation:
    """Two defensive hooks around the parquet pipeline:

    1) After download, the file must have PAR1 magic bytes. A truncated /
       empty body with HTTP 200 would otherwise be saved, committed into
       sync_state, and then crash _rebuild_duckdb_views.
    2) _rebuild_duckdb_views must skip individual broken parquets instead
       of aborting the entire rebuild.
    """

    def _write(self, tmp_config, tid: str, body: bytes) -> None:
        (tmp_config / "local" / "server" / "parquet").mkdir(parents=True, exist_ok=True)
        (tmp_config / "local" / "server" / "parquet" / f"{tid}.parquet").write_bytes(body)

    def test_download_producing_non_parquet_is_recorded_as_error(self, tmp_config):
        """HTTP 200 with a truncated / HTML body must not commit state."""
        def fake_stream(path, target, *a, **kw):
            # Simulate a server that returned a 404 HTML page with 200 status
            # (or a Caddy gateway error, or a truncated body).
            with open(target, "wb") as f:
                f.write(b"<html>oops</html>")

        with patch("cli.commands.sync.api_get", return_value=_resp(200, MANIFEST)):
            with patch("cli.commands.sync.stream_download", side_effect=fake_stream):
                with patch("cli.commands.sync._rebuild_duckdb_views") as mock_rebuild:
                    result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        assert "Downloaded: 0" in result.output
        assert "Errors: 2" in result.output
        assert "PAR1" in result.output  # error message mentions the magic-byte reason
        assert mock_rebuild.call_count == 0

    def test_rebuild_skips_broken_parquet_without_aborting(self, tmp_config):
        """Pre-existing broken parquet must not kill the whole rebuild."""
        self._write(tmp_config, "broken", b"not-parquet-at-all")
        self._write(tmp_config, "also_bad", b"PAR1" + b"\x00" * 10 + b"PAR1")

        from cli.commands.sync import _rebuild_duckdb_views
        local_dir = tmp_config / "local"
        parquet_dir = local_dir / "server" / "parquet"
        # Must not raise — both files are garbage but the function recovers.
        _rebuild_duckdb_views(local_dir, parquet_dir)

    def test_valid_parquet_magic_bytes_helper(self, tmp_config):
        from cli.commands.sync import _is_valid_parquet
        p = tmp_config / "x.parquet"
        p.write_bytes(b"")
        assert _is_valid_parquet(p) is False
        p.write_bytes(b"PAR1")
        assert _is_valid_parquet(p) is False  # too short
        p.write_bytes(b"PAR1" + b"\x00" * 10 + b"PAR1")
        assert _is_valid_parquet(p) is True
        p.write_bytes(b"<html>err</html>")
        assert _is_valid_parquet(p) is False


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
