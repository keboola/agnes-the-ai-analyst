# tests/test_cli_fetch.py
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
import pyarrow as pa
import json
import pytest


def _seed_local_dir(tmp_path):
    """Set up the user's agnes-data directory for the CLI to find."""
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "snapshots").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(_seed_local_dir(tmp_path)))
    yield tmp_path


class TestDaFetch:
    def test_estimate_only_does_not_create_snapshot(self, cli_env, monkeypatch):
        from cli.commands.fetch import fetch_app
        with patch("cli.commands.fetch.api_post_json") as m:
            m.return_value = {
                "estimated_scan_bytes": 1_000_000,
                "estimated_result_rows": 100,
                "estimated_result_bytes": 1_000,
                "bq_cost_estimate_usd": 0.0001,
            }
            runner = CliRunner()
            result = runner.invoke(fetch_app, [
                "bq_view",
                "--select", "a,b",
                "--where", "a > 1",
                "--limit", "100",
                "--estimate",
            ])
        assert result.exit_code == 0, result.stdout
        # No parquet should be created
        assert not list((cli_env / "user" / "snapshots").glob("*.parquet"))

    def test_fetch_creates_snapshot_with_meta(self, cli_env, monkeypatch):
        from cli.commands.fetch import fetch_app
        # Estimate path
        with patch("cli.commands.fetch.api_post_json") as m_est, \
             patch("cli.commands.fetch.api_post_arrow") as m_scan:
            m_est.return_value = {
                "estimated_scan_bytes": 1000,
                "estimated_result_rows": 2,
                "estimated_result_bytes": 100,
                "bq_cost_estimate_usd": 0.0,
            }
            m_scan.return_value = pa.table({"a": [1, 2], "b": ["x", "y"]})
            runner = CliRunner()
            result = runner.invoke(fetch_app, [
                "bq_view",
                "--select", "a,b",
                "--limit", "10",
                "--no-estimate",
            ])
        assert result.exit_code == 0, result.stdout
        snap = cli_env / "user" / "snapshots" / "bq_view.parquet"
        meta = cli_env / "user" / "snapshots" / "bq_view.meta.json"
        assert snap.exists()
        assert meta.exists()
        assert json.loads(meta.read_text())["rows"] == 2

    def test_fetch_existing_snapshot_without_force_fails(self, cli_env, monkeypatch):
        from cli.commands.fetch import fetch_app
        # Pre-create a snapshot
        snap = cli_env / "user" / "snapshots" / "bq_view.parquet"
        snap.write_bytes(b"PAR1\\x00\\x00PAR1")
        meta = cli_env / "user" / "snapshots" / "bq_view.meta.json"
        meta.write_text('{"name": "bq_view", "table_id": "bq_view", "select": null, "where": null, "limit": null, "order_by": null, "fetched_at": "x", "effective_as_of": "x", "rows": 0, "bytes_local": 0, "estimated_scan_bytes_at_fetch": 0, "result_hash_md5": ""}')

        runner = CliRunner()
        result = runner.invoke(fetch_app, ["bq_view", "--no-estimate"])
        assert result.exit_code == 6, f"expected exit code 6 (snapshot_exists); got {result.exit_code}\n{result.stdout}"
