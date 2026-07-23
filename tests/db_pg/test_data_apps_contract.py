"""Cross-engine contract tests for the data_apps repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_memory_domains_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.data_apps import DataAppsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return DataAppsRepository(conn), conn


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

    from src.repositories.data_apps_pg import DataAppsPgRepository

    return DataAppsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a data_apps repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


@pytest.fixture
def backend(request):
    return request.node.callspec.params["repo"]


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_create_then_get_consistent(repo):
    aid = repo.create(slug="sales-dash", name="Sales dashboard", owner_user_id="u1")
    row = repo.get(aid)
    assert row is not None
    assert aid.startswith("app_")
    assert row["id"] == aid
    assert row["slug"] == "sales-dash"
    assert row["name"] == "Sales dashboard"
    assert row["owner_user_id"] == "u1"
    assert row["state"] == "created"
    assert row["repo_mode"] == "internal"
    assert row["sleep_mode"] == "recreate"
    assert row["idle_timeout_s"] == 1800


def test_get_by_slug_consistent(repo):
    aid = repo.create(slug="x", name="X", owner_user_id="u1")
    found = repo.get_by_slug("x")
    assert found is not None
    assert found["id"] == aid
    assert repo.get_by_slug("nope") is None


def test_slug_unique_raises(repo):
    repo.create(slug="dup", name="A", owner_user_id="u1")
    with pytest.raises((duckdb.ConstraintException, sa.exc.IntegrityError)):
        repo.create(slug="dup", name="B", owner_user_id="u2")


def test_list_filters_by_owner_and_state(repo):
    a = repo.create(slug="a", name="A", owner_user_id="u1")
    repo.create(slug="b", name="B", owner_user_id="u2")
    repo.set_state(a, "running")

    by_owner = repo.list(owner_user_id="u1")
    assert {r["id"] for r in by_owner} == {a}

    by_state = repo.list(state="running")
    assert {r["id"] for r in by_state} == {a}

    assert len(repo.list(limit=1000)) == 2


def test_set_state_and_record_deploy(repo):
    aid = repo.create(slug="s", name="S", owner_user_id="u1")
    repo.set_state(aid, "deploying", detail="building image")
    row = repo.get(aid)
    assert row["state"] == "deploying"
    assert row["state_detail"] == "building image"

    repo.record_deploy(aid, "abc123")
    row = repo.get(aid)
    assert row["deployed_sha"] == "abc123"
    assert row["last_deploy_at"] is not None


def test_touch_last_request(repo):
    aid = repo.create(slug="t", name="T", owner_user_id="u1")
    assert repo.get(aid)["last_request_at"] is None
    repo.touch_last_request(aid)
    assert repo.get(aid)["last_request_at"] is not None


def test_list_idle_consistent(repo, backend):
    aid = repo.create(slug="i", name="I", owner_user_id="u1")
    repo.set_state(aid, "running")

    if backend == "duckdb":
        repo.conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [aid],
        )
    else:
        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_apps SET last_request_at = now() - INTERVAL '2 hours' WHERE id = :id"),
                {"id": aid},
            )

    assert [r["id"] for r in repo.list_idle(older_than_s=3600)] == [aid]
    assert repo.list_idle(older_than_s=3600 * 3) == []


def test_update_whitelist(repo):
    aid = repo.create(slug="w", name="W", owner_user_id="u1")
    assert repo.update(aid, mem_limit="2g", service_token_id="t1") is True
    row = repo.get(aid)
    assert row["mem_limit"] == "2g"
    assert row["service_token_id"] == "t1"

    with pytest.raises(ValueError):
        repo.update(aid, state="running")


def test_delete_round_trip(repo):
    aid = repo.create(slug="ghost", name="Ghost", owner_user_id="u1")
    assert repo.delete(aid) is True
    assert repo.get(aid) is None
    assert repo.delete(aid) is False
