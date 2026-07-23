"""Cross-engine contract tests for the ``idempotency_keys`` repository.

Parametrises over [DuckDB impl, Postgres impl] — same calls, same answers.
Follows the pattern established in ``test_agents_contract.py`` /
``test_jobs_contract.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.idempotency import IdempotencyRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return IdempotencyRepository(conn), conn


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

    from src.repositories.idempotency_pg import IdempotencyPgRepository

    return IdempotencyPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_get_miss_returns_none(repo):
    assert repo.get("nope", "owner-1", "agent-1") is None


def test_put_then_get_roundtrip(repo):
    repo.put("key-1", "owner-1", "agent-1", "hash-abc", '{"answer": "hi"}', 200, ttl_s=3600)
    row = repo.get("key-1", "owner-1", "agent-1")
    assert row is not None
    assert row["request_hash"] == "hash-abc"
    assert row["response_body"] == '{"answer": "hi"}'
    assert row["status_code"] == 200


def test_scoped_by_owner_and_agent(repo):
    """Same key string under a DIFFERENT owner or agent is a distinct row —
    no cross-tenant / cross-agent collision."""
    repo.put("shared-key", "owner-1", "agent-1", "hash-a", "body-a", 200, ttl_s=3600)
    repo.put("shared-key", "owner-2", "agent-1", "hash-b", "body-b", 200, ttl_s=3600)
    repo.put("shared-key", "owner-1", "agent-2", "hash-c", "body-c", 200, ttl_s=3600)

    assert repo.get("shared-key", "owner-1", "agent-1")["response_body"] == "body-a"
    assert repo.get("shared-key", "owner-2", "agent-1")["response_body"] == "body-b"
    assert repo.get("shared-key", "owner-1", "agent-2")["response_body"] == "body-c"


def test_hash_mismatch_is_visible_to_caller(repo):
    """``get`` doesn't itself enforce the hash match — it just returns the
    stored row so the caller (the API handler) can compare ``request_hash``
    against the incoming request's own hash and decide 200-replay vs.
    409-reuse."""
    repo.put("key-2", "owner-1", "agent-1", "hash-original", "body", 200, ttl_s=3600)
    row = repo.get("key-2", "owner-1", "agent-1")
    assert row["request_hash"] == "hash-original"
    assert row["request_hash"] != "hash-different"


def test_put_overwrites_existing_row_for_same_triple(repo):
    repo.put("key-3", "owner-1", "agent-1", "hash-1", "body-1", 200, ttl_s=3600)
    repo.put("key-3", "owner-1", "agent-1", "hash-2", "body-2", 201, ttl_s=3600)
    row = repo.get("key-3", "owner-1", "agent-1")
    assert row["request_hash"] == "hash-2"
    assert row["response_body"] == "body-2"
    assert row["status_code"] == 201


def test_get_expired_row_returns_none(repo):
    repo.put("key-4", "owner-1", "agent-1", "hash", "body", 200, ttl_s=0)
    # ttl_s=0 -> expires_at is "now" at insert time; a moment later it's past.
    time.sleep(0.01)
    assert repo.get("key-4", "owner-1", "agent-1") is None


def test_purge_expired_removes_only_expired_rows(repo):
    repo.put("expired-1", "owner-1", "agent-1", "hash", "body", 200, ttl_s=0)
    repo.put("expired-2", "owner-1", "agent-1", "hash", "body", 200, ttl_s=0)
    repo.put("fresh-1", "owner-1", "agent-1", "hash", "body", 200, ttl_s=3600)
    time.sleep(0.01)

    removed = repo.purge_expired()
    assert removed == 2
    assert repo.get("fresh-1", "owner-1", "agent-1") is not None


def test_purge_expired_noop_when_nothing_expired(repo):
    repo.put("fresh-2", "owner-1", "agent-1", "hash", "body", 200, ttl_s=3600)
    assert repo.purge_expired() == 0
