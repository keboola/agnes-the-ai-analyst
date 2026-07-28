"""Cross-engine contract tests for the memory_mining_consent repository."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.memory_mining_consent import MemoryMiningConsentRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MemoryMiningConsentRepository(conn), conn


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

    from src.repositories.memory_mining_consent_pg import (
        MemoryMiningConsentPgRepository,
    )

    return MemoryMiningConsentPgRepository(db_pg.get_engine()), None


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


def test_unknown_user_is_not_opted_in(repo):
    assert repo.is_opted_in("nobody@x") is False
    assert repo.get("nobody@x") is None
    assert repo.list_opted_in() == []


def test_opt_in_then_out_roundtrip(repo):
    repo.set_consent("a@x", opted_in=True)
    assert repo.is_opted_in("a@x") is True
    assert repo.get("a@x")["opted_in"] is True
    assert "a@x" in repo.list_opted_in()

    repo.set_consent("a@x", opted_in=False)
    assert repo.is_opted_in("a@x") is False
    assert "a@x" not in repo.list_opted_in()

    # opting back in flips it again
    repo.set_consent("a@x", opted_in=True)
    assert repo.is_opted_in("a@x") is True


def test_list_opted_in_only_returns_opted_in(repo):
    repo.set_consent("in@x", opted_in=True)
    repo.set_consent("out@x", opted_in=False)
    assert repo.list_opted_in() == ["in@x"]
