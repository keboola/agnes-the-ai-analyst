"""Tests for Agnes MCP server tools (cli/mcp/server.py).

Each tool is tested by mocking the underlying API client calls so the tests
run without a live Agnes server.  We also verify the MCP protocol layer:
the server starts, responds to initialize + tools/list, and reports the
expected tool names.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ────────────────────────────────────────────────────────────────


def _import_server():
    """Import cli.mcp.server, skipping if mcp is not installed."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as srv

    return srv


# ── MCP protocol smoke-test ────────────────────────────────────────────────


class TestMCPProtocol:
    def test_server_starts_and_lists_tools(self):
        """Send initialize + tools/list over stdin, verify 6 tools are registered."""
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "cli.mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )

        init_msg = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                }
            )
            + "\n"
        )
        # MCP protocol requires `notifications/initialized` after the
        # initialize response before the client can issue requests.
        initialized_notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
        list_msg = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"

        # Send initialize and wait for the response before sending the
        # next messages — avoids a race on Python 3.13 where writing all
        # messages at once + closing stdin can cause the server to exit
        # before flushing the tools/list response.
        proc.stdin.write(init_msg)
        proc.stdin.flush()
        try:
            init_line = proc.stdout.readline()
        except Exception:
            proc.kill()
            proc.wait()
            pytest.fail("MCP server closed stdout before sending initialize response")

        # Now send the rest and read until the server exits.
        proc.stdin.write(initialized_notif)
        proc.stdin.write(list_msg)
        proc.stdin.flush()

        try:
            remaining, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            remaining, _ = proc.communicate()

        out = init_line + remaining
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        assert lines, "MCP server produced no output"

        tool_names = set()
        for line in lines:
            try:
                d = json.loads(line)
                tools = d.get("result", {}).get("tools", [])
                for t in tools:
                    tool_names.add(t["name"])
            except (json.JSONDecodeError, KeyError):
                pass

        expected = {"server_info", "catalog", "schema", "describe", "query", "pull"}
        assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}. Got: {tool_names}"

    def test_server_info_in_initialize_response(self):
        """Initialize response must carry serverInfo.name == 'Agnes'."""

        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "cli.mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )

        init_msg = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                }
            )
            + "\n"
        )

        proc.stdin.write(init_msg)
        proc.stdin.flush()

        try:
            out, _ = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

        found = False
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                info = d.get("result", {}).get("serverInfo", {})
                if info.get("name") == "Agnes":
                    found = True
                    break
            except json.JSONDecodeError:
                pass

        assert found, f"serverInfo.name != 'Agnes' in output: {out[:500]}"


# ── tool unit tests ────────────────────────────────────────────────────────


class TestCatalogTool:
    def test_catalog_returns_tables(self):
        srv = _import_server()
        mock_data = {"tables": [{"id": "orders", "name": "Orders", "query_mode": "local"}]}
        with patch("cli.mcp.server.api_get_json", return_value=mock_data) as m:
            result = srv.catalog()
        m.assert_called_once_with("/api/v2/catalog")
        assert result["tables"][0]["id"] == "orders"

    def test_catalog_raises_on_error(self):
        srv = _import_server()
        from cli.v2_client import V2ClientError

        with patch("cli.mcp.server.api_get_json", side_effect=V2ClientError(401, "Unauthorized")):
            with pytest.raises(ValueError, match="catalog"):
                srv.catalog()


class TestSchemaTool:
    def test_schema_passes_table_id(self):
        srv = _import_server()
        mock_data = {"table_id": "orders", "columns": [{"name": "id", "type": "VARCHAR"}]}
        with patch("cli.mcp.server.api_get_json", return_value=mock_data) as m:
            result = srv.schema("orders")
        m.assert_called_once_with("/api/v2/schema/orders")
        assert result["columns"][0]["name"] == "id"

    def test_schema_raises_on_404(self):
        srv = _import_server()
        from cli.v2_client import V2ClientError

        with patch("cli.mcp.server.api_get_json", side_effect=V2ClientError(404, "Not found")):
            with pytest.raises(ValueError, match="schema"):
                srv.schema("nonexistent")


class TestDescribeTool:
    def test_describe_calls_schema_and_sample(self):
        srv = _import_server()
        schema_data = {"table_id": "orders", "columns": []}
        sample_data = {"rows": [["a", 1]]}

        def _mock_get(path, **kwargs):
            if "schema" in path:
                return schema_data
            return sample_data

        with patch("cli.mcp.server.api_get_json", side_effect=_mock_get):
            result = srv.describe("orders", rows=3)

        assert "schema" in result
        assert "sample" in result

    def test_describe_clamps_rows_to_50(self):
        srv = _import_server()
        calls = []

        def _mock_get(path, **kwargs):
            calls.append((path, kwargs))
            return {}

        with patch("cli.mcp.server.api_get_json", side_effect=_mock_get):
            srv.describe("orders", rows=999)

        # The n kwarg in the sample call must be capped at 50
        sample_call = next((c for c in calls if "sample" in c[0]), None)
        assert sample_call is not None
        assert sample_call[1].get("n", 0) <= 50


class TestQueryTool:
    def test_query_posts_to_api(self):
        srv = _import_server()
        mock_data = {"columns": ["id", "name"], "rows": [[1, "Alice"]], "truncated": False}
        with patch("cli.mcp.server.api_post_json", return_value=mock_data) as m:
            result = srv.query("SELECT id, name FROM users LIMIT 5")
        m.assert_called_once_with("/api/query", {"sql": "SELECT id, name FROM users LIMIT 5", "limit": 1000})
        assert result["columns"] == ["id", "name"]

    def test_query_respects_limit(self):
        srv = _import_server()
        with patch("cli.mcp.server.api_post_json", return_value={"columns": [], "rows": []}) as m:
            srv.query("SELECT 1", limit=50)
        _, payload = m.call_args[0]
        assert payload["limit"] == 50

    def test_query_raises_on_server_error(self):
        srv = _import_server()
        from cli.v2_client import V2ClientError

        with patch("cli.mcp.server.api_post_json", side_effect=V2ClientError(400, "syntax error")):
            with pytest.raises(ValueError, match="query"):
                srv.query("SELECT broken syntax !!!!")


class TestQueryLocalTool:
    def test_raises_when_db_missing(self, tmp_path):
        srv = _import_server()
        with patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}):
            with pytest.raises(FileNotFoundError, match="Local DuckDB"):
                srv.query_local("SELECT 1")

    def test_queries_local_duckdb(self, tmp_path):
        import duckdb

        srv = _import_server()

        # Create a minimal local DuckDB with a test view
        db_path = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db_path.parent.mkdir(parents=True)
        with duckdb.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")

        with patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}):
            result = srv.query_local("SELECT x FROM t")

        assert result["columns"] == ["x"]
        assert result["rows"] == [[42]]

    def test_table_miss_hints_at_query_tool(self, tmp_path):
        import duckdb

        srv = _import_server()

        db_path = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db_path.parent.mkdir(parents=True)
        with duckdb.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")

        with patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}):
            with pytest.raises(ValueError) as exc_info:
                srv.query_local("SELECT * FROM nope")

        message = str(exc_info.value)
        assert "query" in message
        assert "server-side" in message

    def test_non_table_miss_error_reraised_without_hint(self, tmp_path):
        import duckdb

        srv = _import_server()

        db_path = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db_path.parent.mkdir(parents=True)
        with duckdb.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")

        with patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}):
            with pytest.raises(duckdb.Error) as exc_info:
                srv.query_local("SELECT broken syntax !!!!")

        message = str(exc_info.value)
        assert "query_mode" not in message
        assert "server-side" not in message


class TestPullTool:
    def test_pull_calls_run_pull(self, tmp_path):
        srv = _import_server()
        from cli.lib.pull import PullResult

        mock_result = PullResult(tables_updated=2, parquets_total=5)

        with (
            patch("cli.mcp.server.get_server_url", return_value="http://localhost:8000"),
            patch("cli.mcp.server.get_token", return_value="tok_test"),
            patch("cli.lib.pull.run_pull", return_value=mock_result) as m,
            patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}),
        ):
            result = srv.pull()

        m.assert_called_once()
        assert result["tables_updated"] == 2
        assert result["parquets_total"] == 5

    def test_pull_returns_duration_s_from_result(self, tmp_path):
        """Regression — Devin Review BUG_0001 on #594.

        The MCP `pull` tool used to read `result.elapsed_s` (with a
        `hasattr` guard) and return `"elapsed_s": None` — the actual
        attribute on `PullResult` is `duration_s`, so every MCP `pull`
        response erased the real wall-clock duration. The fix renames
        the response key to `duration_s` and reads the correct attribute.
        """
        srv = _import_server()
        from cli.lib.pull import PullResult

        mock_result = PullResult(tables_updated=1, parquets_total=3, duration_s=12.345)

        with (
            patch("cli.mcp.server.get_server_url", return_value="http://localhost:8000"),
            patch("cli.mcp.server.get_token", return_value="tok_test"),
            patch("cli.lib.pull.run_pull", return_value=mock_result),
            patch.dict("os.environ", {"AGNES_LOCAL_DIR": str(tmp_path)}),
        ):
            result = srv.pull()

        # The fix: key is duration_s (matches PullResult + --json), value
        # carries the actual wall-clock seconds, not None.
        assert "duration_s" in result
        assert result["duration_s"] == 12.3  # round(12.345, 1)
        # And the broken key is gone — locks against silent re-introduction.
        assert "elapsed_s" not in result

    def test_pull_raises_without_token(self):
        srv = _import_server()
        with (
            patch("cli.mcp.server.get_server_url", return_value="http://localhost:8000"),
            patch("cli.mcp.server.get_token", return_value=None),
        ):
            with pytest.raises(ValueError, match="No Agnes token"):
                srv.pull()


class TestServerInfoTool:
    def test_returns_server_url(self):
        srv = _import_server()
        with (
            patch("cli.mcp.server.get_server_url", return_value="http://localhost:8000"),
            patch("cli.mcp.server.get_token", return_value="tok"),
            patch("cli.mcp.server.api_get") as m_get,
            patch("cli.mcp.server.api_get_json", return_value={"email": "analyst@test.com"}),
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok"}
            m_get.return_value = mock_resp

            result = srv.server_info()

        assert result["server_url"] == "http://localhost:8000"
        assert result["authenticated"] is True
        assert result["user_email"] == "analyst@test.com"
