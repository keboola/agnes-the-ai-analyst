"""Cross-engine contract tests for the user_journey repository.

Parametrises over [DuckDB impl, Postgres impl]. Same calls, same shapes
back. Follows the pattern established in
``tests/db_pg/test_user_stack_subscriptions_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.user_journey import UserJourneyRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UserJourneyRepository(conn), conn


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

    from src.repositories.user_journey_pg import UserJourneyPgRepository

    return UserJourneyPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


def test_get_defaults_for_unknown_user(repo):
    state = repo.get("nobody")
    assert state == {
        "first_asked": False,
        "stack_setup_done": False,
        "explored_stack": False,
        "catalog_discovered": False,
        "use_anywhere": False,
        "onboarded": False,
        "successful_answers": 0,
    }


def test_update_partial_upsert(repo):
    result = repo.update("user_a", first_asked=True)
    assert result["first_asked"] is True
    assert result["onboarded"] is False
    assert result["successful_answers"] == 0

    # Second partial update preserves the previously-set field.
    result2 = repo.update("user_a", onboarded=True, successful_answers=3)
    assert result2["first_asked"] is True
    assert result2["onboarded"] is True
    assert result2["successful_answers"] == 3

    fetched = repo.get("user_a")
    assert fetched == result2


def test_update_rejects_unknown_field(repo):
    with pytest.raises(ValueError):
        repo.update("user_a", not_a_real_field=True)


def test_update_does_not_bleed_across_users(repo):
    repo.update("user_a", onboarded=True)
    repo.update("user_b", onboarded=False)
    assert repo.get("user_a")["onboarded"] is True
    assert repo.get("user_b")["onboarded"] is False


def test_reset(repo):
    repo.update("user_a", onboarded=True, successful_answers=5)
    repo.reset("user_a")
    assert repo.get("user_a") == {
        "first_asked": False,
        "stack_setup_done": False,
        "explored_stack": False,
        "catalog_discovered": False,
        "use_anywhere": False,
        "onboarded": False,
        "successful_answers": 0,
    }


def test_reset_unknown_user_is_noop(repo):
    repo.reset("nobody")  # must not raise
