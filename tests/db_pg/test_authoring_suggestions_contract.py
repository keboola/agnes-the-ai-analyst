"""Cross-engine contract tests for the authoring_suggestions repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both; the
same return shapes must come back. DuckDB is the contract authority. Mirrors
``test_memory_domain_suggestions_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.authoring_suggestions import AuthoringSuggestionsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AuthoringSuggestionsRepository(conn), conn


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

    from src.repositories.authoring_suggestions_pg import (
        AuthoringSuggestionsPgRepository,
    )

    return AuthoringSuggestionsPgRepository(db_pg.get_engine()), None


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


def test_create_get_roundtrip_preserves_payload(repo):
    sid = repo.create(
        domain="data-package",
        payload={"name": "Fin", "slug": "fin", "description": "x"},
        created_by="u@x",
    )
    assert sid.startswith("asug_")
    row = repo.get(sid)
    assert row["domain"] == "data-package"
    assert row["status"] == "pending"
    assert row["created_by"] == "u@x"
    # payload round-trips as a dict on both engines
    assert row["payload"] == {"name": "Fin", "slug": "fin", "description": "x"}


def test_list_filters_by_status_and_domain(repo):
    repo.create(domain="mcp", payload={"name": "a"}, created_by="u@x")
    s2 = repo.create(domain="data-package", payload={"name": "b"}, created_by="u@x")
    repo.resolve(s2, status="approved", resolved_by="admin", created_resource_id="dp_1")

    pending = repo.list(status="pending")
    assert all(r["status"] == "pending" for r in pending)
    assert {r["domain"] for r in pending} == {"mcp"}

    dp = repo.list(domain="data-package")
    assert {r["id"] for r in dp} == {s2}


def test_resolve_is_guarded_and_stamps(repo):
    sid = repo.create(domain="marketplace", payload={"name": "m"}, created_by="u@x")
    assert repo.count_pending() == 1

    first = repo.resolve(
        sid,
        status="approved",
        resolved_by="admin",
        resolution_note="ok",
        created_resource_id="mp_1",
    )
    assert first is True
    row = repo.get(sid)
    assert row["status"] == "approved"
    assert row["resolved_by"] == "admin"
    assert row["created_resource_id"] == "mp_1"
    assert row["resolved_at"] is not None
    assert repo.count_pending() == 0

    # re-resolving an already-resolved row is a no-op guard miss
    second = repo.resolve(sid, status="rejected", resolved_by="admin2")
    assert second is False
    assert repo.get(sid)["status"] == "approved"


def test_resolve_rejects_invalid_status(repo):
    sid = repo.create(domain="corporate-memory", payload={"name": "c"}, created_by="u@x")
    with pytest.raises(ValueError):
        repo.resolve(sid, status="bogus", resolved_by="admin")


def test_reopen_reverts_claim_and_set_resource_id_stamps(repo):
    """reopen() rolls a claimed suggestion back to pending (used when an
    approve's replay fails); set_created_resource_id() stamps after success.
    Both must behave identically on DuckDB and Postgres."""
    sid = repo.create(domain="data-package", payload={"name": "R", "slug": "r"}, created_by="u@x")

    # claim, then roll back
    assert repo.resolve(sid, status="approved", resolved_by="admin", resolution_note="n") is True
    repo.reopen(sid)
    row = repo.get(sid)
    assert row["status"] == "pending"
    assert row["resolved_by"] is None
    assert row["resolved_at"] is None
    assert row["created_resource_id"] is None

    # re-claim (now pending again), then stamp the created resource id
    assert repo.resolve(sid, status="approved", resolved_by="admin") is True
    repo.set_created_resource_id(sid, "pkg_xyz")
    assert repo.get(sid)["created_resource_id"] == "pkg_xyz"
