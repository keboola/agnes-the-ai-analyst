"""Tests for the per-user MCP secrets surface (RFC #461 §4 phase B).

Cover:

* ``PerUserSecretsRepository`` round-trip + key isolation.
* The user-facing PUT / DELETE / GET ``/api/mcp/sources/{id}/my-secret``.
* ``_lookup_secret_for_source`` precedence — per-user vault wins over
  shared vault when scope='per_user' AND caller_user_id matches; falls
  back to shared when the analyst has no row, regardless of scope.
"""
from __future__ import annotations

import duckdb
import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.secrets_vault import (
    PerUserSecretsRepository,
    SharedSecretsRepository,
    _reset_ephemeral_key_for_tests,
)
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _per_user_conn():
    conn = duckdb.connect(":memory:")
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


# ── PerUserSecretsRepository ───────────────────────────────────────────────


def test_repo_upsert_get_round_trip():
    conn = _per_user_conn()
    repo = PerUserSecretsRepository(conn)
    repo.upsert("src_n", "user_a", "alice-token")
    assert repo.has("src_n", "user_a")
    assert repo.get("src_n", "user_a") == "alice-token"


def test_repo_isolates_users():
    conn = _per_user_conn()
    repo = PerUserSecretsRepository(conn)
    repo.upsert("src_n", "user_a", "alice")
    repo.upsert("src_n", "user_b", "bob")
    assert repo.get("src_n", "user_a") == "alice"
    assert repo.get("src_n", "user_b") == "bob"
    repo.delete("src_n", "user_a")
    assert repo.get("src_n", "user_a") is None
    # user_b row survives
    assert repo.get("src_n", "user_b") == "bob"


def test_repo_list_for_source_returns_user_ids_only():
    conn = _per_user_conn()
    repo = PerUserSecretsRepository(conn)
    repo.upsert("src_n", "user_a", "alice")
    repo.upsert("src_n", "user_b", "bob")
    repo.upsert("src_other", "user_a", "x")
    user_ids = repo.list_for_source("src_n")
    assert set(user_ids) == {"user_a", "user_b"}


# ── REST: PUT / GET / DELETE /my-secret ───────────────────────────────────


def _seed_per_user_source(scope: str = "per_user", source_id: str = "src_pu") -> None:
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=f"pu-up-{source_id}",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="bearer",
        scope=scope,
    )
    conn.close()


def test_my_secret_put_then_status_returns_yes(seeded_app):
    _seed_per_user_source()
    client = seeded_app["client"]
    r = client.put(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "my-personal-token"},
    )
    assert r.status_code == 204
    r2 = client.get(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["has_secret"] is True
    assert body["source_scope"] == "per_user"


def test_my_secret_404_for_unknown_source(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/mcp/sources/src_nope/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "x"},
    )
    assert r.status_code == 404


def test_my_secret_isolates_per_user(seeded_app):
    """Admin's PUT shouldn't be visible to analyst's GET (different user_id)."""
    _seed_per_user_source()
    client = seeded_app["client"]
    client.put(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "admin-token"},
    )
    r = client.get(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.json()["has_secret"] is False


def test_my_secret_delete_clears(seeded_app):
    _seed_per_user_source()
    client = seeded_app["client"]
    client.put(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "x"},
    )
    r = client.delete(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 204
    r2 = client.get(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r2.json()["has_secret"] is False


# ── client._lookup_secret_for_source precedence ────────────────────────────


def test_lookup_per_user_wins_over_shared_when_scope_per_user(seeded_app):
    """With scope='per_user' and a per-user vault row, the per-user value
    wins over both shared vault and env-var."""
    from connectors.mcp.client import _lookup_secret_for_source

    _seed_per_user_source()
    client = seeded_app["client"]
    # Seed shared
    client.put(
        "/api/admin/mcp-sources/src_pu/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "shared-fallback"},
    )
    # Seed per-user for analyst
    client.put(
        "/api/mcp/sources/src_pu/my-secret",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "analyst-own"},
    )
    src = {"id": "src_pu", "scope": "per_user"}
    assert _lookup_secret_for_source(src, caller_user_id="analyst1") == "analyst-own"
    # Admin has no per-user row → shared fallback
    assert _lookup_secret_for_source(src, caller_user_id="admin1") == "shared-fallback"


def test_lookup_falls_back_to_shared_when_scope_shared(seeded_app):
    _seed_per_user_source(scope="shared")
    client = seeded_app["client"]
    client.put(
        "/api/admin/mcp-sources/src_pu/secret",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "shared-only"},
    )
    # Even with caller_user_id, scope=shared bypasses per-user lookup.
    from connectors.mcp.client import _lookup_secret_for_source
    src = {"id": "src_pu", "scope": "shared"}
    assert _lookup_secret_for_source(src, caller_user_id="analyst1") == "shared-only"
