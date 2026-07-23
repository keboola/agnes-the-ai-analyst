"""Cross-engine contract tests for the agent_artifacts repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agent_artifacts import AgentArtifactsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentArtifactsRepository(conn), conn


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

    from src.repositories.agent_artifacts_pg import AgentArtifactsPgRepository

    return AgentArtifactsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_and_list_for_session(repo):
    repo.create(
        id="ar1",
        session_id="c1",
        agent_id="a1",
        owner_user_id="u1",
        filename="report.csv",
        object_key="artifacts/c1/report.csv",
        size_bytes=1234,
        content_type="text/csv",
        md5="abc",
    )
    rows = repo.list_for_session("c1")
    assert len(rows) == 1 and rows[0]["filename"] == "report.csv"
    assert repo.get("ar1")["object_key"] == "artifacts/c1/report.csv"


def test_get_missing_returns_none(repo):
    assert repo.get("missing") is None


def test_list_for_session_isolated_by_session(repo):
    repo.create(
        id="ar1",
        session_id="c1",
        agent_id="a1",
        owner_user_id="u1",
        filename="a.csv",
        object_key="artifacts/c1/a.csv",
        size_bytes=1,
        content_type="text/csv",
        md5="a",
    )
    repo.create(
        id="ar2",
        session_id="c2",
        agent_id="a1",
        owner_user_id="u1",
        filename="b.csv",
        object_key="artifacts/c2/b.csv",
        size_bytes=2,
        content_type="text/csv",
        md5="b",
    )
    rows = repo.list_for_session("c1")
    assert len(rows) == 1 and rows[0]["id"] == "ar1"


def test_create_allows_null_agent_id(repo):
    repo.create(
        id="ar1",
        session_id="c1",
        agent_id=None,
        owner_user_id="u1",
        filename="report.csv",
        object_key="artifacts/c1/report.csv",
        size_bytes=1234,
        content_type="text/csv",
        md5="abc",
    )
    row = repo.get("ar1")
    assert row["agent_id"] is None
