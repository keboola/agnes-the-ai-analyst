"""Cross-engine contract tests for the file_corpora repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both backends; the same return shapes must come back.

Follows the pattern established in test_data_packages_contract.py.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.file_corpora import FileCorporaRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return FileCorporaRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
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

    from src.repositories.file_corpora_pg import FileCorporaPgRepository

    return FileCorporaPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a file_corpora repo bound to either DuckDB or PG."""
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
# contract tests
# ---------------------------------------------------------------------------


def test_create_then_get_returns_same_shape(repo):
    corpus_id = repo.create(
        name="My Collection",
        slug="my-collection",
        description="Test desc",
        created_by="user1",
    )
    row = repo.get(corpus_id)
    assert row is not None
    assert row["id"] == corpus_id
    assert row["slug"] == "my-collection"
    assert row["name"] == "My Collection"
    assert row["description"] == "Test desc"
    assert row["created_by"] == "user1"
    assert row["deleted_at"] is None


def test_create_id_has_col_prefix(repo):
    corpus_id = repo.create(name="X", slug="x", description=None, created_by="u")
    assert corpus_id.startswith("col_")


def test_create_ids_are_unique(repo):
    id1 = repo.create(name="A", slug="a", description=None, created_by="u")
    id2 = repo.create(name="B", slug="b", description=None, created_by="u")
    assert id1 != id2


def test_get_returns_none_when_missing(repo):
    assert repo.get("col_nonexistent") is None


def test_get_by_slug_resolves(repo):
    corpus_id = repo.create(name="A", slug="a-slug", description=None, created_by="u")
    found = repo.get_by_slug("a-slug")
    assert found is not None
    assert found["id"] == corpus_id


def test_get_by_slug_returns_none_when_missing(repo):
    assert repo.get_by_slug("does-not-exist") is None


def test_get_by_slug_excludes_soft_deleted(repo):
    corpus_id = repo.create(name="Ghost", slug="ghost", description=None, created_by="u")
    repo.soft_delete(corpus_id)
    assert repo.get_by_slug("ghost") is None


def test_list_returns_all_live_corpora(repo):
    repo.create(name="A", slug="aa", description=None, created_by="u")
    repo.create(name="B", slug="bb", description=None, created_by="u")
    rows = repo.list()
    slugs = {r["slug"] for r in rows}
    assert {"aa", "bb"} <= slugs


def test_list_excludes_soft_deleted(repo):
    id1 = repo.create(name="Live", slug="live", description=None, created_by="u")
    id2 = repo.create(name="Dead", slug="dead", description=None, created_by="u")
    repo.soft_delete(id2)
    rows = repo.list()
    ids = {r["id"] for r in rows}
    assert id1 in ids
    assert id2 not in ids


def test_soft_delete_sets_deleted_at(repo):
    corpus_id = repo.create(name="ToDelete", slug="to-delete", description=None, created_by="u")
    repo.soft_delete(corpus_id)
    # default get() hides soft-deleted
    assert repo.get(corpus_id) is None
    # include_deleted=True reveals it with deleted_at set
    row = repo.get(corpus_id, include_deleted=True)
    assert row is not None
    assert row["deleted_at"] is not None


def test_list_search_filters_by_name(repo):
    repo.create(name="Finance Data", slug="finance", description=None, created_by="u")
    repo.create(name="Marketing Stuff", slug="marketing", description=None, created_by="u")
    rows = repo.list(search="Finance")
    names = {r["name"] for r in rows}
    assert "Finance Data" in names
    assert "Marketing Stuff" not in names
