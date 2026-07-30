"""``POST /api/admin/mcp-sources/{id}/oauth/register`` and
``PUT /api/admin/mcp-sources/{id}/oauth/client`` (2026-07-30 outbound MCP
OAuth sources spec §2).

Discovery/DCR calls are monkeypatched at the ``connectors.mcp.oauth_client``
module level (the handler imports those functions locally, at call time —
patching the module attribute is what a local ``from X import Y`` picks up).
No real network traffic.
"""

from __future__ import annotations

import ipaddress
import socket as socket_mod

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository

#: A real, publicly-routable (non-private/loopback/link-local) IPv4 used as
#: the canned DNS answer for every non-literal hostname below — keeps the
#: SSRF resolver's real logic exercised (parse, is_private/is_loopback/…
#: checks) without any actual network access.
_FAKE_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    """Resolve any ``*.example.com``-style hostname to a fixed public IP;
    pass literal IP addresses straight through to the real resolver (which
    doesn't hit the network for those). No real DNS traffic either way."""
    real_getaddrinfo = socket_mod.getaddrinfo

    def _fake(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 0, "", (_FAKE_PUBLIC_IP, 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr("src.net.ssrf_safe_client.socket.getaddrinfo", _fake)


def _hdr(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _seed_oauth_source(source_id="src_oauth_reg", url="https://mcp.example.com/mcp"):
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url=url,
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    return source_id


def _seed_non_oauth_source(source_id="src_bearer1"):
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url="https://mcp.example.com/mcp",
        auth_method="bearer",
        scope="shared",
    )
    conn.close()
    return source_id


_AS_METADATA = {
    "issuer": "https://as.example.com",
    "authorization_endpoint": "https://as.example.com/authorize",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
}


def _patch_discovery_success(monkeypatch, *, registered_client_id="new-client-id"):
    import connectors.mcp.oauth_client as oc

    async def _fake_discover_pr(source_url, *, client):
        return {"authorization_servers": ["https://as.example.com"]}

    async def _fake_discover_as(issuer, *, client):
        return _AS_METADATA

    async def _fake_register(as_metadata, *, redirect_uri, client, scopes=None, client_name="Agnes"):
        return oc.RegisteredOAuthClient(
            issuer=as_metadata["issuer"],
            client_id=registered_client_id,
            client_secret="new-secret",
            registration_access_token="new-rat",
            authorization_endpoint=as_metadata["authorization_endpoint"],
            token_endpoint=as_metadata["token_endpoint"],
            registration_endpoint=as_metadata["registration_endpoint"],
            scopes=scopes,
        )

    revoke_calls = []

    async def _fake_revoke(*, registration_endpoint, client_id, registration_access_token, client):
        revoke_calls.append((registration_endpoint, client_id, registration_access_token))

    monkeypatch.setattr(oc, "discover_protected_resource_metadata", _fake_discover_pr)
    monkeypatch.setattr(oc, "discover_as_metadata", _fake_discover_as)
    monkeypatch.setattr(oc, "register_dynamic_client", _fake_register)
    monkeypatch.setattr(oc, "best_effort_revoke_registration", _fake_revoke)
    return revoke_calls


# ---------------------------------------------------------------------------
# POST …/oauth/register
# ---------------------------------------------------------------------------


def test_register_requires_admin(seeded_app):
    source_id = _seed_oauth_source()
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register")
    assert r.status_code in (401, 403)


def test_register_404_for_missing_source(seeded_app):
    r = seeded_app["client"].post("/api/admin/mcp-sources/does-not-exist/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 404


def test_register_400_for_non_oauth_source(seeded_app):
    source_id = _seed_non_oauth_source()
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 400


def test_register_409_without_public_url(seeded_app, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    source_id = _seed_oauth_source()
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 409


def test_register_success_persists_row_and_never_echoes_secret(seeded_app, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    source_id = _seed_oauth_source()
    _patch_discovery_success(monkeypatch)

    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == "new-client-id"
    assert body["has_client_secret"] is True
    assert "client_secret" not in body
    assert "registration_access_token" not in body

    from src.repositories.mcp_source_oauth_clients import MCPSourceOAuthClientRepository

    conn = get_system_db()
    row = MCPSourceOAuthClientRepository(conn).get(source_id)
    conn.close()
    assert row["client_id"] == "new-client-id"
    assert row["client_secret"] == "new-secret"


def test_register_pkce_fail_closed_returns_502(seeded_app, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    source_id = _seed_oauth_source()
    import connectors.mcp.oauth_client as oc

    async def _fake_discover_pr(source_url, *, client):
        return {"authorization_servers": ["https://as.example.com"]}

    async def _fake_discover_as_no_pkce(issuer, *, client):
        return {**_AS_METADATA, "code_challenge_methods_supported": ["plain"]}

    monkeypatch.setattr(oc, "discover_protected_resource_metadata", _fake_discover_pr)
    monkeypatch.setattr(oc, "discover_as_metadata", _fake_discover_as_no_pkce)

    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 502
    assert "S256" in r.json()["detail"]


def test_register_idempotent_revokes_old_registration(seeded_app, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    source_id = _seed_oauth_source()

    from src.repositories.mcp_source_oauth_clients import MCPSourceOAuthClientRepository

    conn = get_system_db()
    MCPSourceOAuthClientRepository(conn).upsert(
        source_id,
        issuer="https://as.example.com",
        client_id="old-client-id",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_access_token="old-rat",
    )
    conn.close()

    revoke_calls = _patch_discovery_success(monkeypatch)

    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{source_id}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 200, r.text
    assert len(revoke_calls) == 1
    endpoint, client_id, rat = revoke_calls[0]
    assert client_id == "old-client-id"
    assert rat == "old-rat"

    conn = get_system_db()
    row = MCPSourceOAuthClientRepository(conn).get(source_id)
    conn.close()
    assert row["client_id"] == "new-client-id"  # replaced, not appended


# ---------------------------------------------------------------------------
# PUT …/oauth/client (manual escape hatch)
# ---------------------------------------------------------------------------


def _manual_payload(**overrides):
    body = {
        "client_id": "manual-client",
        "client_secret": "manual-secret",
        "authorization_endpoint": "https://as2.example.com/authorize",
        "token_endpoint": "https://as2.example.com/token",
    }
    body.update(overrides)
    return body


def test_manual_client_requires_admin(seeded_app):
    source_id = _seed_oauth_source("src_oauth_manual1")
    r = seeded_app["client"].put(f"/api/admin/mcp-sources/{source_id}/oauth/client", json=_manual_payload())
    assert r.status_code in (401, 403)


def test_manual_client_404_for_missing_source(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/does-not-exist/oauth/client", headers=_hdr(seeded_app), json=_manual_payload()
    )
    assert r.status_code == 404


def test_manual_client_400_for_non_oauth_source(seeded_app):
    source_id = _seed_non_oauth_source("src_bearer2")
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{source_id}/oauth/client", headers=_hdr(seeded_app), json=_manual_payload()
    )
    assert r.status_code == 400


def test_manual_client_success_defaults_issuer_from_authorization_endpoint(seeded_app):
    source_id = _seed_oauth_source("src_oauth_manual2")
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{source_id}/oauth/client", headers=_hdr(seeded_app), json=_manual_payload()
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issuer"] == "https://as2.example.com"
    assert body["has_client_secret"] is True
    assert "client_secret" not in body


def test_manual_client_rejects_http_url(seeded_app):
    source_id = _seed_oauth_source("src_oauth_manual3")
    payload = _manual_payload(token_endpoint="http://as2.example.com/token")
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{source_id}/oauth/client", headers=_hdr(seeded_app), json=payload
    )
    assert r.status_code == 400
    assert "token_endpoint" in r.json()["detail"]


def test_manual_client_rejects_loopback_ip(seeded_app):
    source_id = _seed_oauth_source("src_oauth_manual4")
    payload = _manual_payload(authorization_endpoint="https://127.0.0.1/authorize")
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{source_id}/oauth/client", headers=_hdr(seeded_app), json=payload
    )
    assert r.status_code == 400
    assert "authorization_endpoint" in r.json()["detail"]


def test_source_delete_cascades_oauth_rows(seeded_app, monkeypatch):
    """DELETE /api/admin/mcp-sources/{id} must leave no orphaned OAuth
    material: the client registration, every user's tokens, and in-flight
    flows all go (Devin Review on #1124)."""
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    sid = _seed_oauth_source(source_id="src_oauth_cascade")
    mcp_source_oauth_clients_repo().upsert(
        sid,
        issuer="https://as.example.com",
        client_id="cid",
        client_secret=None,
        registration_access_token=None,
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        scopes=None,
    )
    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "tok", refresh_token=None, expires_at=None, scopes=None)
    mcp_oauth_flows_repo().create("cascade-nonce", sid, "admin1", "verifier")

    r = seeded_app["client"].delete(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
    )
    assert r.status_code == 204, r.text
    assert mcp_source_oauth_clients_repo().get(sid) is None
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume("cascade-nonce") is None


def test_register_translates_ssrf_rejection_to_400(seeded_app, monkeypatch):
    """A discovery target resolving to a blocked address must produce an
    actionable 400, not an opaque 500 (Devin Review on #1124)."""
    import connectors.mcp.oauth_client as oc

    from src.net.ssrf_safe_client import SSRFRejected

    async def _boom(source_url, *, client):
        raise SSRFRejected("address_in_blocked_range: 10.0.0.5")

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    monkeypatch.setattr(oc, "discover_protected_resource_metadata", _boom)
    sid = _seed_oauth_source(source_id="src_oauth_ssrf")
    r = seeded_app["client"].post(
        f"/api/admin/mcp-sources/{sid}/oauth/register",
        headers=_hdr(seeded_app),
    )
    assert r.status_code == 400
    assert "oauth_endpoint_rejected" in r.json()["detail"]
    assert "address_in_blocked_range" in r.json()["detail"]


def test_register_returns_409_without_vault_key(seeded_app, monkeypatch):
    """Missing encryption key must give the same actionable 409 as the other
    credential endpoints, not a generic 500 (Devin Review on #1124)."""
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    _patch_discovery_success(monkeypatch)
    sid = _seed_oauth_source(source_id="src_oauth_novault")
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    _reset_ephemeral_key_for_tests()
    try:
        r = seeded_app["client"].post(
            f"/api/admin/mcp-sources/{sid}/oauth/register",
            headers=_hdr(seeded_app),
        )
    finally:
        _reset_ephemeral_key_for_tests()
    assert r.status_code == 409, r.text
    assert "vault_key_not_configured" in r.json()["detail"]


def test_manual_client_returns_409_without_vault_key(seeded_app, monkeypatch):
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    sid = _seed_oauth_source(source_id="src_oauth_novault2")
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    _reset_ephemeral_key_for_tests()
    try:
        r = seeded_app["client"].put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={
                "client_id": "cid",
                "client_secret": "sek",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
            },
        )
    finally:
        _reset_ephemeral_key_for_tests()
    assert r.status_code == 409, r.text
    assert "vault_key_not_configured" in r.json()["detail"]
