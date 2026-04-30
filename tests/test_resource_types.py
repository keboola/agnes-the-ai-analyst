"""Tests for app.resource_types — registry + list_blocks delegates.

Focus on the TABLE resource type. The marketplace projection has integration
coverage elsewhere (test_marketplace_*.py); here we exercise the table
projection and the wiring into /api/admin/access-overview.
"""

from __future__ import annotations

import pytest

from app.resource_types import (
    RESOURCE_TYPES,
    ResourceType,
    _table_blocks,
)
from src.db import get_system_db
from src.repositories.table_registry import TableRegistryRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_conn(seeded_app):
    """Open a system DB connection for the active test DATA_DIR.

    seeded_app sets up DATA_DIR via the e2e_env fixture and seeds users; we
    just need the system DB here. Closed by the fixture teardown.
    """
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


class TestTableBlocks:
    def test_groups_by_bucket(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(id="t_finance_a", name="finance_a", bucket="in.c-finance",
                      source_type="dummy")
        repo.register(id="t_finance_b", name="finance_b", bucket="in.c-finance",
                      source_type="dummy")
        repo.register(id="t_marketing_a", name="marketing_a",
                      bucket="in.c-marketing", source_type="dummy")

        blocks = _table_blocks(system_conn)
        by_name = {b["name"]: b for b in blocks}

        assert "in.c-finance" in by_name
        assert "in.c-marketing" in by_name
        assert len(by_name["in.c-finance"]["items"]) == 2
        assert len(by_name["in.c-marketing"]["items"]) == 1

        finance_ids = {it["resource_id"] for it in by_name["in.c-finance"]["items"]}
        assert finance_ids == {"t_finance_a", "t_finance_b"}

    def test_item_shape_matches_ui_contract(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(
            id="shape_test", name="shape_test", bucket="b1",
            source_type="keboola", query_mode="remote",
            description="hello",
        )

        blocks = _table_blocks(system_conn)
        item = blocks[0]["items"][0]

        # Fields that admin_access.html renderResources reads:
        assert item["resource_id"] == "shape_test"
        assert item["name"] == "shape_test"
        assert item["category"] == "remote"        # query_mode → badge
        assert item["source_type"] == "keboola"    # → badge
        assert item["description"] == "hello"

    def test_handles_null_or_empty_bucket(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(id="orphan", name="orphan", source_type="dummy")
        # bucket left as None

        blocks = _table_blocks(system_conn)
        names = {b["name"] for b in blocks}
        assert "(no bucket)" in names
        orphan_block = next(b for b in blocks if b["name"] == "(no bucket)")
        assert orphan_block["items"][0]["resource_id"] == "orphan"

    def test_empty_registry_returns_empty_list(self, system_conn):
        # Fresh DB, no tables registered yet.
        assert _table_blocks(system_conn) == []


class TestResourceTypeRegistration:
    def test_table_is_in_registry(self):
        assert ResourceType.TABLE in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.TABLE]
        assert spec.key is ResourceType.TABLE
        assert spec.display_name == "Tables"
        assert callable(spec.list_blocks)

    def test_enum_value_persisted_form(self):
        # Stored verbatim in resource_grants.resource_type — guard against
        # accidental rename.
        assert ResourceType.TABLE.value == "table"


class TestAccessOverviewIncludesTables:
    """v19+ — TABLE is unconditionally enabled (no env-gate)."""

    def test_tables_section_present(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        type_keys = {r["type_key"] for r in resp.json()["resources"]}
        assert "table" in type_keys
        assert "marketplace_plugin" in type_keys  # regression — still there

    def test_seeded_tables_appear_in_overview(self, seeded_app):
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="overview_test", name="overview_test",
                bucket="in.c-overview", source_type="dummy",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        tables_section = next(
            r for r in resp.json()["resources"] if r["type_key"] == "table"
        )
        all_resource_ids = {
            it["resource_id"]
            for block in tables_section["blocks"]
            for it in block["items"]
        }
        assert "overview_test" in all_resource_ids


class TestTableGrantsAlwaysOn:
    """v19+ — the env-gate AGNES_ENABLE_TABLE_GRANTS was removed; TABLE is
    listed unconditionally and grants succeed without a feature flag."""

    def test_resource_types_endpoint_includes_table(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/resource-types",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        keys = {r["key"] for r in resp.json()}
        assert "table" in keys
        assert "marketplace_plugin" in keys

    def test_create_table_grant_accepted(self, seeded_app):
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ff_table", name="ff_table",
                bucket="in.c-ff", source_type="dummy",
            )
        finally:
            conn.close()
        c = seeded_app["client"]
        admin = _auth(seeded_app["admin_token"])
        gresp = c.post(
            "/api/admin/groups",
            headers=admin,
            json={"name": "table-grant-on"},
        )
        assert gresp.status_code == 201
        gid = gresp.json()["id"]
        resp = c.post(
            "/api/admin/grants",
            headers=admin,
            json={
                "group_id": gid,
                "resource_type": "table",
                "resource_id": "ff_table",
            },
        )
        assert resp.status_code == 201
