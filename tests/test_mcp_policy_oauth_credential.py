"""``enforce_per_user_credential``'s ``auth_method='oauth'`` branch
(2026-07-30 outbound MCP OAuth sources spec §4).

An oauth source has no ``mcp_user_secrets`` row at all — the fail-closed
check instead consults ``mcp_user_oauth_tokens`` and treats a
present-but-expired-and-unrefreshable row as missing, raising the SAME
``PerUserCredentialMissing`` (same remedy string) as the secret-backed
per_user path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.api.mcp_policy import PerUserCredentialMissing, enforce_per_user_credential
from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.duckdb_conn import _open_duckdb
from src.repositories.mcp_user_oauth_tokens import MCPUserOAuthTokenRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _oauth_conn():
    conn = _open_duckdb(":memory:")
    conn.execute(
        """CREATE TABLE mcp_user_oauth_tokens (
              source_id         VARCHAR NOT NULL,
              user_id           VARCHAR NOT NULL,
              access_token_enc  BLOB NOT NULL,
              refresh_token_enc BLOB,
              expires_at        TIMESTAMP,
              scopes            VARCHAR,
              created_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
              PRIMARY KEY (source_id, user_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE mcp_user_secrets (
              source_id        VARCHAR NOT NULL,
              user_id          VARCHAR NOT NULL,
              secret_value_enc BLOB NOT NULL,
              created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
              PRIMARY KEY (source_id, user_id)
           )"""
    )
    return conn


@pytest.fixture
def oauth_db(monkeypatch):
    conn = _oauth_conn()
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    return conn


_SOURCE = {"id": "src_oauth1", "name": "oauth-src", "scope": "per_user", "auth_method": "oauth"}


def test_no_row_raises_missing(oauth_db):
    with pytest.raises(PerUserCredentialMissing):
        enforce_per_user_credential(_SOURCE, "user1")


def test_fresh_token_row_is_not_missing(oauth_db):
    MCPUserOAuthTokenRepository(oauth_db).upsert(
        "src_oauth1", "user1", "at-1", refresh_token="rt-1", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    enforce_per_user_credential(_SOURCE, "user1")  # no raise


def test_non_expiring_row_is_not_missing(oauth_db):
    MCPUserOAuthTokenRepository(oauth_db).upsert("src_oauth1", "user1", "at-1", expires_at=None)
    enforce_per_user_credential(_SOURCE, "user1")  # no raise


def test_expired_with_refresh_token_is_not_missing(oauth_db):
    """Still refreshable at call time — not a dead end, so not 'missing'."""
    MCPUserOAuthTokenRepository(oauth_db).upsert(
        "src_oauth1",
        "user1",
        "at-1",
        refresh_token="rt-1",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    enforce_per_user_credential(_SOURCE, "user1")  # no raise


def test_expired_without_refresh_token_is_missing(oauth_db):
    MCPUserOAuthTokenRepository(oauth_db).upsert(
        "src_oauth1",
        "user1",
        "at-1",
        refresh_token=None,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with pytest.raises(PerUserCredentialMissing):
        enforce_per_user_credential(_SOURCE, "user1")


def test_missing_row_raises_the_same_remedy_string_as_secret_backed_sources(oauth_db):
    """Same exception type + message contract regardless of credential kind
    (spec §4: 'one 403 message contract across both kinds')."""
    secret_source = {"id": "src_secret1", "name": "secret-src", "scope": "per_user", "auth_method": "bearer"}
    with pytest.raises(PerUserCredentialMissing) as oauth_exc:
        enforce_per_user_credential(_SOURCE, "user1")
    with pytest.raises(PerUserCredentialMissing) as secret_exc:
        enforce_per_user_credential(secret_source, "user1")
    # Both messages follow the exact same template modulo the source label/id.
    assert str(oauth_exc.value).replace("oauth-src", "X").replace("src_oauth1", "Y") == str(secret_exc.value).replace(
        "secret-src", "X"
    ).replace("src_secret1", "Y")


def test_caller_less_path_is_noop_even_for_oauth(oauth_db):
    """Materialize path (no caller) never reaches oauth or vault lookups."""
    enforce_per_user_credential(_SOURCE, None)  # no raise


def test_shared_scope_is_noop_even_with_oauth_auth_method(oauth_db):
    enforce_per_user_credential({**_SOURCE, "scope": "shared"}, "user1")  # no raise
