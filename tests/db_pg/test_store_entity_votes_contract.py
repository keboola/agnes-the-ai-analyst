"""Cross-engine contract tests for the ``store_entity_votes`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both;
the same return shapes must come back. Any divergence is a bug in whichever
side is wrong.

Models the per-user one-vote-per-entity pattern after the ``knowledge_votes``
repo (see ``tests/db_pg/test_mcp_sources_contract.py`` for the fixture shape).
Closes #398.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so the
    # session timezone is pinned to UTC — keeps the
    # `test_no_bare_duckdb_connect_in_production_code` regression guard green.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.store_entity_votes import StoreEntityVotesRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return StoreEntityVotesRepository(conn), conn


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

    from src.repositories.store_entity_votes_pg import StoreEntityVotesPgRepository
    return StoreEntityVotesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a ``store_entity_votes`` repo bound to either DuckDB or PG."""
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

def test_empty_aggregate_is_zero(repo):
    agg = repo.get_aggregate("e1")
    assert agg == {"up": 0, "down": 0, "my_vote": 0}


def test_vote_up_then_aggregate(repo):
    repo.vote("e1", "u1", 1)
    agg = repo.get_aggregate("e1", user_id="u1")
    assert agg["up"] == 1
    assert agg["down"] == 0
    assert agg["my_vote"] == 1


def test_same_user_up_then_down_keeps_one_row_and_flips(repo):
    """POST rate up then down by same user => one row, my_vote flips."""
    repo.vote("e1", "u1", 1)
    repo.vote("e1", "u1", -1)
    agg = repo.get_aggregate("e1", user_id="u1")
    # One row only — up flipped to down.
    assert agg["up"] == 0
    assert agg["down"] == 1
    assert agg["my_vote"] == -1


def test_aggregate_counts_across_users(repo):
    repo.vote("e1", "u1", 1)
    repo.vote("e1", "u2", 1)
    repo.vote("e1", "u3", -1)
    agg = repo.get_aggregate("e1", user_id="u2")
    assert agg["up"] == 2
    assert agg["down"] == 1
    assert agg["my_vote"] == 1


def test_my_vote_zero_when_user_has_not_voted(repo):
    repo.vote("e1", "u1", 1)
    agg = repo.get_aggregate("e1", user_id="u2")
    assert agg["up"] == 1
    assert agg["my_vote"] == 0


def test_my_vote_omitted_user_is_zero(repo):
    repo.vote("e1", "u1", 1)
    agg = repo.get_aggregate("e1")
    assert agg["up"] == 1
    assert agg["my_vote"] == 0


def test_unvote_removes_the_row(repo):
    """Clear (0) path: unvote removes the row entirely."""
    repo.vote("e1", "u1", 1)
    repo.unvote("e1", "u1")
    agg = repo.get_aggregate("e1", user_id="u1")
    assert agg["up"] == 0
    assert agg["down"] == 0
    assert agg["my_vote"] == 0


def test_unvote_missing_row_is_idempotent(repo):
    # No raise when clearing a vote that was never cast.
    repo.unvote("e1", "never-voted")
    agg = repo.get_aggregate("e1")
    assert agg == {"up": 0, "down": 0, "my_vote": 0}


def test_votes_are_scoped_per_entity(repo):
    repo.vote("e1", "u1", 1)
    repo.vote("e2", "u1", -1)
    assert repo.get_aggregate("e1", user_id="u1") == {"up": 1, "down": 0, "my_vote": 1}
    assert repo.get_aggregate("e2", user_id="u1") == {"up": 0, "down": 1, "my_vote": -1}
