"""Cross-engine contract test for the observability_views repository.

Pins ``count_for_user`` + ``name_exists`` on both backends — the two methods
that back the per-user saved-view cap in ``app/api/observability.py`` (the
create-view handler). That cap previously ran raw ``conn.execute`` on the
DuckDB-typed connection, so on a Postgres-backed deployment it read an empty
DuckDB table (count always 0 → never capped) while ``create()`` wrote to PG.
Routing through the factory + this contract make that drift impossible.
"""

from __future__ import annotations

import pytest


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (not bare `duckdb.connect`) so the session
    # timezone is pinned to UTC — `tests/test_duckdb_session_tz.py` guards it.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.observability_views import ObservabilityViewsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {"views": ObservabilityViewsRepository(conn), "conn": conn, "backend": "duckdb"}


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.observability_views_pg import ObservabilityViewsPgRepository

    eng = db_pg.get_engine()
    return {"views": ObservabilityViewsPgRepository(eng), "engine": eng, "backend": "pg"}


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        bundle = _make_duckdb_repo(tmp_path)
        yield bundle
        bundle["conn"].close()
    else:
        bundle = _make_pg_repo(pg_engine, monkeypatch)
        yield bundle


class TestCountForUser:
    def test_empty_is_zero(self, repos):
        assert repos["views"].count_for_user("u1") == 0

    def test_counts_only_that_user(self, repos):
        v = repos["views"]
        v.create("u1", "v-a", {"window": "7d"})
        v.create("u1", "v-b", {"window": "30d"})
        v.create("u2", "v-c", {"window": "1d"})
        assert v.count_for_user("u1") == 2
        assert v.count_for_user("u2") == 1

    def test_upsert_same_name_does_not_double_count(self, repos):
        v = repos["views"]
        v.create("u1", "v-a", {"window": "7d"})
        v.create("u1", "v-a", {"window": "30d"})  # ON CONFLICT update, not insert
        assert v.count_for_user("u1") == 1


class TestNameExists:
    def test_false_when_absent(self, repos):
        assert repos["views"].name_exists("u1", "nope") is False

    def test_true_after_create(self, repos):
        v = repos["views"]
        v.create("u1", "v-a", {"window": "7d"})
        assert v.name_exists("u1", "v-a") is True
        # scoped to the user — same name under a different user is independent
        assert v.name_exists("u2", "v-a") is False
