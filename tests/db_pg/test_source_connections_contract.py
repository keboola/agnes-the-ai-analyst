"""Cross-engine contract tests for the source_connections repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.source_connections import SourceConnectionsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SourceConnectionsRepository(conn), conn


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

    from src.repositories.source_connections_pg import SourceConnectionsPgRepository

    return SourceConnectionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_get_roundtrip(repo):
    repo.create(
        id="c1",
        name="kbc_eu",
        source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
        token_env="KBC_EU_TOKEN",
        is_default=True,
        created_by="t@example.com",
    )
    row = repo.get("c1")
    assert row["name"] == "kbc_eu"
    assert row["config"]["stack_url"] == "https://connection.example.com"
    assert repo.get_by_name("kbc_eu")["id"] == "c1"
    assert repo.get("nope") is None


def test_list_filters_by_source_type(repo):
    repo.create(id="c1", name="kbc", source_type="keboola", config={"stack_url": "https://a"})
    repo.create(id="c2", name="bq", source_type="bigquery", config={"project": "p"})
    assert {r["id"] for r in repo.list()} == {"c1", "c2"}
    assert [r["id"] for r in repo.list(source_type="keboola")] == ["c1"]


def test_default_is_unique_per_source_type(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"}, is_default=True)
    repo.create(id="c2", name="b", source_type="keboola", config={"stack_url": "https://b"}, is_default=True)
    rows = repo.list(source_type="keboola")
    defaults = [r for r in rows if r["is_default"]]
    assert [r["id"] for r in defaults] == ["c2"]  # last set wins
    assert repo.get_default("keboola")["id"] == "c2"
    assert repo.get_default("bigquery") is None


def test_update_and_delete(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"})
    repo.update("c1", config={"stack_url": "https://b"}, token_env="X")
    assert repo.get("c1")["config"]["stack_url"] == "https://b"
    assert repo.get("c1")["token_env"] == "X"
    repo.delete("c1")
    assert repo.get("c1") is None


def test_update_renames_connection(repo):
    # Backs the "Add data source" wizard's rename-after-test-connection step
    # (#755) — the project name returned by test-connection is only known
    # once the row already exists.
    repo.create(id="c1", name="draft", source_type="keboola", config={"stack_url": "https://a"})
    repo.update("c1", name="Production")
    row = repo.get("c1")
    assert row["name"] == "Production"
    assert row["config"]["stack_url"] == "https://a"  # untouched
    assert repo.get_by_name("Production")["id"] == "c1"
    assert repo.get_by_name("draft") is None


def test_update_promotes_default_and_demotes_siblings(repo):
    repo.create(id="c1", name="a", source_type="keboola", config={"stack_url": "https://a"}, is_default=True)
    repo.create(id="c2", name="b", source_type="keboola", config={"stack_url": "https://b"})
    # Promoting c2 must demote the previous default c1 (unique per source_type).
    repo.update("c2", is_default=True)
    assert repo.get("c2")["is_default"]
    assert not repo.get("c1")["is_default"]
    assert repo.get_default("keboola")["id"] == "c2"
    # Demoting c2 leaves no default for the type.
    repo.update("c2", is_default=False)
    assert not repo.get("c2")["is_default"]
    assert repo.get_default("keboola") is None
