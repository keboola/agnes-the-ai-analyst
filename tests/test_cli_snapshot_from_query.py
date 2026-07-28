"""Tests for `agnes snapshot create --from-query` (issue #616).

The --from-query mode materializes a snapshot from a raw SELECT executed
remotely (no --select/--where parsing). Backs the auto-snapshot fallback.
"""

from unittest.mock import patch, MagicMock

import pyarrow as pa
from typer.testing import CliRunner

from cli.commands.snapshot import snapshot_app

_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_from_query_in_help():
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "--help"])
    assert result.exit_code == 0
    assert "--from-query" in _clean(result.output)


def test_from_query_mutually_exclusive_with_select(tmp_path, monkeypatch):
    """--from-query and --select are mutually exclusive."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        snapshot_app,
        ["create", "web_view", "--from-query", "SELECT * FROM web_view",
         "--select", "country"],
    )
    assert result.exit_code != 0
    out = result.output + (result.stderr or "")
    assert "mutually exclusive" in out


def test_from_query_materializes_raw_sql(tmp_path, monkeypatch):
    """--from-query posts {from_query: <sql>} to /api/v2/scan and materializes
    the Arrow result as a local snapshot + view."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Local DuckDB must exist (fetch-path guard).
    import duckdb
    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    duckdb.connect(str(db_dir / "analytics.duckdb")).close()

    table = pa.table({"country": ["CZ", "US"]})
    captured = {}

    def fake_arrow(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return table

    with patch("cli.commands.snapshot.api_post_arrow", side_effect=fake_arrow):
        runner = CliRunner()
        result = runner.invoke(
            snapshot_app,
            ["create", "auto_deadbeef", "--from-query",
             "SELECT country FROM web_view", "--ttl", "24h"],
        )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    # Request carried the raw SQL, NOT a select/where-built request.
    assert captured["payload"]["from_query"] == "SELECT country FROM web_view"
    assert "select" not in captured["payload"]
    assert "where" not in captured["payload"]
    # Snapshot view exists locally.
    conn = duckdb.connect(str(db_dir / "analytics.duckdb"), read_only=True)
    rows = conn.execute('SELECT country FROM "auto_deadbeef" ORDER BY 1').fetchall()
    conn.close()
    assert rows == [("CZ",), ("US",)]
    # Meta written with a TTL.
    from cli.snapshot_meta import read_meta
    meta = read_meta(tmp_path / "user" / "snapshots", "auto_deadbeef")
    assert meta is not None
    assert meta.expires_at is not None


def test_from_query_skips_select_based_estimate(tmp_path, monkeypatch):
    """--from-query does NOT call /api/v2/scan/estimate (which is
    select/where-based and can't estimate a raw query)."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    import duckdb
    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    duckdb.connect(str(db_dir / "analytics.duckdb")).close()

    table = pa.table({"x": [1]})
    estimate_mock = MagicMock()
    with patch("cli.commands.snapshot.api_post_arrow", return_value=table), \
         patch("cli.commands.snapshot.api_post_json", estimate_mock):
        runner = CliRunner()
        result = runner.invoke(
            snapshot_app,
            ["create", "auto_x", "--from-query", "SELECT 1 AS x"],
        )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    estimate_mock.assert_not_called()
