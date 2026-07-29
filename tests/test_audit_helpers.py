"""Shared audit classification rules (src/audit_helpers.py)."""

import duckdb
import pytest

from src.audit_helpers import (
    AUDIT_SOURCE_CASE_SQL,
    RESULT_CLASS_CASE_SQL,
    RESULT_CLASSES,
    SCHEDULER_ACTION_SQL,
    classify_result,
)


def test_classify_result_classes():
    assert classify_result(None) == "none"
    assert classify_result("success") == "success"
    assert classify_result("ok") == "success"
    assert classify_result("error") == "error"
    assert classify_result("error.404") == "error"
    assert classify_result("denied") == "denied"
    assert classify_result("blocked") == "denied"
    assert classify_result("invalid_password") == "denied"
    assert classify_result("deactivated") == "denied"
    assert classify_result("skipped") == "other"


def test_result_classes_tuple_is_closed_set():
    assert set(RESULT_CLASSES) == {"success", "error", "denied", "none", "other"}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, "none"),
        ("success", "success"),
        ("ok", "success"),
        ("error.exec_failed", "error"),
        ("denied", "denied"),
        ("skipped", "other"),
    ],
)
def test_sql_case_matches_python_mirror(result, expected):
    """RESULT_CLASS_CASE_SQL and classify_result must never drift apart."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE audit_log (result VARCHAR)")
    conn.execute("INSERT INTO audit_log VALUES (?)", [result])
    row = conn.execute(f"SELECT {RESULT_CLASS_CASE_SQL} FROM audit_log").fetchone()
    assert row[0] == expected == classify_result(result)


@pytest.mark.parametrize(
    ("client_kind", "action", "user_id", "expected"),
    [
        ("cli", "data.access_check", "u1", "cli"),
        (None, "run_session_processor:usage", "u1", "scheduler"),
        (None, "run_jira_sla_poll", "u1", "scheduler"),
        (None, "marketplace.sync_all", "u1", "scheduler"),
        (None, "job.enqueue", None, "system"),
        (None, "table.read", "u1", "other"),
        ("", "table.read", "u1", "other"),
    ],
)
def test_source_case_buckets(client_kind, action, user_id, expected):
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE audit_log (client_kind VARCHAR, action VARCHAR, user_id VARCHAR)")
    conn.execute("INSERT INTO audit_log VALUES (?, ?, ?)", [client_kind, action, user_id])
    row = conn.execute(f"SELECT {AUDIT_SOURCE_CASE_SQL} FROM audit_log").fetchone()
    assert row[0] == expected


def test_scheduler_rule_matches_last_scheduler_tick_rule():
    """The shared predicate is the one last_scheduler_tick has always used."""
    assert "action LIKE 'run_%'" in SCHEDULER_ACTION_SQL
    assert "marketplace.sync_all" in SCHEDULER_ACTION_SQL
