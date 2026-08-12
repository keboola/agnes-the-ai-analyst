"""Task 8 -- table access policies enforced on the ``table_id``-shaped read
surfaces (table access policies design doc §5, §8; plan Task 8).

``/api/v2/sample``, ``/api/v2/scan`` (local branch) and
``POST /api/mcp/query-table/{id}`` never open the analytics catalog the way
``/api/query`` does -- they build their own ``FROM <source>`` (a throwaway
``read_parquet(...)`` in a fresh ``:memory:`` connection, or a bare view
reference) instead of parsing caller SQL, so ``rewrite_sql`` (Task 6) does
not reach them. This is the FIRST test that exercises these three surfaces
through the live HTTP endpoints with a policy attached.

End-to-end over a real ``TestClient`` + real DuckDB (no mocks): a
``server_only`` table carries a row+column policy filtering by
``$user_groups`` and excluding a column. Two non-admin users, each in their
own group, see disjoint row slices and never the masked column; the admin
(full-surface credential) sees the raw, unfiltered table. A sibling table
with NO policy attached is exercised the same way to pin that the inert case
is untouched.
"""

from __future__ import annotations

import pytest

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"


class TestPoliciedFromSql:
    """Direct unit coverage for the shared FROM builder
    (``src.access_policy.policied_from_sql``) all three ``table_id``
    surfaces below route through -- pins its exact contract independent of
    any one endpoint's plumbing around it."""

    def test_wraps_the_relation_body_in_a_cte_over_the_source(self):
        from src.access_policy import PoliciedRelation, policied_from_sql

        relation = PoliciedRelation(
            relation_sql="SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)",
            params={"user_groups": ["TeamA"]},
            policied=True,
            table_id="orders",
        )
        out = policied_from_sql(relation, table_name="orders", source_sql="read_parquet('/tmp/x.parquet')")
        assert out == (
            "(WITH \"orders\" AS (SELECT * FROM read_parquet('/tmp/x.parquet')) "
            "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit))"
        )

    def test_refuses_a_non_policied_relation(self):
        from src.access_policy import PoliciedRelation, policied_from_sql
        from src.sql_ident import quote_ident

        relation = PoliciedRelation(
            relation_sql=f"SELECT * FROM {quote_ident('orders')}",
            params={},
            policied=False,
            table_id="orders",
        )
        with pytest.raises(ValueError):
            policied_from_sql(relation, table_name="orders", source_sql="read_parquet('/tmp/x.parquet')")


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` ``orders`` table carrying ``POLICY_SQL``, plus a
    sibling ``products`` table with no policy attached at all -- both
    granted to two non-admin users each in their own group (``TeamA`` /
    ``TeamB``, also the ``unit`` values the policy filters on), plus the
    seeded admin.

    ``id == name == "orders"`` for both tables (deliberately, unlike Task
    7's fixture) -- ``/api/v2/sample`` and ``/api/v2/scan``'s local branch
    resolve their parquet by registry ``id`` (``app/utils.py::
    resolve_local_parquet``), while the mock extract writes the parquet
    filename from the per-table ``name`` key and the master view is created
    under that same name (§5.3) -- so id and name must coincide here for
    the parquet to actually be found by every surface under test, the same
    convention ``tests/test_mcp_per_table.py`` uses.
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
            {
                "name": "products",
                "data": [
                    {"id": "1", "sku": "A1"},
                    {"id": "2", "sku": "A2"},
                ],
            },
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy("orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")

        # No access_policy_sql on this one -- the inert-case control.
        registry.register(
            id="products",
            name="products",
            source_type="keboola",
            query_mode="local",
        )

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        users.create(id="u_team_b", email="team-b@example.com", name="Team B")

        # Each grant also creates the group the policy's $user_groups reads --
        # "TeamA"/"TeamB" are simultaneously the RBAC-visibility group and the
        # row-filter value, mirroring Task 7's fixture.
        grant_table_via_package(conn, "orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "orders", "u_team_b", group_name="TeamB")
        grant_table_via_package(conn, "products", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
        "team_b_token": create_access_token("u_team_b", "team-b@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── /api/v2/sample ──────────────────────────────────────────────────────


def test_v2_sample_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    assert all("secret" not in row for row in rows)


def test_v2_sample_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_b_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert {row["id"] for row in rows} == {"3"}
    assert all("secret" not in row for row in rows)


def test_v2_sample_admin_sees_all_rows_and_all_columns_unfiltered(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["admin_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 3
    assert any("secret" in row for row in rows)


def test_v2_sample_non_policied_sibling_table_is_untouched(policied_orders):
    """The inert case: a table with no access_policy_sql behaves exactly
    like `/api/v2/sample` did before this feature existed -- every row,
    every column, for a non-admin grantee."""
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/products?n=10", headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    assert all("sku" in row for row in rows)


# ── /api/v2/scan (local branch) ─────────────────────────────────────────


def test_v2_scan_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "orders"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert "secret" not in table.column_names
    assert table.num_rows == 2
    assert set(table.column("id").to_pylist()) == {"1", "2"}


def test_v2_scan_admin_sees_all_rows_and_all_columns_unfiltered(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "orders"},
        headers=_auth(policied_orders["admin_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert "secret" in table.column_names
    assert table.num_rows == 3


def test_v2_scan_non_policied_sibling_table_is_untouched(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "products"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert table.num_rows == 2
    assert "sku" in table.column_names


# ── POST /api/mcp/query-table/{id} ──────────────────────────────────────


def test_mcp_query_table_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert "secret" not in body["columns"]
    assert {row["id"] for row in body["rows"]} == {"1", "2"}
    assert all("secret" not in row for row in body["rows"])


def test_mcp_query_table_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_b_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1
    assert {row["id"] for row in body["rows"]} == {"3"}


def test_mcp_query_table_admin_sees_all_rows_and_the_secret_column(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["admin_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 3
    assert "secret" in body["columns"]


def test_mcp_query_table_masked_column_is_not_revealed_in_the_400(policied_orders):
    """A filter on the EXCLUDE'd column must not be silently honored (it
    would leak whether a hidden value matches) NOR list `secret` in the
    error's `allowed` column set (§8's instruction: never reveal an
    EXCLUDE'd column, including in an error message)."""
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {"secret": "s1"}, "limit": 10},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "unknown_filter_columns"
    assert "secret" in detail["unknown"]
    assert "secret" not in detail["allowed"]


def test_mcp_query_table_non_policied_sibling_table_is_untouched(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/products",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert "sku" in body["columns"]
