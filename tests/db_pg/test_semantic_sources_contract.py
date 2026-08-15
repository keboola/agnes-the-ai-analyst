"""Cross-engine contract tests for the semantic_sources repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.semantic_sources import SemanticSourcesRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SemanticSourcesRepository(conn), conn


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

    from src.repositories.semantic_sources_pg import SemanticSourcesPgRepository

    return SemanticSourcesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_and_get(repo):
    repo.create(
        id="s1",
        kind="git",
        name="Finance models",
        adapter="native",
        config={"repo_url": "https://example.com/x.git", "ref": "main", "glob": "semantic/**/*.yaml"},
    )
    row = repo.get("s1")
    assert row["kind"] == "git"
    assert row["config"]["glob"] == "semantic/**/*.yaml"
    assert row["enabled"] is True


def test_list_enabled_only(repo):
    repo.create(id="s1", kind="git", name="on", adapter="native", config={})
    repo.create(id="s2", kind="git", name="off", adapter="native", config={}, enabled=False)
    assert {r["id"] for r in repo.list_all(enabled_only=True)} == {"s1"}
    assert len(repo.list_all()) == 2


def test_record_sync_stores_outcome(repo):
    repo.create(id="s1", kind="git", name="x", adapter="native", config={})
    repo.record_sync("s1", status="error", error="clone failed: auth")
    row = repo.get("s1")
    assert row["last_sync_status"] == "error"
    assert "auth" in row["last_sync_error"]
    assert row["last_sync_at"] is not None


def test_record_sync_clears_previous_error_on_success(repo):
    repo.create(id="s1", kind="git", name="x", adapter="native", config={})
    repo.record_sync("s1", status="error", error="boom")
    repo.record_sync("s1", status="ok", error=None)
    assert repo.get("s1")["last_sync_error"] is None
