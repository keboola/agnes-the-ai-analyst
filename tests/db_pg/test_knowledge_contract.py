"""Cross-engine contract tests for the knowledge repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Covers:
- get_votes_by_user  — {item_id: vote} per-user vote map
- count_relations    — filtered COUNT over knowledge_item_relations
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
    from src.repositories.knowledge import KnowledgeRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return KnowledgeRepository(conn), conn


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

    from src.repositories.knowledge_pg import KnowledgePgRepository

    return KnowledgePgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def k_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a knowledge repo for either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


def _create_item(repo, item_id, title="Test item"):
    repo.create(
        id=item_id,
        title=title,
        content="content for " + item_id,
        category="general",
        status="approved",
    )


# ---------------------------------------------------------------------------
# get_votes_by_user contract tests
# ---------------------------------------------------------------------------


def test_get_votes_by_user_empty(k_repo):
    assert k_repo.get_votes_by_user("alice") == {}


def test_get_votes_by_user_upvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    assert k_repo.get_votes_by_user("alice") == {"item-1": 1}


def test_get_votes_by_user_downvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", -1)
    assert k_repo.get_votes_by_user("alice") == {"item-1": -1}


def test_get_votes_by_user_multiple_items(k_repo):
    _create_item(k_repo, "item-1")
    _create_item(k_repo, "item-2")
    _create_item(k_repo, "item-3")
    k_repo.vote("item-1", "alice", 1)
    k_repo.vote("item-2", "alice", -1)
    # item-3 not voted — must not appear
    k_repo.vote("item-1", "bob", 1)  # other user — must not appear for alice

    result = k_repo.get_votes_by_user("alice")
    assert result == {"item-1": 1, "item-2": -1}


def test_get_votes_by_user_vote_override(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    k_repo.vote("item-1", "alice", -1)  # override
    assert k_repo.get_votes_by_user("alice") == {"item-1": -1}


def test_get_votes_by_user_after_unvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    k_repo.unvote("item-1", "alice")
    assert k_repo.get_votes_by_user("alice") == {}


# ---------------------------------------------------------------------------
# count_relations contract tests
# ---------------------------------------------------------------------------


def test_count_relations_empty(k_repo):
    assert k_repo.count_relations() == 0


def test_count_relations_total(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "duplicate")
    assert k_repo.count_relations() == 2


def test_count_relations_filtered_by_type(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "related")
    assert k_repo.count_relations(relation_type="duplicate") == 1
    assert k_repo.count_relations(relation_type="related") == 1
    assert k_repo.count_relations(relation_type="nonexistent") == 0


def test_count_relations_filtered_by_resolved(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "duplicate")
    k_repo.resolve_relation("item-a", "item-b", "duplicate", "admin", "merged")

    assert k_repo.count_relations(resolved=False) == 1
    assert k_repo.count_relations(resolved=True) == 1
    assert k_repo.count_relations() == 2


def test_count_relations_type_and_resolved_combined(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "related")
    k_repo.resolve_relation("item-a", "item-b", "duplicate", "admin", "merged")

    assert k_repo.count_relations(relation_type="duplicate", resolved=True) == 1
    assert k_repo.count_relations(relation_type="duplicate", resolved=False) == 0
    assert k_repo.count_relations(relation_type="related", resolved=False) == 1
