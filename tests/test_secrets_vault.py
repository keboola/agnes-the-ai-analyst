"""Tests for app/secrets_vault.py — Fernet-backed shared MCP source secrets.

Covers the encryption helpers + the SharedSecretsRepository round-trip,
including the env-var-key path and the ephemeral-fallback path.
"""
from __future__ import annotations

import duckdb
import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import (
    SharedSecretsRepository,
    _reset_ephemeral_key_for_tests,
    decrypt_secret,
    encrypt_secret,
)


@pytest.fixture(autouse=True)
def _reset_ephemeral():
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _conn_with_vault_table():
    conn = duckdb.connect(":memory:")
    conn.execute(
        """CREATE TABLE mcp_secrets (
              source_id        VARCHAR PRIMARY KEY,
              secret_value_enc BLOB NOT NULL,
              created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
              updated_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
           )"""
    )
    return conn


# ── cipher helpers ────────────────────────────────────────────────────────


def test_encrypt_decrypt_round_trip_with_env_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    token = encrypt_secret("hunter2")
    assert isinstance(token, bytes)
    assert decrypt_secret(token) == "hunter2"


def test_encrypt_decrypt_round_trip_with_ephemeral_key(monkeypatch):
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    token = encrypt_secret("hunter2")
    assert decrypt_secret(token) == "hunter2"


def test_invalid_env_key_raises(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encrypt_secret("x")


# ── SharedSecretsRepository ───────────────────────────────────────────────


def test_repo_upsert_get_round_trip(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    assert repo.get("src_test") is None
    repo.upsert("src_test", "topsecret-123")
    assert repo.has("src_test") is True
    assert repo.get("src_test") == "topsecret-123"


def test_repo_upsert_replaces_prior(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "first")
    repo.upsert("src_test", "second")
    assert repo.get("src_test") == "second"


def test_repo_delete_removes_row(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "x")
    repo.delete("src_test")
    assert repo.get("src_test") is None
    assert repo.has("src_test") is False


def test_repo_returns_none_on_decrypt_failure_after_key_rotation(monkeypatch):
    """Junk-decrypt returns None so callers can fall back to the env-var path."""
    # Encrypt under key A
    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_a)
    conn = _conn_with_vault_table()
    repo = SharedSecretsRepository(conn)
    repo.upsert("src_test", "value-encrypted-with-A")

    # Rotate to key B and clear the ephemeral cache; row is unreadable
    _reset_ephemeral_key_for_tests()
    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("AGNES_VAULT_KEY", key_b)

    assert repo.get("src_test") is None
