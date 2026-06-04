"""Parity test for internal-table sampling across both backends.

The catalog ``/sample`` preview for an internal source (``agnes_audit`` /
``agnes_sessions`` / ``agnes_telemetry``) reads the physical state table
(``audit_log`` etc.) through ``connectors.internal.access.sample_internal_rows``.
That row data lives in the active state backend; the old code read it off a raw
always-DuckDB connection, so on a Postgres instance the preview came back empty.

These tests seed an ``audit_log`` row through the factory and assert
``sample_internal_rows`` returns it under the RBAC filter on DuckDB AND Postgres.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def test_sample_internal_rows_scopes_to_user_on_both_backends(_env):
    from connectors.internal.access import (
        INTERNAL_TABLES_BY_ID,
        build_filter_clause,
        sample_internal_rows,
    )
    from src.repositories import audit_repo

    # Seed two audit rows for different users through the factory (→ active
    # backend). The non-admin filter must return only the caller's row.
    audit_repo().log(user_id="analyst1", action="probe.mine", resource="r1")
    audit_repo().log(user_id="someone_else", action="probe.theirs", resource="r2")

    audit_def = INTERNAL_TABLES_BY_ID["agnes_audit"]
    where = build_filter_clause(
        audit_def, {"id": "analyst1", "email": "analyst@test.com"}, is_admin=False
    )
    rows = sample_internal_rows(audit_def, where, 50)

    actions = {r.get("action") for r in rows}
    user_ids = {r.get("user_id") for r in rows}
    assert "probe.mine" in actions, (
        f"[{_env}] seeded audit row missing from internal sample — "
        f"sample_internal_rows read the wrong backend. got actions={actions}"
    )
    assert "probe.theirs" not in actions, (
        f"[{_env}] RBAC filter leaked another user's row: {actions}"
    )
    assert user_ids == {"analyst1"}, f"[{_env}] unexpected user_ids: {user_ids}"


def test_sample_internal_rows_admin_sees_all_on_both_backends(_env):
    from connectors.internal.access import (
        INTERNAL_TABLES_BY_ID,
        build_filter_clause,
        sample_internal_rows,
    )
    from src.repositories import audit_repo

    audit_repo().log(user_id="u_a", action="probe.a", resource="r")
    audit_repo().log(user_id="u_b", action="probe.b", resource="r")

    audit_def = INTERNAL_TABLES_BY_ID["agnes_audit"]
    # Admin → empty where clause → unscoped.
    where = build_filter_clause(audit_def, {"id": "admin1"}, is_admin=True)
    assert where == ""
    rows = sample_internal_rows(audit_def, where, 50)
    actions = {r.get("action") for r in rows}
    assert {"probe.a", "probe.b"} <= actions, (
        f"[{_env}] admin unscoped sample missing rows: {actions}"
    )
