"""Cross-engine contract tests for the outbound MCP OAuth data layer
(``mcp_source_oauth_clients`` / ``mcp_user_oauth_tokens`` / ``mcp_oauth_flows``,
v109 — 2026-07-30 spec, PR 1 / phase 1).

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both;
the same return shapes must come back. Follows the pattern established in
``test_mcp_sources_contract.py`` / ``test_idempotency_contract.py``.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend, one per table
# ---------------------------------------------------------------------------


def _upgrade_pg_to_head(pg_engine, monkeypatch):
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
    return db_pg.get_engine()


def _make_duckdb_oauth_clients(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.mcp_source_oauth_clients import MCPSourceOAuthClientRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MCPSourceOAuthClientRepository(conn), conn


def _make_pg_oauth_clients(pg_engine, monkeypatch):
    engine = _upgrade_pg_to_head(pg_engine, monkeypatch)
    from src.repositories.mcp_source_oauth_clients_pg import MCPSourceOAuthClientPgRepository

    return MCPSourceOAuthClientPgRepository(engine), None


def _make_duckdb_user_tokens(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MCPUserOAuthTokenRepository(conn), conn


def _make_pg_user_tokens(pg_engine, monkeypatch):
    engine = _upgrade_pg_to_head(pg_engine, monkeypatch)
    from src.repositories.mcp_user_oauth_tokens_pg import MCPUserOAuthTokenPgRepository

    return MCPUserOAuthTokenPgRepository(engine), None


def _make_duckdb_flows(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.mcp_oauth_flows import MCPOAuthFlowRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MCPOAuthFlowRepository(conn), conn


def _make_pg_flows(pg_engine, monkeypatch):
    engine = _upgrade_pg_to_head(pg_engine, monkeypatch)
    from src.repositories.mcp_oauth_flows_pg import MCPOAuthFlowPgRepository

    return MCPOAuthFlowPgRepository(engine), None


@pytest.fixture(params=["duckdb", "pg"])
def oauth_clients_repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        repo, conn = _make_duckdb_oauth_clients(tmp_path)
        yield repo
        conn.close()
    else:
        repo, _ = _make_pg_oauth_clients(pg_engine, monkeypatch)
        yield repo


@pytest.fixture(params=["duckdb", "pg"])
def user_tokens_repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        repo, conn = _make_duckdb_user_tokens(tmp_path)
        yield repo
        conn.close()
    else:
        repo, _ = _make_pg_user_tokens(pg_engine, monkeypatch)
        yield repo


@pytest.fixture(params=["duckdb", "pg"])
def flows_repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        repo, conn = _make_duckdb_flows(tmp_path)
        yield repo
        conn.close()
    else:
        repo, _ = _make_pg_flows(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# mcp_source_oauth_clients
# ---------------------------------------------------------------------------


def test_oauth_client_upsert_then_get_round_trips_decrypted(oauth_clients_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    oauth_clients_repo.upsert(
        "src-1",
        issuer="https://as.example.com",
        client_id="client-abc",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        client_secret="s3cr3t",
        registration_access_token="rat-xyz",
        scopes="read write",
    )
    row = oauth_clients_repo.get("src-1")
    assert row is not None
    assert row["source_id"] == "src-1"
    assert row["issuer"] == "https://as.example.com"
    assert row["client_id"] == "client-abc"
    assert row["client_secret"] == "s3cr3t"
    assert row["registration_access_token"] == "rat-xyz"
    assert row["authorization_endpoint"] == "https://as.example.com/authorize"
    assert row["token_endpoint"] == "https://as.example.com/token"
    assert row["scopes"] == "read write"
    # ciphertext columns never leak to the caller
    assert "client_secret_enc" not in row
    assert "registration_access_token_enc" not in row


def test_oauth_client_public_pkce_client_has_no_secret(oauth_clients_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    oauth_clients_repo.upsert(
        "src-public",
        issuer="https://as.example.com",
        client_id="public-client",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    )
    row = oauth_clients_repo.get("src-public")
    assert row is not None
    assert row["client_secret"] is None
    assert row["registration_access_token"] is None


def test_oauth_client_upsert_replaces_existing_row(oauth_clients_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    oauth_clients_repo.upsert(
        "src-1",
        issuer="https://as.example.com",
        client_id="client-abc",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    )
    oauth_clients_repo.upsert(
        "src-1",
        issuer="https://as.example.com",
        client_id="client-rotated",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    )
    row = oauth_clients_repo.get("src-1")
    assert row is not None
    assert row["client_id"] == "client-rotated"


def test_oauth_client_get_returns_none_for_missing_id(oauth_clients_repo):
    assert oauth_clients_repo.get("not-here") is None


def test_oauth_client_delete_removes_row(oauth_clients_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    oauth_clients_repo.upsert(
        "src-1",
        issuer="https://as.example.com",
        client_id="client-abc",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    )
    assert oauth_clients_repo.get("src-1") is not None
    oauth_clients_repo.delete("src-1")
    assert oauth_clients_repo.get("src-1") is None


def test_oauth_client_delete_missing_id_is_idempotent(oauth_clients_repo):
    oauth_clients_repo.delete("never-existed")


# ---------------------------------------------------------------------------
# mcp_user_oauth_tokens
# ---------------------------------------------------------------------------


def test_user_token_upsert_then_get_round_trips_decrypted(user_tokens_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    user_tokens_repo.upsert(
        "src-1",
        "user-1",
        "access-tok",
        refresh_token="refresh-tok",
        expires_at=expires_at,
        scopes="read",
    )
    row = user_tokens_repo.get("src-1", "user-1")
    assert row is not None
    assert row["source_id"] == "src-1"
    assert row["user_id"] == "user-1"
    assert row["access_token"] == "access-tok"
    assert row["refresh_token"] == "refresh-tok"
    assert row["scopes"] == "read"
    assert "access_token_enc" not in row
    assert "refresh_token_enc" not in row


def test_user_token_refresh_token_optional(user_tokens_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    user_tokens_repo.upsert("src-1", "user-1", "access-tok")
    row = user_tokens_repo.get("src-1", "user-1")
    assert row is not None
    assert row["refresh_token"] is None
    assert row["expires_at"] is None


def test_user_token_upsert_replaces_existing_row(user_tokens_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    user_tokens_repo.upsert("src-1", "user-1", "access-tok-1")
    user_tokens_repo.upsert("src-1", "user-1", "access-tok-2", refresh_token="refresh-2")
    row = user_tokens_repo.get("src-1", "user-1")
    assert row is not None
    assert row["access_token"] == "access-tok-2"
    assert row["refresh_token"] == "refresh-2"


def test_user_token_scoped_per_source_and_user(user_tokens_repo, monkeypatch):
    """Rotated refresh tokens / access tokens are scoped to the exact
    (source_id, user_id) pair — no cross-user or cross-source leakage."""
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    user_tokens_repo.upsert("src-1", "user-1", "tok-a")
    user_tokens_repo.upsert("src-1", "user-2", "tok-b")
    user_tokens_repo.upsert("src-2", "user-1", "tok-c")
    assert user_tokens_repo.get("src-1", "user-1")["access_token"] == "tok-a"
    assert user_tokens_repo.get("src-1", "user-2")["access_token"] == "tok-b"
    assert user_tokens_repo.get("src-2", "user-1")["access_token"] == "tok-c"


def test_user_token_get_returns_none_for_missing_pair(user_tokens_repo):
    assert user_tokens_repo.get("src-1", "user-1") is None


def test_user_token_has_reflects_presence(user_tokens_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    assert user_tokens_repo.has("src-1", "user-1") is False
    user_tokens_repo.upsert("src-1", "user-1", "tok")
    assert user_tokens_repo.has("src-1", "user-1") is True


def test_user_token_delete_removes_row(user_tokens_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    user_tokens_repo.upsert("src-1", "user-1", "tok")
    assert user_tokens_repo.has("src-1", "user-1") is True
    user_tokens_repo.delete("src-1", "user-1")
    assert user_tokens_repo.has("src-1", "user-1") is False
    assert user_tokens_repo.get("src-1", "user-1") is None


def test_user_token_delete_missing_pair_is_idempotent(user_tokens_repo):
    user_tokens_repo.delete("never", "existed")


def test_user_token_delete_for_source_drops_all_users(user_tokens_repo, monkeypatch):
    """Source-delete cascade: EVERY user's tokens for the source go, other
    sources' rows survive (Devin Review on #1124)."""
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    user_tokens_repo.upsert("src-1", "user-1", "tok-a")
    user_tokens_repo.upsert("src-1", "user-2", "tok-b")
    user_tokens_repo.upsert("src-2", "user-1", "tok-c")
    assert user_tokens_repo.delete_for_source("src-1") == 2
    assert user_tokens_repo.has("src-1", "user-1") is False
    assert user_tokens_repo.has("src-1", "user-2") is False
    assert user_tokens_repo.has("src-2", "user-1") is True
    assert user_tokens_repo.delete_for_source("src-1") == 0


# ---------------------------------------------------------------------------
# mcp_oauth_flows
# ---------------------------------------------------------------------------


def test_flow_create_then_consume_round_trips_decrypted(flows_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    flows_repo.create("nonce-1", "src-1", "user-1", "verifier-xyz")
    flow = flows_repo.consume("nonce-1")
    assert flow is not None
    assert flow["nonce"] == "nonce-1"
    assert flow["source_id"] == "src-1"
    assert flow["user_id"] == "user-1"
    assert flow["pkce_verifier"] == "verifier-xyz"


def test_flow_consume_is_single_use(flows_repo, monkeypatch):
    """Second consume() for the same nonce (replay, or a concurrent racer)
    gets None — the row is gone after the first successful read."""
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    flows_repo.create("nonce-1", "src-1", "user-1", "verifier-xyz")
    first = flows_repo.consume("nonce-1")
    assert first is not None
    second = flows_repo.consume("nonce-1")
    assert second is None


def test_flow_consume_missing_nonce_returns_none(flows_repo):
    assert flows_repo.consume("never-created") is None


def test_flow_sweep_expired_removes_old_rows_only(flows_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    flows_repo.create("old-nonce", "src-1", "user-1", "verifier-old")
    time.sleep(1.1)
    flows_repo.create("fresh-nonce", "src-1", "user-1", "verifier-fresh")

    deleted = flows_repo.sweep_expired(ttl_seconds=1)
    assert deleted == 1

    # The old flow is gone; the fresh one survives and is still consumable.
    assert flows_repo.consume("old-nonce") is None
    fresh = flows_repo.consume("fresh-nonce")
    assert fresh is not None
    assert fresh["pkce_verifier"] == "verifier-fresh"


def test_flow_sweep_expired_returns_zero_when_nothing_stale(flows_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    flows_repo.create("fresh-nonce", "src-1", "user-1", "verifier-fresh")
    assert flows_repo.sweep_expired(ttl_seconds=600) == 0


def test_flow_delete_for_source_drops_only_that_source(flows_repo, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "TWMxHbnAmXbo9lHXNfLC8_ItqIYWatKQ_rOx1Vgg1yA=")
    flows_repo.create("n1", "src-1", "user-1", "v1")
    flows_repo.create("n2", "src-1", "user-2", "v2")
    flows_repo.create("n3", "src-2", "user-1", "v3")
    assert flows_repo.delete_for_source("src-1") == 2
    assert flows_repo.consume("n1") is None
    assert flows_repo.consume("n3") is not None
    assert flows_repo.delete_for_source("src-1") == 0
