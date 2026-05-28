"""Tests for the PUT/DELETE /api/admin/mcp-sources/{id}/secret endpoints.

Verify admin-only gating + the round trip through the vault, including
that connectors/mcp/client._lookup_secret_for_source picks up the
stored value over the legacy env-var.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.secrets_vault import SharedSecretsRepository, _reset_ephemeral_key_for_tests
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    """Use a stable vault key per test so the ephemeral-on-restart trap
    can't make a passing test flake when re-run."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _seed_source(source_id: str = "src_v") -> None:
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=f"vault-up-{source_id}",
        transport="http",
        url="https://upstream.example.com/mcp",
        auth_method="bearer",
        auth_secret_env="UPSTREAM_TOKEN_ENV",
    )
    conn.close()


# ── admin gate ────────────────────────────────────────────────────────────


def test_set_secret_requires_admin(seeded_app):
    _seed_source()
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "x"},
    )
    assert r.status_code == 403


def test_set_secret_404_for_unknown_source(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_nope/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "x"},
    )
    assert r.status_code == 404


def test_set_secret_rejects_empty(seeded_app):
    _seed_source()
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": ""},
    )
    assert r.status_code == 400


# ── round trip + vault precedence ────────────────────────────────────────


def test_set_then_get_decrypts_through_repository(seeded_app):
    _seed_source()
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "vault-secret-abc"},
    )
    assert r.status_code == 204

    conn = get_system_db()
    try:
        stored = SharedSecretsRepository(conn).get("src_v")
    finally:
        conn.close()
    assert stored == "vault-secret-abc"


def test_delete_clears_vault(seeded_app):
    _seed_source()
    client = seeded_app["client"]
    client.put(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "x"},
    )
    r = client.delete(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 204

    conn = get_system_db()
    try:
        assert SharedSecretsRepository(conn).get("src_v") is None
    finally:
        conn.close()


def test_client_lookup_uses_vault_over_env(seeded_app, monkeypatch):
    """connectors/mcp/client._lookup_secret_for_source should prefer the
    vault row over the env-var named in auth_secret_env."""
    from connectors.mcp.client import _lookup_secret_for_source

    _seed_source()
    monkeypatch.setenv("UPSTREAM_TOKEN_ENV", "from-env-not-from-vault")

    # No vault row yet → env wins
    src = {"id": "src_v", "auth_secret_env": "UPSTREAM_TOKEN_ENV"}
    assert _lookup_secret_for_source(src) == "from-env-not-from-vault"

    # Write vault row → vault wins
    client = seeded_app["client"]
    client.put(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "from-vault"},
    )
    assert _lookup_secret_for_source(src) == "from-vault"

    # Drop the vault row → env wins again
    client.delete(
        "/api/admin/mcp-sources/src_v/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert _lookup_secret_for_source(src) == "from-env-not-from-vault"
