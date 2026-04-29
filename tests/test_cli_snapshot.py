"""Tests for `da snapshot list/refresh/drop/prune` (spec §4.2).

NOTE: Tests construct a local Typer app so cli/main.py is NOT imported.
The snapshot_app is registered under the "snapshot" sub-app.
"""
import json
import pytest
import typer
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
import pyarrow as pa

from cli.snapshot_meta import SnapshotMeta, write_meta


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    snap_dir = tmp_path / "user" / "snapshots"
    snap_dir.mkdir(parents=True)
    yield tmp_path


@pytest.fixture
def test_app():
    """Local Typer app with snapshot sub-commands (no cli.main dependency)."""
    from cli.commands.snapshot import snapshot_app
    app = typer.Typer()
    app.add_typer(snapshot_app, name="snapshot")
    return app


def _seed_meta(tmp_path, name="cz_recent", rows=100):
    snap_dir = tmp_path / "user" / "snapshots"
    parquet = snap_dir / f"{name}.parquet"
    parquet.write_bytes(b"PAR1\x00\x00PAR1")
    write_meta(snap_dir, SnapshotMeta(
        name=name, table_id="bq_view", select=None, where=None, limit=None, order_by=None,
        fetched_at="2026-04-27T10:00:00+00:00",
        effective_as_of="2026-04-27T10:00:00+00:00",
        rows=rows, bytes_local=parquet.stat().st_size,
        estimated_scan_bytes_at_fetch=0, result_hash_md5="abc",
    ))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestSnapshotList:
    def test_list_empty(self, cli_env, test_app):
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "list"])
        assert result.exit_code == 0
        assert "no snapshots" in result.output

    def test_list_shows_snapshot(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent", rows=42)
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "list"])
        assert result.exit_code == 0
        assert "cz_recent" in result.output

    def test_list_json(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent", rows=42)
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["name"] == "cz_recent"


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------

class TestSnapshotDrop:
    def test_drop_removes_files(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent")
        snap_dir = cli_env / "user" / "snapshots"
        assert (snap_dir / "cz_recent.parquet").exists()

        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "drop", "cz_recent"])
        assert result.exit_code == 0
        assert not (snap_dir / "cz_recent.parquet").exists()
        assert not (snap_dir / "cz_recent.meta.json").exists()

    def test_drop_missing_returns_2(self, cli_env, test_app):
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "drop", "nonexistent"])
        assert result.exit_code == 2

    def test_drop_message(self, cli_env, test_app):
        _seed_meta(cli_env, "my_snap")
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "drop", "my_snap"])
        assert result.exit_code == 0
        assert "my_snap" in result.output


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

class TestSnapshotRefresh:
    def test_refresh_missing_returns_2(self, cli_env, test_app):
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "refresh", "nosuchsnap"])
        assert result.exit_code == 2

    def test_refresh_success(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent", rows=100)
        snap_dir = cli_env / "user" / "snapshots"

        # Build a minimal Arrow table to return from the mocked API call
        arrow_table = pa.table({"col": [1, 2, 3]})

        with patch("cli.commands.snapshot.api_post_arrow", return_value=arrow_table):
            runner = CliRunner()
            result = runner.invoke(test_app, ["snapshot", "refresh", "cz_recent"])

        assert result.exit_code == 0, result.output
        assert "Refreshed" in result.output
        # Meta file should be updated
        from cli.snapshot_meta import read_meta
        new_meta = read_meta(snap_dir, "cz_recent")
        assert new_meta is not None
        assert new_meta.rows == 3

    def test_refresh_server_error_returns_5(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent")
        from cli.v2_client import V2ClientError
        with patch("cli.commands.snapshot.api_post_arrow",
                   side_effect=V2ClientError(status_code=500, body="internal error")):
            runner = CliRunner()
            result = runner.invoke(test_app, ["snapshot", "refresh", "cz_recent"])
        assert result.exit_code == 5

    def test_refresh_with_where_override(self, cli_env, test_app):
        _seed_meta(cli_env, "cz_recent")
        arrow_table = pa.table({"col": [1]})
        captured = {}
        def fake_post(path, payload):
            captured["where"] = payload.get("where")
            return arrow_table
        with patch("cli.commands.snapshot.api_post_arrow", side_effect=fake_post):
            runner = CliRunner()
            result = runner.invoke(
                test_app,
                ["snapshot", "refresh", "cz_recent", "--where", "country = 'US'"]
            )
        assert result.exit_code == 0, result.output
        assert captured["where"] == "country = 'US'"


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

class TestSnapshotPrune:
    def test_prune_empty(self, cli_env, test_app):
        runner = CliRunner()
        result = runner.invoke(test_app, ["snapshot", "prune", "--older-than", "7d"])
        assert result.exit_code == 0
        assert "no matches" in result.output

    def test_prune_dry_run(self, cli_env, test_app):
        _seed_meta(cli_env, "old_snap", rows=10)
        runner = CliRunner()
        snap_dir = cli_env / "user" / "snapshots"
        # With --older-than 0d everything qualifies (age is > 0)
        result = runner.invoke(test_app, ["snapshot", "prune", "--older-than", "0d", "--dry-run"])
        assert result.exit_code == 0
        assert "would drop" in result.output
        # Dry run must NOT delete files
        assert (snap_dir / "old_snap.parquet").exists()

    def test_prune_deletes_matching(self, cli_env, test_app):
        _seed_meta(cli_env, "old_snap")
        snap_dir = cli_env / "user" / "snapshots"
        result = CliRunner().invoke(test_app, ["snapshot", "prune", "--older-than", "0d"])
        assert result.exit_code == 0
        assert "dropped" in result.output
        assert not (snap_dir / "old_snap.parquet").exists()

    def test_prune_larger_than(self, cli_env, test_app):
        # Seed a tiny snapshot (~8 bytes); --larger-than 1m should not match
        _seed_meta(cli_env, "tiny_snap")
        result = CliRunner().invoke(test_app, ["snapshot", "prune", "--larger-than", "1m"])
        assert result.exit_code == 0
        assert "no matches" in result.output

    def test_prune_no_flags_drops_all(self, cli_env, test_app):
        _seed_meta(cli_env, "snap1")
        snap_dir = cli_env / "user" / "snapshots"
        result = CliRunner().invoke(test_app, ["snapshot", "prune"])
        assert result.exit_code == 0
        # No predicates → all snapshots match → all are dropped
        assert "dropped" in result.output
        assert not (snap_dir / "snap1.parquet").exists()
