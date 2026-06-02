"""Cross-engine contract tests for the recipes repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_memory_domain_suggestions_contract.py``
(Task 1D.3). No FK-target seeding needed for recipes — the
``related_table_ids`` JSONB array is just a list of opaque table-id
strings, with no enforced bridge to another table.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.recipes import RecipesRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return RecipesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.recipes_pg import RecipesPgRepository
    return RecipesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a recipes repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------

def test_create_then_get(repo):
    rid = repo.create(
        slug="top-customers",
        title="Top customers",
        description="Find top N customers by revenue",
        icon=None,
        color=None,
        sql_template="SELECT customer_id, SUM(revenue) ...",
        related_table_ids=["orders", "customers"],
        created_by="alice@example.com",
    )
    row = repo.get(rid)
    assert row is not None
    assert rid.startswith("rcp_")
    assert row["id"] == rid
    assert row["slug"] == "top-customers"
    assert row["title"] == "Top customers"
    assert row["description"] == "Find top N customers by revenue"
    assert row["sql_template"] == "SELECT customer_id, SUM(revenue) ..."
    assert row["related_table_ids"] == ["orders", "customers"]
    assert row["created_by"] == "alice@example.com"


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
    assert repo.get_by_slug("missing") is None


def test_search_filters_by_title(repo):
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
        slug="ghost",
        title="Ghost",
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="u",
    )
    repo.delete(rid)
    assert repo.get(rid) is None
    assert repo.get_by_slug("ghost") is None
    assert repo.get(rid, include_deleted=True) is not None
    repo.restore(rid)
    assert repo.get(rid) is not None


def test_update_related_table_ids_jsonb_round_trip(repo):
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
    # Initial state: NULL related_table_ids reads back as [] on both sides.
    pre = repo.get(rid)
    assert pre["related_table_ids"] == []
    repo.update(rid, related_table_ids=["orders"])
    row = repo.get(rid)
    assert row["related_table_ids"] == ["orders"]
