"""v49 repo refactor: knowledge.py reads/writes ``domain`` through the
``knowledge_item_domains`` junction; mandatory tier rides on the new
``knowledge_items.is_required`` boolean (status='mandatory' overload is gone)."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.memory_domains import MemoryDomainsRepository


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    return c


@pytest.fixture
def repo(conn):
    return KnowledgeRepository(conn)


class TestDomainJunctionReads:
    def test_create_with_domain_populates_junction(self, conn, repo):
        # v49 migration seeded canonical domains incl. 'finance'.
        repo.create(
            id="k1", title="Q4 budget", content="x", category="finance",
            domain="finance",
        )
        # Junction row exists pointing at md_finance.
        row = conn.execute(
            "SELECT md.slug FROM knowledge_item_domains kid "
            "JOIN memory_domains md ON md.id = kid.domain_id "
            "WHERE kid.item_id = 'k1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "finance"

    def test_list_items_with_domain_filter_uses_junction(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", domain="finance")
        repo.create(id="k2", title="B", content="x", category="c", domain="engineering")
        items = repo.list_items(domain="finance")
        assert {it["id"] for it in items} == {"k1"}

    def test_list_items_unknown_domain_returns_empty(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", domain="finance")
        items = repo.list_items(domain="no-such-domain")
        assert items == []

    def test_update_replaces_junction_membership(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", domain="finance")
        repo.update(item_id="k1", domain="engineering")
        rows = conn.execute(
            "SELECT md.slug FROM knowledge_item_domains kid "
            "JOIN memory_domains md ON md.id = kid.domain_id "
            "WHERE kid.item_id = 'k1'"
        ).fetchall()
        assert [r[0] for r in rows] == ["engineering"]

    def test_update_unknown_domain_raises(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c")
        with pytest.raises(ValueError):
            repo.update(item_id="k1", domain="bogus-slug")

    def test_create_unknown_domain_raises(self, conn, repo):
        with pytest.raises(ValueError):
            repo.create(id="k1", title="A", content="x", category="c", domain="bogus")

    def test_create_without_domain_writes_no_junction_row(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c")
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_domains WHERE item_id = 'k1'"
        ).fetchone()[0]
        assert n == 0


class TestIsRequiredFlag:
    def test_set_is_required_writes_flag_only(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", status="approved")
        repo.set_is_required("k1", True)
        row = conn.execute(
            "SELECT is_required, status FROM knowledge_items WHERE id='k1'"
        ).fetchone()
        assert row[0] is True
        assert row[1] == "approved"

    def test_set_is_required_false(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", status="approved")
        repo.set_is_required("k1", True)
        repo.set_is_required("k1", False)
        row = conn.execute(
            "SELECT is_required FROM knowledge_items WHERE id='k1'"
        ).fetchone()
        assert row[0] is False


class TestDismissEXISTSGuardUsesIsRequired:
    def test_required_item_visible_when_dismissed(self, conn, repo):
        """A dismissed item with ``is_required=TRUE`` still appears in
        list_items — the governance hard rule the SQL guard enforces."""
        repo.create(id="k_req", title="Required", content="x", category="c",
                    status="approved")
        repo.create(id="k_norm", title="Normal", content="x", category="c",
                    status="approved")
        repo.set_is_required("k_req", True)
        # User dismisses both.
        conn.execute("INSERT INTO knowledge_item_user_dismissed(user_id, item_id) "
                     "VALUES ('u1', 'k_req')")
        conn.execute("INSERT INTO knowledge_item_user_dismissed(user_id, item_id) "
                     "VALUES ('u1', 'k_norm')")
        visible = {
            it["id"]
            for it in repo.list_items(
                user_groups=[], hide_dismissed=True, dismissed_by_user="u1"
            )
        }
        assert "k_req" in visible
        assert "k_norm" not in visible


class TestListByIsRequired:
    def test_filter_by_is_required_true(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", status="approved")
        repo.create(id="k2", title="B", content="x", category="c", status="approved")
        repo.set_is_required("k1", True)
        items = repo.list_items(is_required=True)
        assert {it["id"] for it in items} == {"k1"}

    def test_filter_by_is_required_false(self, conn, repo):
        repo.create(id="k1", title="A", content="x", category="c", status="approved")
        repo.create(id="k2", title="B", content="x", category="c", status="approved")
        repo.set_is_required("k1", True)
        items = repo.list_items(is_required=False)
        assert {it["id"] for it in items} == {"k2"}


class TestGrantedDomainsFilter:
    def test_granted_domain_ids_join_via_junction(self, conn, repo):
        repo.create(id="k_fin", title="Finance item", content="x",
                    category="c", domain="finance")
        repo.create(id="k_eng", title="Engineering item", content="x",
                    category="c", domain="engineering")
        # Restrict audience so the visibility OR-clause depends on the
        # granted-domain branch (audience IS NULL/all otherwise lets every
        # item through to non-admin callers).
        conn.execute("UPDATE knowledge_items SET audience = 'group:finance-team'")
        # Resolve domain ids
        md_finance = conn.execute(
            "SELECT id FROM memory_domains WHERE slug='finance'"
        ).fetchone()[0]
        items = repo.list_items(user_groups=[], granted_domains=[md_finance])
        ids = {it["id"] for it in items}
        # 'k_fin' visible via the granted domain join.
        assert "k_fin" in ids
        # 'k_eng' has neither audience match nor a granted domain → hidden.
        assert "k_eng" not in ids
