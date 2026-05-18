"""Repository tests for ``data_packages`` + ``data_package_tables`` (v49).

Covers CRUD, slug uniqueness, the M:N junction with ``table_registry``, and
cascade-on-delete invariants. No FastAPI / HTTP wiring — pure repo + DuckDB.
"""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.data_packages import DataPackagesRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    # Seed two tables in the registry so add_table / list_tables have real
    # rows to point at.
    conn.execute("INSERT INTO table_registry(id, name) VALUES ('t1', 'orders')")
    conn.execute("INSERT INTO table_registry(id, name) VALUES ('t2', 'customers')")
    return DataPackagesRepository(conn)


class TestCreateAndRead:
    def test_create_assigns_id_starting_with_pkg(self, repo):
        pkg_id = repo.create(
            name="Sales bundle", slug="sales",
            description="All sales tables",
            icon="📦", color="#fce7f3", created_by="admin",
        )
        assert pkg_id.startswith("pkg_")

    def test_get_returns_dict_with_all_columns(self, repo):
        pkg_id = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
        )
        pkg = repo.get(pkg_id)
        assert pkg["id"] == pkg_id
        assert pkg["slug"] == "sales"
        assert pkg["name"] == "Sales"
        assert pkg["created_by"] == "admin"
        assert pkg["created_at"] is not None

    def test_get_returns_none_for_unknown_id(self, repo):
        assert repo.get("pkg_nope") is None

    def test_get_by_slug(self, repo):
        repo.create(name="Sales", slug="sales", description=None,
                    icon=None, color=None, created_by="admin")
        pkg = repo.get_by_slug("sales")
        assert pkg is not None
        assert pkg["name"] == "Sales"

    def test_get_by_slug_returns_none_when_missing(self, repo):
        assert repo.get_by_slug("missing") is None


class TestList:
    def test_list_returns_packages_in_name_order(self, repo):
        repo.create(name="Zeta", slug="z", description=None, icon=None, color=None, created_by="a")
        repo.create(name="Alpha", slug="a", description=None, icon=None, color=None, created_by="a")
        repo.create(name="Mike", slug="m", description=None, icon=None, color=None, created_by="a")
        names = [p["name"] for p in repo.list()]
        assert names == ["Alpha", "Mike", "Zeta"]

    def test_list_search_filters_by_name(self, repo):
        repo.create(name="Sales bundle", slug="sb", description=None,
                    icon=None, color=None, created_by="a")
        repo.create(name="Finance pack", slug="fp", description=None,
                    icon=None, color=None, created_by="a")
        results = repo.list(search="sales")
        assert {r["slug"] for r in results} == {"sb"}


class TestUpdate:
    def test_update_metadata(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.update(pkg_id, name="Sales+", description="updated",
                    icon="🎯", color="#abc")
        pkg = repo.get(pkg_id)
        assert pkg["name"] == "Sales+"
        assert pkg["description"] == "updated"
        assert pkg["icon"] == "🎯"
        assert pkg["color"] == "#abc"

    def test_update_partial_keeps_other_fields(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description="desc",
                             icon="📦", color="#abc", created_by="admin")
        repo.update(pkg_id, name="Sales+")
        pkg = repo.get(pkg_id)
        assert pkg["description"] == "desc"
        assert pkg["icon"] == "📦"
        assert pkg["color"] == "#abc"

    def test_update_with_no_fields_is_noop(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.update(pkg_id)  # nothing to do; no exception
        assert repo.get(pkg_id)["name"] == "Sales"


class TestDelete:
    def test_delete_hides_row_from_get(self, repo):
        # v54: delete() is now a soft delete. get() filters
        # ``deleted_at IS NULL`` so the row vanishes from the default
        # read path even though it's still on disk.
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.delete(pkg_id)
        assert repo.get(pkg_id) is None
        # include_deleted=True is the escape hatch /restore uses.
        assert repo.get(pkg_id, include_deleted=True) is not None

    def test_delete_preserves_junction(self, repo):
        # v54: junction rows survive soft-delete so restore brings the
        # package back whole. (Hard-delete still cascades — covered below.)
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.add_table(pkg_id, "t1", added_by="admin")
        repo.delete(pkg_id)
        n = repo.conn.execute(
            "SELECT COUNT(*) FROM data_package_tables WHERE package_id = ?",
            [pkg_id],
        ).fetchone()[0]
        assert n == 1

    def test_restore_brings_row_back(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.delete(pkg_id)
        assert repo.get(pkg_id) is None
        repo.restore(pkg_id)
        assert repo.get(pkg_id) is not None

    def test_hard_delete_cascades_junction(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.add_table(pkg_id, "t1", added_by="admin")
        repo.hard_delete(pkg_id)
        n = repo.conn.execute(
            "SELECT COUNT(*) FROM data_package_tables WHERE package_id = ?",
            [pkg_id],
        ).fetchone()[0]
        assert n == 0
        # Row is gone even from include_deleted view.
        assert repo.get(pkg_id, include_deleted=True) is None


class TestSlugUniqueness:
    def test_duplicate_slug_raises(self, repo):
        repo.create(name="Sales", slug="sales", description=None,
                    icon=None, color=None, created_by="admin")
        with pytest.raises(duckdb.ConstraintException):
            repo.create(name="Sales B", slug="sales", description=None,
                        icon=None, color=None, created_by="admin")


class TestTableJunction:
    def test_add_table_inserts_row(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        assert repo.add_table(pkg_id, "t1", added_by="admin") is True

    def test_add_table_idempotent(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.add_table(pkg_id, "t1", added_by="admin")
        # Second add returns False (already present), no exception.
        assert repo.add_table(pkg_id, "t1", added_by="admin") is False

    def test_remove_table(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.add_table(pkg_id, "t1", added_by="admin")
        assert repo.remove_table(pkg_id, "t1") is True
        assert repo.list_tables(pkg_id) == []

    def test_remove_missing_table_returns_false(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        assert repo.remove_table(pkg_id, "t1") is False

    def test_list_tables(self, repo):
        pkg_id = repo.create(name="Sales", slug="sales", description=None,
                             icon=None, color=None, created_by="admin")
        repo.add_table(pkg_id, "t1", added_by="admin")
        repo.add_table(pkg_id, "t2", added_by="admin")
        tables = repo.list_tables(pkg_id)
        assert {t["id"] for t in tables} == {"t1", "t2"}

    def test_list_packages_of_table(self, repo):
        p1 = repo.create(name="Sales", slug="sales", description=None,
                         icon=None, color=None, created_by="admin")
        p2 = repo.create(name="Finance", slug="finance", description=None,
                         icon=None, color=None, created_by="admin")
        repo.add_table(p1, "t1", added_by="admin")
        repo.add_table(p2, "t1", added_by="admin")
        pkgs = repo.list_packages_of_table("t1")
        assert {p["id"] for p in pkgs} == {p1, p2}
