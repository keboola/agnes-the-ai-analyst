"""Cross-engine contract tests for the memory_domain_suggestions repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_memory_domains_contract.py``
(Task 1D.2). No ``knowledge_items`` seeding here — suggestions are
standalone (no bridge table), audit-only stamping of
``created_domain_id`` is a plain scalar with no FK.
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
    from src.repositories.memory_domain_suggestions import (
        MemoryDomainSuggestionsRepository,
    )

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MemoryDomainSuggestionsRepository(conn), conn


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

    from src.repositories.memory_domain_suggestions_pg import (
        MemoryDomainSuggestionsPgRepository,
    )
    return MemoryDomainSuggestionsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a memory_domain_suggestions repo bound to either DuckDB or PG."""
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

def test_create_then_get(repo):
    sid = repo.create(
        name="Finance",
        description="Finance domain proposal",
        rationale="We have lots of finance facts",
        created_by="alice@example.com",
    )
    row = repo.get(sid)
    assert row is not None
    assert sid.startswith("sug_")
    assert row["id"] == sid
    assert row["name"] == "Finance"
    assert row["status"] == "pending"
    assert row["created_by"] == "alice@example.com"
    assert row["resolved_at"] is None
    assert row["created_domain_id"] is None


def test_count_pending(repo):
    repo.create(name="A", description=None, rationale=None, created_by="u")
    repo.create(name="B", description=None, rationale=None, created_by="u")
    assert repo.count_pending() == 2


def test_resolve_to_approved(repo):
    sid = repo.create(
        name="X", description=None, rationale=None, created_by="u",
    )
    ok = repo.resolve(
        sid,
        status="approved",
        resolved_by="admin@example.com",
        created_domain_id="md_abc",
    )
    assert ok is True
    row = repo.get(sid)
    assert row["status"] == "approved"
    assert row["created_domain_id"] == "md_abc"
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "admin@example.com"


def test_list_filtered_by_status(repo):
    a = repo.create(name="A", description=None, rationale=None, created_by="u")
    b = repo.create(name="B", description=None, rationale=None, created_by="u")
    repo.resolve(
        a,
        status="approved",
        resolved_by="admin@example.com",
        created_domain_id="md_x",
    )
    pending = repo.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == b
