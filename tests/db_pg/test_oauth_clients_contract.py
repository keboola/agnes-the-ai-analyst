"""Cross-engine contract tests for the ``oauth_clients`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.oauth_clients import OAuthClientsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return OAuthClientsRepository(conn), conn


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

    from src.repositories.oauth_clients_pg import OAuthClientsPgRepository

    return OAuthClientsPgRepository(db_pg.get_engine()), None


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


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_upsert_client_and_get(repo):
    repo.upsert_client(
        client_id="client-1",
        client_secret="s3cr3t",
        redirect_uris=["https://example.com/cb"],
        client_name="Test App",
        client_metadata={"grant_types": ["authorization_code"]},
    )
    row = repo.get_client("client-1")
    assert row is not None
    assert row["client_id"] == "client-1"
    assert row["client_secret"] == "s3cr3t"
    assert row["redirect_uris"] == ["https://example.com/cb"]
    assert row["client_name"] == "Test App"
    assert isinstance(row["client_metadata"], dict)


def test_get_client_missing_returns_none(repo):
    assert repo.get_client("no-such-client") is None


def test_upsert_client_is_idempotent(repo):
    repo.upsert_client(
        client_id="c1",
        redirect_uris=["https://a.example/cb"],
    )
    repo.upsert_client(
        client_id="c1",
        redirect_uris=["https://b.example/cb"],
        client_name="Updated",
    )
    row = repo.get_client("c1")
    assert row is not None
    assert row["redirect_uris"] == ["https://b.example/cb"]
    assert row["client_name"] == "Updated"


def test_save_and_get_auth_code(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    expires_at = time.time() + 600
    repo.save_auth_code(
        code="auth-code-abc",
        client_id="c1",
        scopes=["read"],
        code_challenge="challenge123",
        redirect_uri="https://x.example/cb",
        redirect_uri_provided_explicitly=True,
        expires_at=expires_at,
        subject="user-id-1",
    )
    row = repo.get_auth_code("auth-code-abc")
    assert row is not None
    assert row["code"] == "auth-code-abc"
    assert row["client_id"] == "c1"
    assert row["scopes"] == ["read"]
    assert row["code_challenge"] == "challenge123"
    assert row["subject"] == "user-id-1"
    assert row["redirect_uri_provided_explicitly"] is True


def test_get_auth_code_missing_returns_none(repo):
    assert repo.get_auth_code("nope") is None


def test_delete_auth_code(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_auth_code(
        code="del-me",
        client_id="c1",
        scopes=[],
        code_challenge="ch",
        redirect_uri="https://x.example/cb",
        redirect_uri_provided_explicitly=False,
        expires_at=time.time() + 300,
        subject="u1",
    )
    assert repo.get_auth_code("del-me") is not None
    repo.delete_auth_code("del-me")
    assert repo.get_auth_code("del-me") is None


def test_delete_auth_code_idempotent(repo):
    repo.delete_auth_code("never-existed")  # must not raise


def test_save_and_get_access_token(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_access_token(
        token="tok-abc",
        client_id="c1",
        scopes=["read", "write"],
        expires_at=int(time.time()) + 3600,
        subject="user-id-2",
    )
    row = repo.get_access_token("tok-abc")
    assert row is not None
    assert row["token"] == "tok-abc"
    assert row["client_id"] == "c1"
    assert row["scopes"] == ["read", "write"]
    assert row["subject"] == "user-id-2"


def test_get_access_token_missing_returns_none(repo):
    assert repo.get_access_token("nope") is None


def test_revoke_access_token(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_access_token(
        token="tok-rev",
        client_id="c1",
        scopes=[],
        expires_at=int(time.time()) + 3600,
        subject="u1",
    )
    repo.revoke_access_token("tok-rev")
    row = repo.get_access_token("tok-rev")
    assert row is not None
    assert row["revoked_at"] is not None


def test_save_and_get_refresh_token(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_refresh_token(
        token="ref-abc",
        client_id="c1",
        scopes=["read"],
        subject="user-id-3",
    )
    row = repo.get_refresh_token("ref-abc")
    assert row is not None
    assert row["token"] == "ref-abc"
    assert row["client_id"] == "c1"
    assert row["scopes"] == ["read"]
    assert row["subject"] == "user-id-3"


def test_save_and_get_refresh_token_preserves_resource(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_refresh_token(
        token="ref-res",
        client_id="c1",
        scopes=["read"],
        subject="user-id-3",
        resource="https://mcp.example/",
    )
    row = repo.get_refresh_token("ref-res")
    assert row is not None
    assert row["resource"] == "https://mcp.example/"


def test_get_refresh_token_missing_returns_none(repo):
    assert repo.get_refresh_token("nope") is None


def test_revoke_refresh_token(repo):
    repo.upsert_client(client_id="c1", redirect_uris=["https://x.example/cb"])
    repo.save_refresh_token(token="ref-rev", client_id="c1", scopes=[], subject="u1")
    repo.revoke_refresh_token("ref-rev")
    row = repo.get_refresh_token("ref-rev")
    assert row is not None
    assert row["revoked_at"] is not None
