"""Cross-engine contract tests for the users repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

This follows the pattern established in test_audit_contract.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UserRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
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

    from src.repositories.users_pg import UsersPgRepository
    return UsersPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def users_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_user(repo, **kwargs):
    defaults = {"id": "user-1", "email": "u@example.com", "name": "U"}
    defaults.update(kwargs)
    repo.create(**defaults)


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------

def test_create_then_get_by_id_returns_same_row(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["id"] == "user-1"
    assert row["email"] == "u@example.com"
    assert row["name"] == "U"


def test_create_then_get_by_email_returns_same_row(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    row = repo.get_by_email("u@example.com")
    assert row is not None
    assert row["id"] == "user-1"
    assert row["email"] == "u@example.com"


def test_get_by_id_missing_returns_none(users_repo):
    repo, _, _ = users_repo
    row = repo.get_by_id("nonexistent-user")
    assert row is None


def test_update_password_hash_persists(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    repo.update("user-1", password_hash="argon2id$xxxx")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["password_hash"] == "argon2id$xxxx"


def test_deactivate_marks_active_false_and_sets_metadata(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    now = datetime.now(timezone.utc)
    repo.update("user-1", active=False, deactivated_at=now, deactivated_by="admin@example.com")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["active"] is False
    assert row["deactivated_at"] is not None
    assert row["deactivated_by"] == "admin@example.com"


def test_list_all_orders_by_email(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="b@x.com", name="B")
    _make_user(repo, id="user-b", email="a@x.com", name="A")
    rows = repo.list_all()
    emails = [r["email"] for r in rows]
    assert emails == sorted(emails)


def test_count_all_increments_on_create(users_repo):
    repo, _, _ = users_repo
    before = repo.count_all()
    _make_user(repo)
    after = repo.count_all()
    assert after == before + 1


def test_delete_removes_user(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    assert repo.get_by_id("user-1") is not None
    repo.delete("user-1")
    assert repo.get_by_id("user-1") is None


def test_set_and_get_by_slack_user_id(users_repo):
    """v71: the Slack identity binding round-trips identically on both engines
    (update(slack_user_id=...) + get_by_slack_user_id)."""
    repo, _, _ = users_repo
    _make_user(repo)

    # Unbound: no slack_user_id, lookup misses.
    row = repo.get_by_id("user-1")
    assert row.get("slack_user_id") is None
    assert repo.get_by_slack_user_id("U999") is None

    repo.update("user-1", slack_user_id="U999")
    row = repo.get_by_id("user-1")
    assert row["slack_user_id"] == "U999"

    bound = repo.get_by_slack_user_id("U999")
    assert bound is not None
    assert bound["id"] == "user-1"
