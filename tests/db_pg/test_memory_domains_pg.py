"""Integration tests for MemoryDomainsPgRepository.

PG-side smoke. Cross-engine parity covered in
tests/db_pg/test_memory_domains_contract.py (Task 1D.2).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    """Per-test repo bound to a freshly-migrated PG schema.

    Mirrors ``test_data_packages_pg.py``: alembic upgrade head plus a
    couple of ``knowledge_items`` seed rows so the
    ``knowledge_item_domains`` bridge tests have valid ``item_id``
    references to point at. (No FK on ``item_id`` per Task 1A.3, but we
    keep the rows for parity with the future cross-engine contract test.)
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        for kid, title in (("ki_1", "First fact"), ("ki_2", "Second fact")):
            conn.execute(
                sa.text(
                    "INSERT INTO knowledge_items (id, title) "
                    "VALUES (:id, :title)"
                ),
                {"id": kid, "title": title},
            )

    from src.repositories.memory_domains_pg import MemoryDomainsPgRepository
    return MemoryDomainsPgRepository(pg_engine)


def test_create_and_get_by_id(repo):
    did = repo.create(
        name="Sales",
        slug="sales",
        description=None,
        icon=None,
        color=None,
        created_by="u",
    )
    row = repo.get(did)
    assert row is not None
    assert did.startswith("md_")
    assert row["slug"] == "sales"
    assert row["name"] == "Sales"


def test_get_by_slug(repo):
    did = repo.create(
        name="X",
        slug="x",
        description=None,
        icon=None,
        color=None,
        created_by="u",
    )
    by_slug = repo.get_by_slug("x")
    assert by_slug is not None
    assert by_slug["id"] == did


def test_exists_by_slug_returns_bool(repo):
    repo.create(
        name="X",
        slug="x",
        description=None,
        icon=None,
        color=None,
        created_by="u",
    )
    assert repo.exists_by_slug("x") is True
    assert repo.exists_by_slug("nope") is False


def test_delete_then_restore_round_trip(repo):
    did = repo.create(
        name="X",
        slug="x",
        description=None,
        icon=None,
        color=None,
        created_by="u",
    )
    repo.delete(did)
    assert repo.get(did) is None
    assert repo.get(did, include_deleted=True) is not None
    repo.restore(did)
    assert repo.get(did) is not None


def test_add_item_then_list_items_of_domain(repo):
    did = repo.create(
        name="Sales",
        slug="sales",
        description=None,
        icon=None,
        color=None,
        created_by="u",
    )
    added = repo.add_item(did, "ki_1", added_by="u")
    assert added is True
    again = repo.add_item(did, "ki_1", added_by="u")
    assert again is False  # idempotent
    repo.add_item(did, "ki_2", added_by="u")
    rows = repo.list_items_of_domain(did)
    assert sorted(r["id"] for r in rows) == ["ki_1", "ki_2"]


def test_list_domains_of_item_pivots_correctly(repo):
    a = repo.create(
        name="A", slug="a", description=None, icon=None, color=None,
        created_by="u",
    )
    b = repo.create(
        name="B", slug="b", description=None, icon=None, color=None,
        created_by="u",
    )
    repo.add_item(a, "ki_1", added_by="u")
    repo.add_item(b, "ki_1", added_by="u")
    domains = repo.list_domains_of_item("ki_1")
    assert sorted(d["id"] for d in domains) == sorted([a, b])
