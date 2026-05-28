"""Tool-handler tests for the chat agent.

These exercise the six tool handlers directly (no FastAPI, no Anthropic
client) and verify RBAC + content shape.
"""

import asyncio
import os
from pathlib import Path

import duckdb
import pytest

from app.chat import tools as chat_tools


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def chat_env(tmp_path, monkeypatch):
    """A clean DATA_DIR with system + analytics DBs and two tables registered:
    one local (ok) + one remote (refused by run_query / describe_table).

    The analytics DB has a tiny "orders" parquet so SELECTs return real data.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "analytics").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "extracts").mkdir(exist_ok=True)

    # Force schema migration on a fresh system.duckdb.
    from src.db import get_system_db
    sys_conn = get_system_db()
    # Seed an admin user (so RBAC bypass triggers in tools that consult it).
    sys_conn.execute(
        "INSERT INTO users (id, email, name, active) VALUES (?, ?, ?, TRUE)",
        ["alice", "alice@example.com", "Alice"],
    )
    sys_conn.execute(
        "INSERT INTO users (id, email, name, active) VALUES (?, ?, ?, TRUE)",
        ["bob", "bob@example.com", "Bob"],
    )
    # Make alice an admin.
    from src.db import SYSTEM_ADMIN_GROUP
    admin_gid = sys_conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
    ).fetchone()[0]
    sys_conn.execute(
        "INSERT INTO user_group_members (user_id, group_id, source) VALUES (?, ?, ?)",
        ["alice", admin_gid, "system_seed"],
    )

    # Register two tables: a local one (orders) and a remote one (events).
    sys_conn.execute(
        """INSERT INTO table_registry
               (id, name, source_type, query_mode, description, registered_at)
           VALUES (?, ?, ?, ?, ?, current_timestamp)""",
        ["orders", "orders", "keboola", "local", "Orders fact table"],
    )
    sys_conn.execute(
        """INSERT INTO table_registry
               (id, name, source_type, query_mode, description, registered_at)
           VALUES (?, ?, ?, ?, ?, current_timestamp)""",
        ["events", "events", "bigquery", "remote", "Web events (BigQuery)"],
    )
    sys_conn.close()

    # Build the analytics DB with a real view over a tiny parquet.
    parquet = tmp_path / "extracts" / "kbc" / "data" / "orders.parquet"
    parquet.parent.mkdir(parents=True, exist_ok=True)
    pq_conn = duckdb.connect(":memory:")
    pq_conn.execute(
        f"COPY (SELECT * FROM (VALUES "
        f"('o1', 100, 'CZ'), ('o2', 250, 'CZ'), ('o3', 50, 'US')"
        f") AS t(id, total, country)) TO '{parquet}' (FORMAT PARQUET)"
    )
    pq_conn.close()

    analytics_path = tmp_path / "analytics" / "server.duckdb"
    an_conn = duckdb.connect(str(analytics_path))
    an_conn.execute(
        f'CREATE OR REPLACE VIEW "orders" AS '
        f"SELECT * FROM read_parquet('{parquet}')"
    )
    # Remote view: just an empty placeholder so the registry row resolves.
    an_conn.execute(
        'CREATE OR REPLACE VIEW "events" AS '
        "SELECT 1 AS id WHERE FALSE"
    )
    an_conn.close()

    # Re-open system DB for the test body so it picks up the seeded rows.
    sys_conn = get_system_db()
    yield {"conn": sys_conn, "admin_user": {"id": "alice", "email": "alice@example.com"},
           "non_admin_user": {"id": "bob", "email": "bob@example.com"}}
    sys_conn.close()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# list_catalog
# --------------------------------------------------------------------------- #


class TestListCatalog:
    def test_admin_sees_all_tables(self, chat_env):
        result = _run(chat_tools.dispatch(
            "list_catalog", {}, chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        ids = {t["id"] for t in result.data["tables"]}
        assert {"orders", "events"} <= ids
        assert result.data["count"] >= 2

    def test_non_admin_sees_only_granted_tables(self, chat_env):
        result = _run(chat_tools.dispatch(
            "list_catalog", {}, chat_env["non_admin_user"], chat_env["conn"],
        ))
        assert result.ok
        # bob has no data-package grants → no external tables visible.
        ids = {t["id"] for t in result.data["tables"]}
        assert "orders" not in ids
        assert "events" not in ids


# --------------------------------------------------------------------------- #
# get_schema
# --------------------------------------------------------------------------- #


class TestGetSchema:
    def test_admin_can_read_local_schema(self, chat_env):
        result = _run(chat_tools.dispatch(
            "get_schema", {"table_id": "orders"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        assert result.data["table_id"] == "orders"
        assert any(c["name"] == "total" for c in result.data["columns"])

    def test_non_admin_denied(self, chat_env):
        result = _run(chat_tools.dispatch(
            "get_schema", {"table_id": "orders"},
            chat_env["non_admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "access denied" in result.data["error"].lower()

    def test_unknown_table(self, chat_env):
        result = _run(chat_tools.dispatch(
            "get_schema", {"table_id": "nonexistent"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "not registered" in result.data["error"]

    def test_missing_arg(self, chat_env):
        result = _run(chat_tools.dispatch(
            "get_schema", {}, chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok


# --------------------------------------------------------------------------- #
# describe_table
# --------------------------------------------------------------------------- #


class TestDescribeTable:
    def test_returns_sample_rows(self, chat_env):
        result = _run(chat_tools.dispatch(
            "describe_table", {"table_id": "orders", "n": 2},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        assert result.data["row_count"] == 2
        assert "id" in result.data["columns"]

    def test_n_capped_at_20(self, chat_env):
        result = _run(chat_tools.dispatch(
            "describe_table", {"table_id": "orders", "n": 9999},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        # Only 3 rows in fixture, but cap should not crash.
        assert result.data["row_count"] <= 20

    def test_refuses_remote(self, chat_env):
        result = _run(chat_tools.dispatch(
            "describe_table", {"table_id": "events"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "remote" in result.data["error"]


# --------------------------------------------------------------------------- #
# run_query
# --------------------------------------------------------------------------- #


class TestRunQuery:
    def test_select_returns_rows(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "SELECT country, COUNT(*) AS n FROM orders GROUP BY 1"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        assert result.data["columns"] == ["country", "n"]
        assert result.data["row_count"] == 2  # CZ + US
        assert "orders" in result.data["tables_referenced"]

    def test_refuses_drop(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "DROP TABLE orders"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "select" in result.data["error"].lower()

    def test_refuses_semicolon_chained(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query",
            {"sql": "SELECT 1; SELECT 2"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok

    def test_refuses_remote_table(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "SELECT * FROM events LIMIT 5"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "remote" in result.data["error"]

    def test_refuses_unregistered_table(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "SELECT * FROM users LIMIT 1"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        # `users` exists in system.duckdb but is not in table_registry — refuse.
        assert not result.ok
        assert "not registered" in result.data["error"]

    def test_refuses_non_admin_without_grant(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "SELECT * FROM orders LIMIT 1"},
            chat_env["non_admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "access denied" in result.data["error"].lower()

    def test_requires_from_clause(self, chat_env):
        result = _run(chat_tools.dispatch(
            "run_query", {"sql": "SELECT 1"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        # Without FROM/JOIN we can't determine RBAC scope — refuse.
        assert "table reference" in result.data["error"]


# --------------------------------------------------------------------------- #
# lookup_metric
# --------------------------------------------------------------------------- #


class TestLookupMetric:
    def test_returns_not_found_for_unknown(self, chat_env):
        result = _run(chat_tools.dispatch(
            "lookup_metric", {"metric_id": "no.such.metric"},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "not found" in result.data["error"]


# --------------------------------------------------------------------------- #
# get_memory_bundle
# --------------------------------------------------------------------------- #


class TestGetMemoryBundle:
    def test_returns_empty_bundle_for_fresh_install(self, chat_env):
        result = _run(chat_tools.dispatch(
            "get_memory_bundle", {},
            chat_env["admin_user"], chat_env["conn"],
        ))
        assert result.ok
        assert result.data["mandatory_count"] == 0
        assert result.data["approved_count"] == 0


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #


class TestDispatcher:
    def test_unknown_tool_returns_error(self, chat_env):
        result = _run(chat_tools.dispatch(
            "no_such_tool", {}, chat_env["admin_user"], chat_env["conn"],
        ))
        assert not result.ok
        assert "unknown tool" in result.data["error"]
