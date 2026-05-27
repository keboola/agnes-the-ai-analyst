"""Cross-engine contract tests for the data_packages repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

Follows the pattern established in ``test_users_contract.py`` /
``test_audit_contract.py``. Both backends are seeded with the same
``table_registry`` rows so the ``list_tables`` JOIN has real targets.
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

def _seed_table_registry_duckdb(conn) -> None:
    for tid, name in (("t1", "orders"), ("t2", "customers"), ("t3", "events")):
        conn.execute(
            "INSERT INTO table_registry (id, name) VALUES (?, ?)",
            [tid, name],
        )


def _seed_table_registry_pg(engine) -> None:
    with engine.begin() as conn:
        for tid, name in (("t1", "orders"), ("t2", "customers"), ("t3", "events")):
            conn.execute(
                sa.text("INSERT INTO table_registry (id, name) VALUES (:id, :name)"),
                {"id": tid, "name": name},
            )


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.data_packages import DataPackagesRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    _seed_table_registry_duckdb(conn)
    return DataPackagesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    _seed_table_registry_pg(pg_engine)

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.data_packages_pg import DataPackagesPgRepository
    return DataPackagesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a data_packages repo bound to either DuckDB or PG."""
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

def test_create_then_get_returns_same_shape(repo):
    pkg_id = repo.create(
        name="X", slug="x", description="d",
        icon=None, color=None, created_by="u",
    )
    row = repo.get(pkg_id)
    assert row is not None
    assert row["id"] == pkg_id
    assert row["slug"] == "x"
    assert row["name"] == "X"
    assert row["description"] == "d"
    assert row["created_by"] == "u"


def test_get_by_slug_resolves_and_returns_none_when_missing(repo):
    pkg_id = repo.create(
        name="A", slug="a", description=None,
        icon=None, color=None, created_by="u",
    )
    found = repo.get_by_slug("a")
    assert found is not None
    assert found["id"] == pkg_id
    assert repo.get_by_slug("missing") is None


def test_get_by_slug_filters_soft_deleted(repo):
    """Both engines must hide soft-deleted rows from get_by_slug."""
    pkg_id = repo.create(
        name="X", slug="ghost", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.delete(pkg_id)
    assert repo.get_by_slug("ghost") is None


def test_delete_filters_out_of_default_list(repo):
    pkg_id = repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.delete(pkg_id)
    assert all(r["id"] != pkg_id for r in repo.list())


def test_add_table_is_idempotent(repo):
    pkg_id = repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
    )
    assert repo.add_table(pkg_id, "t1", added_by="u") is True
    assert repo.add_table(pkg_id, "t1", added_by="u") is False


def test_list_member_ids_bulk_returns_per_package_lists(repo):
    a = repo.create(
        name="A", slug="a", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.add_table(a, "t1", added_by="u")
    bulk = repo.list_member_ids_bulk()
    assert bulk[a] == ["t1"]


def test_update_tags_jsonb_round_trip(repo):
    pkg_id = repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
        tags=["a", "b"],
    )
    repo.update(pkg_id, tags=["c"])
    row = repo.get(pkg_id)
    assert row["tags"] == ["c"]
