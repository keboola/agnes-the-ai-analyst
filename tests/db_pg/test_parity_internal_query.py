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


def test_cte_shadow_cannot_escape_rbac_both_backends(_env):
    """A non-admin cannot widen their view by opening their own WITH clause
    that redefines an agnes_* alias to read the unfiltered base table — the
    base-table reference is caught by the denylist on both backends (and on PG
    the materialised table holds only the caller's rows anyway)."""
    from connectors.internal.access import InternalAccessError, execute_internal_query

    _seed_events()
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            system_db_path="",
            user={"id": "ua", "email": "a@example.com"},
            is_admin=False,
            sql=(
                "WITH agnes_telemetry AS (SELECT * FROM usage_events) "
                "SELECT COUNT(*) AS n FROM agnes_telemetry"
            ),
            limit=100,
        )


def test_postgres_tvf_is_unavailable_pg(_env):
    """PG-only: the DuckDB ``postgres`` extension is NOT loaded on the query
    handle (we materialise instead of ATTACHing), so ``postgres_query`` &
    friends — which would otherwise bypass RBAC by reaching PG directly with a
    string-literal catalog arg — simply don't exist. The query fails rather
    than leaking.

    Restricted to the PG backend via an in-body skip rather than a
    ``@parametrize("state_backend", ["pg"], indirect=True)`` marker: re-parametrizing
    a name already supplied by the parametrized ``state_backend`` fixture is a
    duplicate-parametrization collection error under newer pytest."""
    if _env != "pg":
        pytest.skip("PG-only: the DuckDB query handle never loads the postgres extension")
    from connectors.internal.access import execute_internal_query

    _seed_events()
    with pytest.raises(Exception):  # noqa: B017 — DuckDB Catalog/Binder error
        execute_internal_query(
            system_db_path="",
            user={"id": "admin", "email": "admin@example.com"},
            is_admin=True,
            sql=(
                "SELECT * FROM agnes_telemetry WHERE user_id IN "
                "(SELECT user_id FROM postgres_query('x', 'SELECT user_id FROM usage_events'))"
            ),
            limit=100,
        )
