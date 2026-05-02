"""`da sync --quiet` truly suppresses stdout chatter, including the download
loop and final summary.

Without --quiet, the same fixture prints "Downloading", "Downloaded:", etc.;
with --quiet, stdout stays empty and the terse one-liner lands on stderr.
The first test forces the download loop to run so the contrast between
noisy/quiet stdout is observable (mutation-tests the flag — see PR #145
for the original empty-manifest test that passed even without --quiet).
"""
import json
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from cli.main import app


def _fake_manifest_one_table():
    resp = MagicMock()
    resp.json.return_value = {
        "tables": {
            "orders": {
                "hash": "abc123",
                "rows": 5,
                "size_bytes": 100,
                "query_mode": "local",
                "source_type": "keboola",
            }
        },
        "assets": {},
        "server_time": "2026-04-30T00:00:00Z",
    }
    resp.raise_for_status = MagicMock()
    return resp


def _stub_download(_url, target_path):
    from pathlib import Path
    Path(target_path).write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")


def test_quiet_suppresses_stdout_when_downloading(tmp_path, monkeypatch):
    """Manifest has tables that actually trigger downloads. Without --quiet
    stdout would contain 'Downloading' / 'Downloaded:'. With --quiet stdout
    stays empty and the terse summary lands on stderr."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "_cfg"))
    runner = CliRunner()

    with patch("cli.commands.sync.api_get", return_value=_fake_manifest_one_table()), \
         patch("cli.commands.sync.stream_download", side_effect=_stub_download), \
         patch("cli.commands.sync._md5_file", return_value="abc123"), \
         patch("cli.commands.sync._rebuild_duckdb_views"), \
         patch("cli.commands.sync._fetch_and_write_rules"):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "", f"expected empty stdout, got: {result.stdout!r}"
    assert "sync: 1 tables" in result.stderr


def test_noisy_mode_prints_to_stdout(tmp_path, monkeypatch):
    """Anchor: the noisy path DOES print download chatter to stdout, so the
    contrast in the quiet test above is meaningful."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "_cfg"))
    runner = CliRunner()

    with patch("cli.commands.sync.api_get", return_value=_fake_manifest_one_table()), \
         patch("cli.commands.sync.stream_download", side_effect=_stub_download), \
         patch("cli.commands.sync._md5_file", return_value="abc123"), \
         patch("cli.commands.sync._rebuild_duckdb_views"), \
         patch("cli.commands.sync._fetch_and_write_rules"):
        result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, result.stdout
    assert "Downloaded:" in result.stdout


def test_quiet_manifest_failure_exits_nonzero(tmp_path, monkeypatch):
    """SessionStart hook contract: server unreachable → non-zero exit (so
    `|| true` swallows it cleanly), error message on stderr."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "_cfg"))
    runner = CliRunner()

    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = RuntimeError("boom")

    with patch("cli.commands.sync.api_get", return_value=fake_resp):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 1
    assert "manifest fetch failed" in result.stderr


def test_quiet_skips_remote_mode_tables(tmp_path, monkeypatch):
    """Materialized rows go through the download path; remote rows do not.
    Locks in the contract that --quiet honors the same skipped_remote
    filter as the noisy path."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "_cfg"))

    resp = MagicMock()
    resp.json.return_value = {
        "tables": {
            "live_orders": {
                "hash": "x", "rows": 0, "size_bytes": 0,
                "query_mode": "remote", "source_type": "bigquery",
            },
            "agg_90d": {
                "hash": "abc", "rows": 5, "size_bytes": 100,
                "query_mode": "materialized", "source_type": "bigquery",
            },
        },
        "assets": {},
        "server_time": "2026-04-30T00:00:00Z",
    }
    resp.raise_for_status = MagicMock()

    runner = CliRunner()
    download_calls = []

    def _spy_download(url, target):
        download_calls.append(url)
        from pathlib import Path
        Path(target).write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")

    with patch("cli.commands.sync.api_get", return_value=resp), \
         patch("cli.commands.sync.stream_download", side_effect=_spy_download), \
         patch("cli.commands.sync._md5_file", return_value="abc"), \
         patch("cli.commands.sync._rebuild_duckdb_views"), \
         patch("cli.commands.sync._fetch_and_write_rules"):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 0, result.stdout
    # Remote table never downloaded; materialized table downloaded.
    assert any("agg_90d" in u for u in download_calls)
    assert not any("live_orders" in u for u in download_calls)
