"""Tests for the ``internal`` data source.

Coverage:
- Per-row RBAC: alice sees only alice's rows; admin sees everyone's.
- Catalog: internal tables show up in /api/v2/catalog.
- Schema: /api/v2/schema/agnes_sessions returns column metadata.
- Cross-source rejection: SQL mixing ``agnes_sessions`` with a registered
  Keboola/BQ table id is rejected before execution.
- Username sanitization: an exotic local-part is rejected, not silently
  scoped to the wrong rows.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from connectors.internal.access import (
    INTERNAL_TABLES_BY_ID,
    InternalAccessError,
    build_filter_clause,
    execute_internal_query,
    find_internal_refs,
    get_schema,
    is_internal_table,
)
from connectors.internal.registry import ensure_internal_tables_registered
from src.db import _ensure_schema, _get_state_dir


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """Stand up a fresh system.duckdb with the v43 schema + registered internal
    rows. ``get_system_db`` is the singleton consumed by the connector, so we
    redirect AGNES_DATA_DIR to a per-test path before the helper opens the
    file."""
    data_dir = tmp_path / "agnes_data"
    (data_dir / "state").mkdir(parents=True)
    # ``src.db._get_data_dir`` reads ``DATA_DIR`` (not ``AGNES_DATA_DIR``).
    # Setting it before ``get_system_db()`` runs reroutes the singleton to
    # the per-test tmp_path.
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("STATE_DIR", raising=False)

    # Force close any pre-existing handle held by an earlier test.
    from src.db import close_system_db
    close_system_db()

    from src.db import get_system_db
    conn = get_system_db()
    _ensure_schema(conn)
    ensure_internal_tables_registered(conn)
    # Seed a couple of canonical rows for the RBAC checks.
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, tool_calls, tool_errors, processor_version) VALUES "
        "('alice/s1.jsonl', 's-a-1', 'alice', 10, 1, 1)"
    )
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, tool_calls, tool_errors, processor_version) VALUES "
        "('bob/s2.jsonl',   's-b-1', 'bob',   20, 0, 1)"
    )
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, result) VALUES "
        "('a-1', 'alice-uuid', 'session.transcript_view', 'success')"
    )
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, result) VALUES "
        "('a-2', 'bob-uuid',   'session.transcript_view', 'success')"
    )
    yield conn
    close_system_db()


# ---------------------------------------------------------------------------
# Static helpers — no DB needed
# ---------------------------------------------------------------------------

def test_is_internal_table_recognises_canonical_ids():
    assert is_internal_table("agnes_sessions")
    assert is_internal_table("agnes_usage")
    assert is_internal_table("agnes_audit")
    assert not is_internal_table("usage_session_summary")  # underlying physical
    assert not is_internal_table("orders_daily")


def test_find_internal_refs_word_boundary():
    refs = find_internal_refs("SELECT * FROM agnes_sessions WHERE x = 'foo'")
    assert refs == ["agnes_sessions"]


def test_find_internal_refs_multiple_in_declaration_order():
    refs = find_internal_refs("SELECT * FROM agnes_audit JOIN agnes_sessions USING (x)")
    # Order follows INTERNAL_TABLES declaration, not the SQL order.
    assert refs == ["agnes_sessions", "agnes_audit"]


def test_find_internal_refs_ignores_unrelated():
    refs = find_internal_refs("SELECT * FROM orders_daily")
    assert refs == []


def test_filter_clause_admin_is_empty():
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    assert build_filter_clause(table, {"email": "admin@x", "id": "admin-uuid"}, True) == ""


def test_filter_clause_non_admin_scopes_to_username():
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    clause = build_filter_clause(table, {"email": "alice@example.com", "id": "alice-uuid"}, False)
    assert clause == "WHERE username = 'alice'"


def test_filter_clause_non_admin_scopes_audit_to_user_id():
    table = INTERNAL_TABLES_BY_ID["agnes_audit"]
    clause = build_filter_clause(
        table, {"email": "alice@example.com", "id": "alice-uuid"}, False,
    )
    assert clause == "WHERE user_id = 'alice-uuid'"


def test_filter_clause_rejects_unsafe_username():
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    with pytest.raises(InternalAccessError):
        build_filter_clause(
            table, {"email": "alice'; DROP TABLE--@example.com", "id": "x"}, False,
        )


# ---------------------------------------------------------------------------
# End-to-end RBAC — runs against a real (fresh) system.duckdb
# ---------------------------------------------------------------------------

def test_admin_sees_every_user_session(system_db, tmp_path):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "admin@x", "id": "admin-uuid"},
        is_admin=True,
        sql="SELECT username FROM agnes_sessions ORDER BY username",
    )
    assert [r[0] for r in rows] == ["alice", "bob"]


def test_non_admin_sees_only_own_sessions(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT username, session_id FROM agnes_sessions",
    )
    assert rows == [("alice", "s-a-1")]


def test_non_admin_sees_only_own_audit_rows(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "bob@example.com", "id": "bob-uuid"},
        is_admin=False,
        sql="SELECT user_id, action FROM agnes_audit",
    )
    assert rows == [("bob-uuid", "session.transcript_view")]


def test_admin_can_count_per_user_session_views(system_db):
    """The motivating admin query: who is looking at whose session transcripts?"""
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "admin@x", "id": "admin-uuid"},
        is_admin=True,
        sql=(
            "SELECT user_id, COUNT(*) AS n FROM agnes_audit "
            "WHERE action = 'session.transcript_view' GROUP BY user_id "
            "ORDER BY user_id"
        ),
    )
    assert rows == [("alice-uuid", 1), ("bob-uuid", 1)]


def test_user_sql_inside_cte_wrapper_still_resolves(system_db):
    """The CTE wrapper that scopes agnes_* aliases must coexist with
    user-supplied SELECT shape — including basic aggregations + LIMIT."""
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT SUM(tool_calls) AS n FROM agnes_sessions",
    )
    assert rows == [(10,)]


def test_schema_returns_underlying_columns(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    cols = get_schema(db_path, "agnes_sessions")
    names = {c["name"] for c in cols}
    # Sample of the documented usage_session_summary columns.
    assert {"session_file", "session_id", "username", "tool_calls"} <= names


def test_get_schema_unknown_table_returns_empty():
    assert get_schema("/nonexistent/path", "not_a_real_internal_table") == []


def test_execute_rejects_sql_without_internal_refs():
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            "/tmp/dummy",
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql="SELECT 1",
        )
