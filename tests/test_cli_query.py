"""Tests for da query command."""

import json
import pytest
from unittest.mock import patch, MagicMock

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


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestRemoteQuery:
    def test_remote_query_success(self):
        """--remote sends SQL to server and prints results."""
        # api_post is imported inside _query_remote so mock the source module
        payload = {"columns": ["id", "name"], "rows": [[1, "Alice"]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT * FROM users", "--remote"])
        assert result.exit_code == 0

    def test_remote_query_failure(self):
        """--remote prints error message on API failure (#160 §4.7: shared
        renderer surfaces the detail; the prior `Query failed: ...` prefix
        was dropped in favor of HTTP-status + structured detail)."""
        with patch("cli.client.api_post", return_value=_resp(400, {"detail": "bad SQL"})):
            result = runner.invoke(app, ["query", "SELECT bad", "--remote"])
        assert result.exit_code == 1
        # Renderer formats string-detail as `HTTP 400: bad SQL`
        assert "HTTP 400" in result.output
        assert "bad SQL" in result.output

    def test_remote_query_truncated(self):
        """Truncated result shows warning."""
        payload = {"columns": ["id"], "rows": [[i] for i in range(5)], "truncated": True}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT id FROM t", "--remote", "--limit", "5"])
        assert result.exit_code == 0
        assert "truncated" in result.output


class TestLocalQuery:
    def test_local_query_no_db(self, tmp_config):
        """Local query without DuckDB exits with guidance."""
        result = runner.invoke(app, ["query", "SELECT 1"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_local_query_with_real_db(self, tmp_config):
        """Local query executes against real DuckDB."""
        import duckdb
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE nums (n INTEGER)")
        conn.execute("INSERT INTO nums VALUES (1), (2), (3)")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT SUM(n) as total FROM nums", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["total"] == 6

    def test_local_query_csv_format(self, tmp_config):
        """--format csv produces CSV output."""
        import duckdb
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT a, b FROM t", "--format", "csv"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert lines[0] == "a,b"
        assert "1,x" in lines[1]

    def test_local_query_table_format(self, tmp_config):
        """Default table format renders without crash."""
        import duckdb
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT id FROM t"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_local_query_limit(self, tmp_config):
        """--limit restricts rows returned."""
        import duckdb
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE big (n INTEGER)")
        conn.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(100)])
        conn.close()

        result = runner.invoke(app, ["query", "SELECT n FROM big", "--format", "json", "--limit", "5"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 5

    def test_local_query_sql_error(self, tmp_config):
        """SQL syntax error exits with error."""
        import duckdb
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        result = runner.invoke(app, ["query", "SELECT * FROM nonexistent_table_xyz"])
        assert result.exit_code == 1
        assert "Query error" in result.output
