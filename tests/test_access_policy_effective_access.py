"""Task 17 -- effective-access diagnosis + error contracts (table access
policies design doc §10.2, §16; plan Task 17).

``GET /api/me/effective-access`` and ``GET /api/admin/users/{id}/effective-
access`` already exist as the self-audit / operator-audit surface for the
generic ``resource_grants`` graph. This adds a per-table ``policy`` block
(§10.2) to both, computed over the stack-gated table set
``src.rbac.get_accessible_tables`` already uses for authorization -- NOT the
legacy per-table ``resource_grants`` rows the existing ``items`` list
carries, which are a no-op for analyst visibility (see ``can_access_table``).

End-to-end over a real ``TestClient`` + real DuckDB (no mocks): a
``server_only`` table filters by ``$user_groups``; a second, unpolicied
table sits in the same stack; a third table's policy depends on a
``policy_mapping`` table that is registered but never synced (§15.1).
"""

from __future__ import annotations

import pytest

ORDERS_POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"
INVOICES_POLICY_SQL = "SELECT * FROM invoices WHERE unit IN (SELECT unit FROM user_access WHERE email = $user_email)"


@pytest.fixture
def policied_workspace(seeded_app, mock_extract_factory, monkeypatch):
    """Four registered tables:

    - ``tbl_orders`` (server_only): policied, filters by ``$user_groups``.
      TeamA sees 2 rows, TeamB sees 1.
    - ``tbl_products``: no policy at all.
    - ``tbl_invoices`` (server_only): policied, depends on a
      ``policy_mapping`` table (``user_access``) that is registered but
      NEVER extracted/synced -- ``sync_state`` carries no row for it,
      simulating an upstream mapping sync that failed or never ran (§15.1).
    - ``user_access``: the (empty/unsynced) mapping table itself. Marking it
      ``policy_mapping=True`` does NOT grant analysts access to it (§15) --
      it is never granted via a data package.

    TeamA is granted ``tbl_orders``, ``tbl_products``, ``tbl_invoices``.
    TeamB is granted only ``tbl_orders``.
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
            {"name": "products", "data": [{"id": "1", "sku": "widget"}]},
            {
                "name": "invoices",
                "data": [
                    {"id": "1", "unit": "TeamA", "total": "10"},
                    {"id": "2", "unit": "TeamB", "total": "20"},
                ],
            },
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)

        registry.register(id="tbl_orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("tbl_orders", sql=ORDERS_POLICY_SQL, note="unit filter", updated_by="admin")

        registry.register(id="tbl_products", name="products", source_type="keboola", query_mode="local")

        registry.register(
            id="tbl_invoices", name="invoices", source_type="keboola", query_mode="local", server_only=True
        )
        registry.set_access_policy("tbl_invoices", sql=INVOICES_POLICY_SQL, note="mapping filter", updated_by="admin")

        # Registered as a mapping table, but never extracted/synced -- no
        # sync_state row at all (the "sync never ran" half of §15.1).
        registry.register(id="user_access", name="user_access", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("user_access", True)

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        users.create(id="u_team_b", email="team-b@example.com", name="Team B")

        grant_table_via_package(conn, "tbl_orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_orders", "u_team_b", group_name="TeamB")
        grant_table_via_package(conn, "tbl_products", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_invoices", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
        "team_b_token": create_access_token("u_team_b", "team-b@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _find(tables: list, table_id: str):
    return next((t for t in tables if t["table_id"] == table_id), None)


def test_me_effective_access_reports_policy_block_for_policied_table(policied_workspace):
    c = policied_workspace["client"]
    r = c.get("/api/me/effective-access", headers=_auth(policied_workspace["team_a_token"]))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_orders")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is True
    assert entry["policy"]["reason"] == "ok"
    assert entry["policy"]["rows_visible"] == 2


def test_me_effective_access_other_persona_sees_their_own_slice(policied_workspace):
    c = policied_workspace["client"]
    r = c.get("/api/me/effective-access", headers=_auth(policied_workspace["team_b_token"]))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_orders")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is True
    assert entry["policy"]["rows_visible"] == 1


def test_me_effective_access_reports_non_policied_table_as_not_applying(policied_workspace):
    c = policied_workspace["client"]
    r = c.get("/api/me/effective-access", headers=_auth(policied_workspace["team_a_token"]))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_products")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is False
    assert entry["policy"]["rows_visible"] is None
    assert entry["policy"]["reason"] == "ok"


def test_admin_effective_access_endpoint_reports_target_users_own_slice(policied_workspace):
    """The admin `/users/{id}/effective-access` endpoint reports the
    TARGET user's slice, not the calling admin's own (unfiltered) view."""
    c = policied_workspace["client"]
    r = c.get(
        "/api/admin/users/u_team_b/effective-access",
        headers=_auth(policied_workspace["admin_token"]),
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_orders")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is True
    assert entry["policy"]["rows_visible"] == 1


def test_admin_target_who_is_admin_sees_unfiltered_count(policied_workspace):
    """§12 admin bypass: `applies` is structural (the table has a policy),
    but `rows_visible` reflects the admin-bypass unfiltered count -- the
    same thing a live `/api/query` call with that identity would show."""
    c = policied_workspace["client"]
    r = c.get(
        "/api/admin/users/admin1/effective-access",
        headers=_auth(policied_workspace["admin_token"]),
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_orders")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is True
    assert entry["policy"]["rows_visible"] == 3
    assert entry["policy"]["reason"] == "ok"


def test_empty_mapping_table_reports_mapping_empty_reason(policied_workspace):
    c = policied_workspace["client"]
    r = c.get("/api/me/effective-access", headers=_auth(policied_workspace["team_a_token"]))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    entry = _find(tables, "tbl_invoices")
    assert entry is not None, tables
    assert entry["policy"]["applies"] is True
    assert entry["policy"]["rows_visible"] is None
    assert entry["policy"]["reason"] == "mapping_empty"
    assert "user_access" in (entry["policy"].get("note") or "")
