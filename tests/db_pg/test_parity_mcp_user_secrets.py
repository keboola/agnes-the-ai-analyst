"""Parity tests for the per-user MCP secret endpoints.

Exercises GET/PUT/DELETE ``/api/mcp/sources/{source_id}/my-secret`` against
BOTH backends via ``seeded_app_both`` — the same assertions run once on
DuckDB and once on Postgres.

Before the fix these handlers constructed ``MCPSourceRepository(conn)`` and
``PerUserSecretsRepository(conn)`` off the raw DuckDB ``_get_db`` connection,
so on a Postgres instance they hit the wrong backend. The fix routes both
through the repo factory (``mcp_sources_repo`` / ``per_user_secrets_repo``).

Seed the source through the factory (``mcp_sources_repo().upsert(...)``),
then drive the endpoints with the analyst token and assert the round-trip.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    """Storing a secret requires a configured vault key (AGNES_VAULT_KEY) since
    the admin-vault hardening; provide a valid Fernet key for the store path."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))


def _seed_source(scope: str = "per_user") -> str:
    """Create an MCP source through the factory and return its id."""
    from src.repositories import mcp_sources_repo

    source_id = "notion"
    mcp_sources_repo().upsert(
        id=source_id,
        name="Notion",
        transport="http",
        url="https://mcp.notion.example/v1",
        scope=scope,
    )
    return source_id


def test_get_status_no_secret_yet(seeded_app_both):
    """GET reports has_secret=False + the source scope before any PUT."""
    client = seeded_app_both["client"]
    token = seeded_app_both["analyst_token"]
    source_id = _seed_source(scope="per_user")

    r = client.get(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_secret"] is False
    assert body["source_scope"] == "per_user"


def test_put_then_get_round_trip(seeded_app_both):
    """PUT stores the caller's secret; GET then reports has_secret=True."""
    client = seeded_app_both["client"]
    token = seeded_app_both["analyst_token"]
    source_id = _seed_source(scope="per_user")

    r = client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        json={"value": "secret-token-abc"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text

    r = client.get(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_secret"] is True

    # The stored value must decrypt back to cleartext through the factory.
    from src.repositories import per_user_secrets_repo
    assert per_user_secrets_repo().get(source_id, "analyst1") == "secret-token-abc"


def test_put_is_per_user_scoped(seeded_app_both):
    """A secret stored by the analyst is not visible to the admin caller."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    admin_token = seeded_app_both["admin_token"]
    source_id = _seed_source(scope="per_user")

    r = client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        json={"value": "analyst-only"},
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 204, r.text

    # Admin has not stored their own → has_secret False for them.
    r = client.get(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_secret"] is False


def test_delete_drops_secret(seeded_app_both):
    """DELETE removes the caller's secret; GET then reports has_secret=False."""
    client = seeded_app_both["client"]
    token = seeded_app_both["analyst_token"]
    source_id = _seed_source(scope="per_user")

    client.put(
        f"/api/mcp/sources/{source_id}/my-secret",
        json={"value": "to-be-deleted"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = client.delete(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text

    r = client.get(
        f"/api/mcp/sources/{source_id}/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_secret"] is False


def test_unknown_source_404(seeded_app_both):
    """All three endpoints 404 for an unregistered source id."""
    client = seeded_app_both["client"]
    token = seeded_app_both["analyst_token"]

    r = client.get(
        "/api/mcp/sources/does-not-exist/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text

    r = client.put(
        "/api/mcp/sources/does-not-exist/my-secret",
        json={"value": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text

    r = client.delete(
        "/api/mcp/sources/does-not-exist/my-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text
