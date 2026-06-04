"""Parity test for the internal-table SQL query feature across both backends.

``agnes query "SELECT … FROM agnes_telemetry"`` runs arbitrary analyst DuckDB
SQL against Agnes's own internal tables (usage_events / audit_log /
usage_session_summary), scoped per-caller by RBAC. It executes in DuckDB; on a
Postgres instance the rows live in PG, so `execute_internal_query` now runs the
query in an in-memory DuckDB with the PG database ATTACHed (postgres extension)
and points the agnes_* CTEs at the attached tables — identical behaviour on both
backends.

These tests assert the SAME query returns the SAME result on DuckDB AND Postgres
(the parity goal), plus that the RBAC scoping + non-admin denylist + the PG
attach-catalog guard hold on both.
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


def _seed_events():
    """2 telemetry events for user 'ua', 1 for 'ub', through the factory."""
    from src.repositories import usage_repo

    repo = usage_repo()
    repo.emit_server_event(event_type="tool.call", user_id="ua", username="a", props={})
    repo.emit_server_event(event_type="tool.call", user_id="ua", username="a", props={})
    repo.emit_server_event(event_type="tool.call", user_id="ub", username="b", props={})


def test_non_admin_query_scoped_to_own_rows_both_backends(_env):
    from connectors.internal.access import execute_internal_query

    _seed_events()
    cols, rows, _ = execute_internal_query(
        system_db_path="",
        user={"id": "ua", "email": "a@example.com"},
        is_admin=False,
        sql="SELECT COUNT(*) AS n FROM agnes_telemetry",
        limit=100,
    )
    assert rows[0][0] == 2, (
        f"[{_env}] non-admin should see only their own 2 events, got {rows} "
        f"(empty would mean the query ran against the wrong backend)"
    )


def test_admin_query_sees_all_rows_both_backends(_env):
    from connectors.internal.access import execute_internal_query

    _seed_events()
    cols, rows, _ = execute_internal_query(
        system_db_path="",
        user={"id": "admin", "email": "admin@example.com"},
        is_admin=True,
        sql="SELECT COUNT(*) AS n FROM agnes_telemetry",
        limit=100,
    )
    assert rows[0][0] == 3, f"[{_env}] admin (unscoped) should see all 3 events, got {rows}"


def test_non_admin_cannot_reference_base_table_both_backends(_env):
    from connectors.internal.access import InternalAccessError, execute_internal_query

    _seed_events()
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            system_db_path="",
            user={"id": "ua", "email": "a@example.com"},
            is_admin=False,
            # references the agnes_* alias (so it passes find_internal_refs) AND
            # the sensitive base table `users` → denylist must reject.
            sql="SELECT * FROM agnes_telemetry WHERE user_id IN (SELECT id FROM users)",
            limit=100,
        )


@pytest.mark.parametrize("state_backend", ["pg"], indirect=True)
def test_non_admin_cannot_reach_attach_catalog_pg(_env):
    """PG-only: a non-admin must not be able to reach the ATTACHed Postgres
    catalog (e.g. pg_catalog system tables) via the `pgsys` alias."""
    from connectors.internal.access import InternalAccessError, execute_internal_query

    _seed_events()
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            system_db_path="",
            user={"id": "ua", "email": "a@example.com"},
            is_admin=False,
            sql="SELECT (SELECT count(*) FROM pgsys.pg_catalog.pg_user) AS x FROM agnes_telemetry",
            limit=100,
        )
