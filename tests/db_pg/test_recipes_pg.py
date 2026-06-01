"""Integration tests for RecipesPgRepository.

PG-side smoke. Cross-engine parity covered in Task 1D.4 contract test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    """Per-test repo bound to a freshly-migrated PG schema."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.recipes_pg import RecipesPgRepository
    return RecipesPgRepository(pg_engine)


def test_create_and_get(repo):
    rid = repo.create(
        slug="top-customers",
        title="Top customers",
        description="Find top N customers by revenue",
        icon=None,
        color=None,
        sql_template="SELECT customer_id, SUM(revenue) ...",
        related_table_ids=["orders", "customers"],
        created_by="u",
    )
    row = repo.get(rid)
    assert row is not None
    assert rid.startswith("rcp_")
    assert row["slug"] == "top-customers"
    assert row["title"] == "Top customers"
    assert row["related_table_ids"] == ["orders", "customers"]


def test_get_by_slug(repo):
    rid = repo.create(
        slug="x",
        title="X",
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    by_slug = repo.get_by_slug("x")
    assert by_slug is not None
    assert by_slug["id"] == rid


def test_list_search_filters_by_title(repo):
    repo.create(
        slug="a",
        title="Top customers",
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    repo.create(
        slug="b",
        title="Churn analysis",
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    matches = repo.list(search="customers")
    assert len(matches) == 1
    assert matches[0]["slug"] == "a"


def test_delete_restore(repo):
    rid = repo.create(
        slug="x",
        title="X",
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    repo.delete(rid)
    assert repo.get(rid) is None
    repo.restore(rid)
    assert repo.get(rid) is not None


def test_update_partial_with_related_table_ids_jsonb(repo):
    rid = repo.create(
        slug="x",
        title="X",
        description="old",
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    repo.update(rid, description="new", related_table_ids=["orders"])
    row = repo.get(rid)
    assert row["description"] == "new"
    assert row["related_table_ids"] == ["orders"]
