"""Integration tests for DataPackagesPgRepository.

PG-side smoke covering CRUD, soft-delete round-trip, the M:N junction with
table_registry, and the bulk-listing fast path. Cross-engine parity is
covered separately in ``tests/test_data_packages_contract.py`` (Task 1D.1).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    """Per-test repo bound to a freshly-migrated PG schema.

    Mirrors the pattern from ``test_store_pg.py``: alembic upgrade head
    on the per-test ``public`` schema, plus seeding two ``table_registry``
    rows so the ``list_tables`` JOIN has real targets to point at (the
    DuckDB contract joins ``data_package_tables`` against
    ``table_registry`` and returns ``{id, name}`` pairs).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        for tid, name in (("t1", "orders"), ("t2", "customers"), ("t3", "events")):
            conn.execute(
                sa.text(
                    "INSERT INTO table_registry (id, name) VALUES (:id, :name)"
                ),
                {"id": tid, "name": name},
            )

    from src.repositories.data_packages_pg import DataPackagesPgRepository
    return DataPackagesPgRepository(pg_engine)


def test_create_and_get_by_id_returns_row(repo):
    pkg_id = repo.create(
        name="Sales metrics",
        slug="sales-metrics",
        description="Pack of sales analysis tables",
        icon="📊",
        color="#0ea5e9",
        created_by="admin@example.com",
        tags=["sales", "kpi"],
    )
    row = repo.get(pkg_id)
    assert row is not None
    assert row["id"] == pkg_id
    assert pkg_id.startswith("pkg_")
    assert row["slug"] == "sales-metrics"
    assert row["name"] == "Sales metrics"
    assert row["tags"] == ["sales", "kpi"]
    assert row["created_by"] == "admin@example.com"


def test_get_by_slug_resolves_to_same_row(repo):
    pkg_id = repo.create(
        name="X", slug="x-pkg", description=None,
        icon=None, color=None, created_by="u",
    )
    by_slug = repo.get_by_slug("x-pkg")
    assert by_slug is not None
    assert by_slug["id"] == pkg_id


def test_delete_then_restore_round_trip(repo):
    pkg_id = repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.delete(pkg_id)
    assert repo.get(pkg_id) is None
    assert repo.get(pkg_id, include_deleted=True) is not None
    repo.restore(pkg_id)
    assert repo.get(pkg_id) is not None


def test_add_table_then_list_tables(repo):
    pkg_id = repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
    )
    added = repo.add_table(pkg_id, "t1", added_by="u")
    assert added is True
    again = repo.add_table(pkg_id, "t1", added_by="u")
    assert again is False  # idempotent
    tables = repo.list_tables(pkg_id)
    # DuckDB sibling returns {id, name} joined against table_registry.
    assert [t["id"] for t in tables] == ["t1"]
    assert [t["name"] for t in tables] == ["orders"]


def test_list_member_ids_bulk_returns_per_package_lists(repo):
    a = repo.create(
        name="A", slug="a", description=None,
        icon=None, color=None, created_by="u",
    )
    b = repo.create(
        name="B", slug="b", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.add_table(a, "t1", added_by="u")
    repo.add_table(a, "t2", added_by="u")
    repo.add_table(b, "t3", added_by="u")
    bulk = repo.list_member_ids_bulk()
    assert sorted(bulk[a]) == ["t1", "t2"]
    assert bulk[b] == ["t3"]


def test_update_partial_fields(repo):
    pkg_id = repo.create(
        name="A", slug="a", description="old",
        icon=None, color=None, created_by="u",
    )
    repo.update(pkg_id, description="new", tags=["x"])
    row = repo.get(pkg_id)
    assert row["description"] == "new"
    assert row["tags"] == ["x"]
    assert row["name"] == "A"  # untouched
