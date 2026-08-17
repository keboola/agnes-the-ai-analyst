"""Table access policy builder — Task 2 of the no-SQL builder UX plan
(``docs/superpowers/plans/2026-08-17-access-policy-builder-ux.md``).

``GET /api/admin/registry/{table_id}/policy/columns`` gives the builder UI
real schema + sample values so an admin never has to know a table's
structure up front — the column list Task 3's ``policy/compile`` spec is
built from.

Mirrors ``tests/test_admin_access_policy_api.py`` for the ``seeded_app`` +
``mock_extract_factory`` + real-data fixture shape; the new route sits
right next to ``preview_table_policy`` and shares its posture (admin-only,
never gated on ``access_policies.enabled`` — that flag gates ATTACHING a
policy via PUT only, per its own hint text in ``app/api/admin.py``).
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def policy_builder_table(seeded_app, mock_extract_factory):
    """A real, ``server_only`` (eligible) local table with a handful of
    columns a mask/row-rule spec can reference, synced through the real
    orchestrator so ``DESCRIBE`` on the analytics connection has something
    to report — mirrors ``policied_invoices_for_preview`` in
    ``tests/test_admin_access_policy_api.py``.
    """
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "policy_builder_invoices",
                "data": [
                    {
                        "invoice_id": "1",
                        "cost_center": "Finance",
                        "email": "a@example.com",
                        "national_id": "111",
                        "amount_eur": "100",
                    },
                    {
                        "invoice_id": "2",
                        "cost_center": "Ops",
                        "email": "b@example.com",
                        "national_id": "222",
                        "amount_eur": "200",
                    },
                ],
            }
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="policy_builder_invoices",
            name="policy_builder_invoices",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
    finally:
        conn.close()

    return seeded_app


@pytest.fixture
def policy_builder_table_with_profile(policy_builder_table):
    """Same table as ``policy_builder_table``, plus a saved profile so the
    columns endpoint's ``samples``/``distinct`` fields have real data to
    surface (the profile is content computed at sync time, independent of
    this test's assertions about the live endpoint)."""
    from src.repositories import profile_repo

    profile_repo().save(
        "policy_builder_invoices",
        {
            "columns": [
                {
                    "name": "invoice_id",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["1", "2"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "cost_center",
                    "type": "VARCHAR",
                    "type_category": "CATEGORICAL",
                    "sample_values": ["Finance", "Ops"],
                    "unique_count": 2,
                    "alerts": [],
                },
                {
                    "name": "email",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["a@example.com", "b@example.com"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "national_id",
                    "type": "VARCHAR",
                    "type_category": "TEXT",
                    "sample_values": ["111", "222"],
                    "unique_count": 2,
                    "alerts": ["unique"],
                },
                {
                    "name": "amount_eur",
                    "type": "VARCHAR",
                    "type_category": "NUMERIC",
                    "sample_values": ["100", "200"],
                    "unique_count": 2,
                    "alerts": [],
                },
            ]
        },
    )
    return policy_builder_table


# ── Task 2: GET /registry/{table_id}/policy/columns ────────────────────


@pytest.mark.journey
class TestPolicyBuilderColumns:
    def test_columns_endpoint_returns_schema_and_samples(self, policy_builder_table_with_profile):
        c = policy_builder_table_with_profile["client"]
        token = policy_builder_table_with_profile["admin_token"]

        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        names = [col["name"] for col in body["columns"]]
        assert "email" in names
        assert "national_id" in names
        assert all("type" in col and "samples" in col for col in body["columns"])

        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["email"]["samples"] == ["a@example.com", "b@example.com"]
        assert by_name["email"]["distinct"] == 2
        assert by_name["email"]["pii"] is True

        assert "mapping_tables" in body
        assert body["eligible"] is True

    def test_columns_endpoint_without_a_profile_returns_empty_samples(self, policy_builder_table):
        """No profile has been saved yet — the endpoint must still answer
        200 with real column names/types, just no samples (never 500)."""
        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]

        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = [col["name"] for col in body["columns"]]
        assert "cost_center" in names
        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["cost_center"]["samples"] == []
        assert by_name["cost_center"]["distinct"] is None

    def test_columns_endpoint_reports_ineligible_table(self, seeded_app):
        """A table that is neither ``query_mode='remote'`` nor
        ``server_only`` can't carry a policy at all (the distribution
        interlock) — the endpoint still answers 200 so the builder UI can
        show the "make server-only first" nudge (Task 5), it just reports
        ``eligible: false``."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ineligible_tbl",
                name="ineligible_tbl",
                source_type="keboola",
                query_mode="local",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/ineligible_tbl/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["eligible"] is False

    def test_columns_endpoint_lists_mapping_tables(self, policy_builder_table):
        from src.repositories import table_registry_repo

        table_registry_repo().set_policy_mapping("policy_builder_invoices", True)

        c = policy_builder_table["client"]
        token = policy_builder_table["admin_token"]
        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert "policy_builder_invoices" in resp.json()["mapping_tables"]

    def test_columns_endpoint_is_admin_only(self, policy_builder_table):
        c = policy_builder_table["client"]
        token = policy_builder_table["analyst_token"]
        resp = c.get(
            "/api/admin/registry/policy_builder_invoices/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text

    def test_columns_endpoint_404_for_unknown_table(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/admin/registry/does-not-exist/policy/columns",
            headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text
