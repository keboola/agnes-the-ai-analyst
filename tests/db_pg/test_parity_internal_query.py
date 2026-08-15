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

import json

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
            sql=("WITH agnes_telemetry AS (SELECT * FROM usage_events) SELECT COUNT(*) AS n FROM agnes_telemetry"),
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


def test_heterogeneous_json_params_materialize_and_stay_queryable_both_backends(_env):
    """Issue #1310: a JSON/JSONB column with mixed row shapes (one dict, one
    list, one scalar, one NULL) must not break the materialize-into-DuckDB
    step that backs a non-admin internal query (and every PG-backend
    internal query, admin or not).

    Pre-fix, ``CREATE TABLE AS SELECT`` over the raw column let DuckDB infer
    a type per result set — a ``STRUCT`` for uniformly-shaped dict rows,
    otherwise a best-effort ``VARCHAR`` holding Python's ``repr()`` of the
    value (single-quoted — not valid JSON). Either way ``agnes_audit`` came
    out unqueryable as JSON, and which failure mode hit depended on which
    rows happened to land in a given caller's RBAC-filtered batch. The fix
    casts every JSON/JSONB column to text in the source SELECT, so the
    materialized column is always a deterministic, genuinely-JSON VARCHAR.
    """
    from connectors.internal.access import execute_internal_query
    from src.repositories import audit_repo

    repo = audit_repo()
    repo.log(user_id="ua", action="shape.dict", params={"tool": "Read", "count": 1})
    repo.log(user_id="ua", action="shape.list", params=["a", "b", "c"])
    repo.log(user_id="ua", action="shape.scalar", params="a-plain-string")
    repo.log(user_id="ua", action="shape.null", params=None)
    # A different user's row must never leak into 'ua's RBAC-scoped batch —
    # also keeps the source table a realistic shared physical table rather
    # than a single-user toy.
    repo.log(user_id="ub", action="shape.other_user", params={"other": True})

    cols, rows, _truncated = execute_internal_query(
        system_db_path="",
        user={"id": "ua", "email": "a@example.com"},
        is_admin=False,
        sql="SELECT action, params FROM agnes_audit",
        limit=100,
    )
    by_action = {r[0]: r[1] for r in rows}
    assert set(by_action) == {"shape.dict", "shape.list", "shape.scalar", "shape.null"}, (
        f"[{_env}] materialization must succeed and scope to user 'ua': {rows}"
    )

    # Queryable AS TEXT: every non-null value round-trips through
    # json.loads() as valid JSON — the pre-fix VARCHAR fallback held
    # Python's repr() (single-quoted), which does not.
    assert json.loads(by_action["shape.dict"]) == {"tool": "Read", "count": 1}
    assert json.loads(by_action["shape.list"]) == ["a", "b", "c"]
    assert json.loads(by_action["shape.scalar"]) == "a-plain-string"
    assert by_action["shape.null"] is None

    # A second query using DuckDB's own JSON functions proves `params` is
    # genuinely queryable as JSON, not merely round-trippable via Python's
    # json module.
    _cols2, rows2, _ = execute_internal_query(
        system_db_path="",
        user={"id": "ua", "email": "a@example.com"},
        is_admin=False,
        sql="SELECT json_extract_string(params, '$.tool') AS tool FROM agnes_audit WHERE action = 'shape.dict'",
        limit=10,
    )
    assert rows2[0][0] == "Read", f"[{_env}] params must be queryable JSON text: {rows2}"
