"""Repository tests for ``memory_domains`` + ``knowledge_item_domains`` (v49).

The schema migration seeds six canonical rows (md_finance, md_engineering, …)
so a freshly-migrated DB starts non-empty. Each test explicitly wipes the
seeded rows when it needs a clean slate; otherwise it relies on them.
"""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.memory_domains import MemoryDomainsRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return MemoryDomainsRepository(conn)


@pytest.fixture
def clean_repo(repo):
    """Drop seeded canonical domains for tests that need a known-empty table."""
    repo.conn.execute("DELETE FROM knowledge_item_domains")
    repo.conn.execute("DELETE FROM memory_domains")
    return repo


class TestCreateAndRead:
    def test_create_assigns_md_prefix_id(self, clean_repo):
        did = clean_repo.create(
            name="Sales coaching", slug="sales-coaching",
            description=None, icon="🎯", color="#abc", created_by="admin",
        )
        assert did.startswith("md_")

    def test_get(self, clean_repo):
        did = clean_repo.create(
            name="Sales coaching", slug="sales-coaching",
            description=None, icon=None, color=None, created_by="admin",
        )
        d = clean_repo.get(did)
        assert d["slug"] == "sales-coaching"
        assert d["name"] == "Sales coaching"

    def test_get_returns_none_for_unknown(self, clean_repo):
        assert clean_repo.get("md_nope") is None

    def test_get_by_slug(self, clean_repo):
        clean_repo.create(
            name="Sales coaching", slug="sales-coaching",
            description=None, icon=None, color=None, created_by="admin",
        )
        d = clean_repo.get_by_slug("sales-coaching")
        assert d is not None
        assert d["name"] == "Sales coaching"

    def test_exists_by_slug(self, clean_repo):
        clean_repo.create(
            name="Sales coaching", slug="sales-coaching",
            description=None, icon=None, color=None, created_by="admin",
        )
        assert clean_repo.exists_by_slug("sales-coaching") is True
        assert clean_repo.exists_by_slug("missing") is False


class TestSeededCanonicalDomains:
    """v49 migration seeds six canonical rows — the repo should surface them."""

    def test_canonical_slugs_present(self, repo):
        slugs = {d["slug"] for d in repo.list()}
        assert {"finance", "engineering", "product", "data",
                "operations", "infrastructure"}.issubset(slugs)


class TestUpdate:
    def test_update_metadata(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance",
            description="old", icon=None, color=None, created_by="admin",
        )
        clean_repo.update(did, name="Finance+", description="new",
                          icon="💰", color="#dcfce7")
        d = clean_repo.get(did)
        assert d["name"] == "Finance+"
        assert d["description"] == "new"
        assert d["icon"] == "💰"
        assert d["color"] == "#dcfce7"

    def test_update_partial_keeps_other_fields(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description="desc",
            icon="💰", color="#abc", created_by="admin",
        )
        clean_repo.update(did, name="Finance+")
        d = clean_repo.get(did)
        assert d["description"] == "desc"
        assert d["icon"] == "💰"


class TestDelete:
    def test_delete_hides_row_from_get(self, clean_repo):
        # v54: delete() is now a soft delete (sets deleted_at).
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.delete(did)
        assert clean_repo.get(did) is None
        assert clean_repo.get(did, include_deleted=True) is not None

    def test_delete_preserves_junction(self, clean_repo):
        # v54: junction rows survive soft-delete so restore brings the
        # domain back whole (knowledge_item_domains untouched).
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        clean_repo.add_item(did, "k1", added_by="admin")
        clean_repo.delete(did)
        n = clean_repo.conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_domains WHERE domain_id = ?",
            [did],
        ).fetchone()[0]
        assert n == 1

    def test_restore_brings_row_back(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.delete(did)
        assert clean_repo.get(did) is None
        clean_repo.restore(did)
        assert clean_repo.get(did) is not None

    def test_hard_delete_cascades_junction(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        clean_repo.add_item(did, "k1", added_by="admin")
        clean_repo.hard_delete(did)
        n = clean_repo.conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_domains WHERE domain_id = ?",
            [did],
        ).fetchone()[0]
        assert n == 0
        assert clean_repo.get(did, include_deleted=True) is None


class TestSlugUniqueness:
    def test_duplicate_slug_raises(self, clean_repo):
        clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        with pytest.raises(duckdb.ConstraintException):
            clean_repo.create(
                name="Finance B", slug="finance", description=None,
                icon=None, color=None, created_by="admin",
            )


class TestItemJunction:
    def test_add_item_inserts_row(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        assert clean_repo.add_item(did, "k1", added_by="admin") is True

    def test_add_item_idempotent(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        clean_repo.add_item(did, "k1", added_by="admin")
        assert clean_repo.add_item(did, "k1", added_by="admin") is False

    def test_remove_item(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        clean_repo.add_item(did, "k1", added_by="admin")
        assert clean_repo.remove_item(did, "k1") is True
        assert clean_repo.list_items_of_domain(did) == []

    def test_list_items_of_domain(self, clean_repo):
        did = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 'A', 'approved')"
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k2', 'B', 'approved')"
        )
        clean_repo.add_item(did, "k1", added_by="admin")
        clean_repo.add_item(did, "k2", added_by="admin")
        items = clean_repo.list_items_of_domain(did)
        assert {it["id"] for it in items} == {"k1", "k2"}

    def test_list_domains_of_item(self, clean_repo):
        d1 = clean_repo.create(
            name="Finance", slug="finance", description=None,
            icon=None, color=None, created_by="admin",
        )
        d2 = clean_repo.create(
            name="Engineering", slug="engineering", description=None,
            icon=None, color=None, created_by="admin",
        )
        clean_repo.conn.execute(
            "INSERT INTO knowledge_items(id, title, status) VALUES ('k1', 't', 'approved')"
        )
        clean_repo.add_item(d1, "k1", added_by="admin")
        clean_repo.add_item(d2, "k1", added_by="admin")
        domains = clean_repo.list_domains_of_item("k1")
        assert {d["id"] for d in domains} == {d1, d2}
