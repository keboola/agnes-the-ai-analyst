"""Cross-engine contract tests for the ``ticket`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Backs the chat sandbox secret
broker (2026-07-14 incident hardening): opaque, short-lived tickets that
let a sandboxed chat agent authenticate to the broker without ever holding
a real credential.

Follows the pattern established in ``test_mcp_sources_contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so
    # the session timezone is pinned to UTC — keeps `tests/db_pg/`'s
    # `test_no_bare_duckdb_connect_in_production_code` regression guard
    # green on new files.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.ticket import TicketRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return TicketRepository(conn), conn


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

    from src.repositories.ticket_pg import TicketPgRepository

    return TicketPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a ``ticket`` repo bound to either DuckDB or PG."""
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


def test_mint_returns_opaque_token(repo):
    tok = repo.mint("chat_1", "main", ttl_seconds=3600)
    assert isinstance(tok, str)
    assert "." not in tok  # opaque — not a JWT-shaped value
    assert len(tok) >= 32


def test_mint_resolve_revoke(repo):
    tok = repo.mint("chat_1", "main", ttl_seconds=3600)
    got = repo.resolve(tok)
    assert got is not None
    assert got["session_id"] == "chat_1"
    assert got["scope"] == "main"
    assert got["expires_at"] is not None
    repo.revoke(tok)
    assert repo.resolve(tok) is None


def test_resolve_unknown_returns_none(repo):
    assert repo.resolve("nope") is None


def test_resolve_expired_returns_none(repo):
    tok = repo.mint("chat_2", "mcp", ttl_seconds=-1)  # already expired
    assert repo.resolve(tok) is None


def test_revoke_unknown_token_is_idempotent(repo):
    repo.revoke("never-existed")  # must not raise


def test_revoke_session_invalidates_all_tickets_for_session(repo):
    t1 = repo.mint("chat_3", "main")
    t2 = repo.mint("chat_3", "mcp")
    other = repo.mint("chat_4", "main")
    repo.revoke_session("chat_3")
    assert repo.resolve(t1) is None
    assert repo.resolve(t2) is None
    # a different session's ticket is untouched
    assert repo.resolve(other) is not None


def test_revoke_session_unknown_session_is_idempotent(repo):
    repo.revoke_session("never-existed")  # must not raise
