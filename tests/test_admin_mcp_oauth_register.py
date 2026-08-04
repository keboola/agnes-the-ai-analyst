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


def test_reregister_revokes_old_only_after_new_registration_succeeds(seeded_app, monkeypatch):
    """Register-then-revoke ordering: a DCR failure during re-registration
    must leave the OLD stored registration intact and un-revoked, so
    connected users keep working (Devin Review on #1124)."""
    import connectors.mcp.oauth_client as oc

    from src.repositories import mcp_source_oauth_clients_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_rereg")
    revoke_calls = _patch_discovery_success(monkeypatch, registered_client_id="cid-old")
    r1 = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r1.status_code == 200, r1.text
    assert revoke_calls == []  # first registration — nothing to revoke

    async def _fail_register(meta, *, redirect_uri, client, scopes=None, client_name="Agnes"):
        raise oc.OAuthDiscoveryError("registration endpoint exploded")

    monkeypatch.setattr(oc, "register_dynamic_client", _fail_register)
    r2 = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r2.status_code == 502
    assert revoke_calls == []  # old registration NOT revoked on failure
    row = mcp_source_oauth_clients_repo().get(sid)
    assert row is not None and row["client_id"] == "cid-old"  # old row intact

    # A successful re-registration revokes the old client afterwards.
    revoke_calls2 = _patch_discovery_success(monkeypatch, registered_client_id="cid-new")
    r3 = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r3.status_code == 200, r3.text
    assert [c[1] for c in revoke_calls2] == ["cid-old"]
    assert mcp_source_oauth_clients_repo().get(sid)["client_id"] == "cid-new"


def test_manual_client_preserves_rat_for_same_client_id_only(seeded_app, monkeypatch):
    """PUT …/oauth/client keeps a DCR-issued registration access token when
    the client_id is unchanged (so re-register can still revoke upstream),
    and drops it when the client is replaced (Devin Review on #1124)."""
    from src.repositories import mcp_source_oauth_clients_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_rat")
    _patch_discovery_success(monkeypatch, registered_client_id="cid-dcr")
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 200, r.text
    assert mcp_source_oauth_clients_repo().get(sid)["registration_access_token"] == "new-rat"

    body = {
        "client_id": "cid-dcr",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "scopes": "read",
    }
    r2 = seeded_app["client"].put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
    assert r2.status_code == 200, r2.text
    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["registration_access_token"] == "new-rat"  # same client_id — kept
    assert row["scopes"] == "read"

    body["client_id"] = "cid-other"
    r3 = seeded_app["client"].put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
    assert r3.status_code == 200, r3.text
    assert mcp_source_oauth_clients_repo().get(sid)["registration_access_token"] is None  # replaced — dropped


def test_flipping_auth_method_away_from_oauth_purges_oauth_rows(seeded_app, monkeypatch):
    """PUT that flips auth_method oauth→bearer must not strand the client
    registration / user tokens / flows (Devin Review on #1124)."""
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    sid = _seed_oauth_source(source_id="src_oauth_flip")
    mcp_source_oauth_clients_repo().upsert(
        sid,
        issuer="https://as.example.com",
        client_id="cid",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
    )
    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "tok", refresh_token=None, expires_at=None, scopes=None)
    mcp_oauth_flows_repo().create("flip-nonce", sid, "admin1", "verifier")

    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        json={"auth_method": "bearer"},
    )
    assert r.status_code == 200, r.text
    assert mcp_source_oauth_clients_repo().get(sid) is None
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume("flip-nonce") is None


def test_manual_client_preserves_secret_when_omitted(seeded_app):
    """The secret is write-only over the API, so a form that re-saves scopes
    arrives with client_secret=None. That must NOT wipe the stored secret —
    doing so breaks every user's refresh. An explicit "" still clears it
    (Devin Review on #1124)."""
    from src.repositories import mcp_source_oauth_clients_repo

    sid = _seed_oauth_source(source_id="src_oauth_secret_keep")
    body = {
        "client_id": "cid-manual",
        "client_secret": "s3cr3t",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "scopes": "read",
    }
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
        .status_code
        == 200
    )

    # Re-save with a different scope and no secret — the common form round-trip.
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={**{k: v for k, v in body.items() if k != "client_secret"}, "scopes": "read write"},
        )
        .status_code
        == 200
    )
    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["client_secret"] == "s3cr3t"
    assert row["scopes"] == "read write"

    # Explicit empty string is a deliberate clear (public PKCE-only client).
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={**body, "client_secret": ""},
        )
        .status_code
        == 200
    )
    assert mcp_source_oauth_clients_repo().get(sid)["client_secret"] is None

    # A different client_id replaces the registration — secret goes with it.
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={**{k: v for k, v in body.items() if k != "client_secret"}, "client_id": "cid-other"},
        )
        .status_code
        == 200
    )
    assert mcp_source_oauth_clients_repo().get(sid)["client_secret"] is None


def test_reregister_with_new_client_id_drops_user_tokens(seeded_app, monkeypatch):
    """Tokens were issued to the old client_id, whose registration the
    re-register just revoked. Some AS answer invalid_client rather than
    invalid_grant on the refresh, which is not classified as a reconnect
    signal — the user would be stuck on an opaque 401. Drop them so the next
    call prompts a reconnect (Devin Review on #1124)."""
    from src.repositories import mcp_oauth_flows_repo, mcp_user_oauth_tokens_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_rereg_tokens")
    _patch_discovery_success(monkeypatch, registered_client_id="cid-first")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )

    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "tok", refresh_token="rt", expires_at=None, scopes=None)
    mcp_oauth_flows_repo().create("rereg-nonce", sid, "admin1", "verifier")

    # Same client_id back from the AS — nothing to invalidate.
    _patch_discovery_success(monkeypatch, registered_client_id="cid-first")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is True

    # New client_id — every user's tokens are now unusable.
    _patch_discovery_success(monkeypatch, registered_client_id="cid-second")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume("rereg-nonce") is None


def test_repointing_an_oauth_source_url_drops_credential_material(seeded_app):
    """Tokens are minted by the OLD resource's authorization server. Repointing
    `url` at a different upstream while staying on oauth would forward them as
    `Authorization: Bearer` to a new host — handing one server's credentials to
    another (Devin Review on #1124). Everything goes; a re-register + reconnect
    is required, same as a new source."""
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    def _seed_material(sid):
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
        mcp_oauth_flows_repo().create(f"{sid}-nonce", sid, "admin1", "verifier")

    sid = _seed_oauth_source(source_id="src_oauth_repoint")
    _seed_material(sid)
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        json={"url": "https://other-upstream.example.com/mcp"},
    )
    assert r.status_code == 200, r.text
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume(f"{sid}-nonce") is None
    assert mcp_source_oauth_clients_repo().get(sid) is None

    # An unrelated patch on the same source leaves the material alone.
    sid2 = _seed_oauth_source(source_id="src_oauth_no_repoint")
    _seed_material(sid2)
    r2 = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid2}",
        headers=_hdr(seeded_app),
        json={"connect_hint": "ask the platform team"},
    )
    assert r2.status_code == 200, r2.text
    assert mcp_user_oauth_tokens_repo().has(sid2, "admin1") is True
    assert mcp_source_oauth_clients_repo().get(sid2) is not None


def test_manual_client_with_a_new_client_id_drops_user_tokens(seeded_app):
    """The manual escape hatch strands tokens on the old client identity
    exactly as a DCR re-registration does, so it gets the same purge — a
    refresh against the new client can answer `invalid_client`, which is not
    classified as a reconnect signal (Devin Review on #1124)."""
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    sid = _seed_oauth_source(source_id="src_oauth_manual_swap")
    body = {
        "client_id": "cid-first",
        "client_secret": "s3cr3t",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
    }
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
        .status_code
        == 200
    )
    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "tok", refresh_token="rt", expires_at=None, scopes=None)
    mcp_oauth_flows_repo().create("manual-swap-nonce", sid, "admin1", "verifier")

    # Same client_id — a plain settings tweak leaves user state alone.
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json={**body, "scopes": "read"})
        .status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is True

    # Different client_id — every user's tokens are now unusable.
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={**body, "client_id": "cid-second"},
        )
        .status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume("manual-swap-nonce") is None
    # The new registration itself is stored, not purged.
    assert mcp_source_oauth_clients_repo().get(sid)["client_id"] == "cid-second"


def test_reregister_returning_the_same_client_id_does_not_revoke_it(seeded_app, monkeypatch):
    """RFC 7591 does not require a fresh client_id per registration — an AS
    that dedupes on client_name/redirect_uris hands back the same one.
    Revoking it would delete the registration just re-issued and leave the
    stored row pointing at nothing (Devin Review on #1124)."""
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_same_cid")
    revokes = _patch_discovery_success(monkeypatch, registered_client_id="cid-stable")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    assert revokes == []  # nothing to revoke on a first registration

    # AS returns the SAME client_id — the live registration must survive.
    revokes2 = _patch_discovery_success(monkeypatch, registered_client_id="cid-stable")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    assert revokes2 == [], "re-registration revoked the client_id the AS had just re-issued"

    # A genuinely different client_id still gets the old one revoked.
    revokes3 = _patch_discovery_success(monkeypatch, registered_client_id="cid-new")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    assert [c[1] for c in revokes3] == ["cid-stable"]


def test_manual_client_refuses_to_silently_demote_an_undecryptable_secret(seeded_app, monkeypatch):
    """After a vault-key rotation the stored secret is unreadable but still
    THERE. Carrying the decrypted None forward would write NULL and quietly
    convert a confidential registration into a public PKCE-only one. The save
    is refused instead, and `has_client_secret` keeps telling the truth
    (Devin Review on #1124)."""
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    sid = _seed_oauth_source(source_id="src_oauth_undecryptable")
    body = {
        "client_id": "cid-conf",
        "client_secret": "s3cr3t",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
    }
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
        .status_code
        == 200
    )

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()

    # A plain re-save with no secret must NOT wipe the column.
    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}/oauth/client",
        headers=_hdr(seeded_app),
        json={**{k: v for k, v in body.items() if k != "client_secret"}, "scopes": "read"},
    )
    assert r.status_code == 409, r.text
    assert "client_secret_undecryptable" in r.text

    # The API still reports a secret is on file — it is unreadable, not gone.
    got = seeded_app["client"].get(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app))
    if got.status_code == 200:
        assert got.json().get("has_client_secret") is True

    # An explicit clear is still allowed — that is a deliberate decision.
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={**body, "client_secret": ""},
        )
        .status_code
        == 200
    )


def test_manual_client_repointing_endpoints_drops_user_tokens(seeded_app):
    """A stored token is only usable against the exact (issuer, endpoints,
    client_id) it was minted for. Repointing `token_endpoint` at a different
    authorization server while KEEPING client_id would have the refresh path
    POST the old server's refresh token — and the client secret via Basic
    auth — to the new host (Devin Review on #1124)."""
    from src.repositories import mcp_oauth_flows_repo, mcp_user_oauth_tokens_repo

    sid = _seed_oauth_source(source_id="src_oauth_repoint_ep")
    body = {
        "client_id": "cid-same",
        "client_secret": "s3cr3t",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
    }
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)
        .status_code
        == 200
    )
    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "tok", refresh_token="rt", expires_at=None, scopes=None)
    mcp_oauth_flows_repo().create("repoint-ep-nonce", sid, "admin1", "verifier")

    # Same identity, only scopes change — tokens stay.
    assert (
        seeded_app["client"]
        .put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json={**body, "scopes": "read"})
        .status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is True

    # Same client_id, DIFFERENT authorization server — tokens must go.
    assert (
        seeded_app["client"]
        .put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            headers=_hdr(seeded_app),
            json={
                **body,
                "authorization_endpoint": "https://other-as.example.com/authorize",
                "token_endpoint": "https://other-as.example.com/token",
            },
        )
        .status_code
        == 200
    )
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is False
    assert mcp_oauth_flows_repo().consume("repoint-ep-nonce") is None


def test_padded_auth_method_does_not_disconnect_everyone(seeded_app):
    """A pasted `"oauth "` used to pass both validators (they strip), persist
    WITH the space, and then read as not-oauth by the flip check — purging
    every user's tokens and the client registration as if the admin had
    turned OAuth off, after which the source could not be re-registered.
    Normalizing at the API boundary is what stops it (invariant sweep
    on #1124)."""
    from src.repositories import mcp_source_oauth_clients_repo, mcp_sources_repo, mcp_user_oauth_tokens_repo

    sid = _seed_oauth_source(source_id="src_oauth_padded")
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

    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        json={"auth_method": "oauth "},
    )
    assert r.status_code == 200, r.text
    # Stored canonically, so every downstream `.lower()` reader agrees.
    assert mcp_sources_repo().get(sid)["auth_method"] == "oauth"
    # And nothing was treated as a flip away from oauth.
    assert mcp_user_oauth_tokens_repo().has(sid, "admin1") is True
    assert mcp_source_oauth_clients_repo().get(sid) is not None


def test_url_repoint_also_drops_non_oauth_credentials(seeded_app):
    """A `bearer` source's per-user token and a shared source's vault secret
    are forwarded as `Authorization` by the same seam that reads the freshly
    written url — so repointing sends them to a host that never issued them.
    The purge covers every credential kind, not just OAuth (invariant sweep
    on #1124)."""
    from src.repositories import mcp_sources_repo, per_user_secrets_repo

    sid = "src_bearer_repoint"
    mcp_sources_repo().upsert(
        id=sid,
        name="bearer_repoint",
        transport="http",
        url="https://h1.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    per_user_secrets_repo().upsert(sid, "admin1", "tok-for-h1")
    assert per_user_secrets_repo().has(sid, "admin1") is True

    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        json={"url": "https://h2.example/mcp"},
    )
    assert r.status_code == 200, r.text
    assert per_user_secrets_repo().has(sid, "admin1") is False


def test_a_rejected_patch_does_not_purge_credentials(seeded_app):
    """The purge runs before the row is repointed — so it must not fire for a
    request that is going to 400 anyway.

    A PUT can carry a new `url` *and* a field combination the repository
    rejects. The endpoint used to validate only the oauth coupling before
    writing, so every other repo rule failed inside `upsert` — which, once the
    purge moved ahead of the write, would have destroyed credentials for a
    request that changed nothing. The case below is one pydantic cannot catch:
    the patch is well-formed in isolation, and only the MERGE with the stored
    row (switching to `stdio` while the source has no `command`) is invalid.
    Both halves are asserted: the call is refused AND the credentials survive
    it (Devin Review on #1124).
    """
    from src.repositories import mcp_sources_repo, per_user_secrets_repo

    sid = "src_rejected_patch"
    mcp_sources_repo().upsert(
        id=sid,
        name="rejected_patch",
        transport="http",
        url="https://h1.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    per_user_secrets_repo().upsert(sid, "admin1", "tok-for-h1")

    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        # new url (would purge) + a transport the merged row cannot satisfy
        json={"url": "https://h2.example/mcp", "transport": "stdio"},
    )
    assert r.status_code == 400, r.text
    assert "command" in r.text
    assert per_user_secrets_repo().has(sid, "admin1") is True
    assert mcp_sources_repo().get(sid)["url"] == "https://h1.example/mcp"


def test_an_explicit_null_scope_cannot_slip_past_the_pre_purge_check(seeded_app):
    """The guard only holds if the validated row IS the written row.

    `exclude_unset` treats an explicit JSON `null` as set, so `{"scope": null}`
    used to reach the merge as `None`. The endpoint validated it with a
    substituted `"shared"` — which passes — then purged, then handed the raw
    `None` to `upsert`, which raised `unsupported scope: None` → 400. Credentials
    gone, edit refused. Defaults are now applied once, in the merge, so no
    caller can validate one value and write another (Devin Review on #1124).
    """
    from src.repositories import mcp_sources_repo, per_user_secrets_repo

    sid = "src_null_scope"
    mcp_sources_repo().upsert(
        id=sid,
        name="null_scope",
        transport="http",
        url="https://h1.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    per_user_secrets_repo().upsert(sid, "admin1", "tok-for-h1")

    r = seeded_app["client"].put(
        f"/api/admin/mcp-sources/{sid}",
        headers=_hdr(seeded_app),
        json={"url": "https://h2.example/mcp", "scope": None},
    )
    # Whatever the endpoint decides the null means, it must not both destroy
    # the credentials and refuse the edit.
    assert r.status_code == 200, r.text
    assert mcp_sources_repo().get(sid)["scope"] == "shared"
    # url DID change, so the purge is correct here — it just must not have run
    # for a request that then failed.
    assert per_user_secrets_repo().has(sid, "admin1") is False


def test_a_failed_purge_leaves_the_source_pointing_at_the_old_host(seeded_app, monkeypatch):
    """Pins the purge/write ORDER, which is the whole point of the fix.

    There is no transaction spanning the `mcp_sources` row and the vault
    tables, so a failure between them has to land somewhere. Purge-last put it
    in the dangerous place: the row already repointed, the old credentials
    still on file, and the next forward shipping them to the new host. With
    the purge first, the same failure leaves the source untouched — nothing is
    disclosed (Devin Review on #1124).
    """
    from src.repositories import mcp_sources_repo, per_user_secrets_repo

    sid = "src_purge_boom"
    mcp_sources_repo().upsert(
        id=sid,
        name="purge_boom",
        transport="http",
        url="https://h1.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    per_user_secrets_repo().upsert(sid, "admin1", "tok-for-h1")

    import app.api.admin_mcp as admin_mcp

    def _boom():
        raise RuntimeError("vault backend went away mid-purge")

    monkeypatch.setattr(admin_mcp, "shared_secrets_repo", _boom)

    with pytest.raises(RuntimeError):
        seeded_app["client"].put(
            f"/api/admin/mcp-sources/{sid}",
            headers=_hdr(seeded_app),
            json={"url": "https://h2.example/mcp"},
        )

    assert mcp_sources_repo().get(sid)["url"] == "https://h1.example/mcp"


def test_deduped_reregistration_keeps_the_secret_and_rat_it_was_not_re_issued(seeded_app, monkeypatch):
    """A deduping AS answers a second DCR with the SAME client_id and may
    re-issue neither the client secret nor the registration access token —
    RFC 7591 requires neither. Writing those Nones through wipes both: the
    lost RAT silently disables upstream deregistration, and the lost secret
    demotes a confidential registration to a public one, so client auth stops
    being sent and every token call fails. Same "keep what's on file" rule the
    manual PUT carries (Devin Review on #1124 for the RAT half).
    """
    import connectors.mcp.oauth_client as oc
    from src.repositories import mcp_source_oauth_clients_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_dedupe")

    _patch_discovery_success(monkeypatch, registered_client_id="cid-stable")
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 200, r.text
    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["client_secret"] == "new-secret"
    assert row["registration_access_token"] == "new-rat"

    # Second registration: same identity, nothing re-issued.
    revoke_calls = _patch_discovery_success(monkeypatch, registered_client_id="cid-stable")

    async def _dedupe_register(as_metadata, *, redirect_uri, client, scopes=None, client_name="Agnes"):
        return oc.RegisteredOAuthClient(
            issuer=as_metadata["issuer"],
            client_id="cid-stable",
            client_secret=None,
            registration_access_token=None,
            authorization_endpoint=as_metadata["authorization_endpoint"],
            token_endpoint=as_metadata["token_endpoint"],
            registration_endpoint=as_metadata["registration_endpoint"],
            scopes=scopes,
        )

    monkeypatch.setattr(oc, "register_dynamic_client", _dedupe_register)
    r2 = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r2.status_code == 200, r2.text
    assert revoke_calls == []  # same client_id — nothing to revoke

    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["client_secret"] == "new-secret"
    assert row["registration_access_token"] == "new-rat"


def test_a_genuinely_new_registration_replaces_secret_and_rat_wholesale(seeded_app, monkeypatch):
    """The counterpart: a DIFFERENT client_id is a new registration, so the old
    client's secret and RAT must NOT be carried onto it."""
    import connectors.mcp.oauth_client as oc
    from src.repositories import mcp_source_oauth_clients_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_replaced")

    _patch_discovery_success(monkeypatch, registered_client_id="cid-first")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )

    _patch_discovery_success(monkeypatch, registered_client_id="cid-second")

    async def _public_register(as_metadata, *, redirect_uri, client, scopes=None, client_name="Agnes"):
        return oc.RegisteredOAuthClient(
            issuer=as_metadata["issuer"],
            client_id="cid-second",
            client_secret=None,
            registration_access_token=None,
            authorization_endpoint=as_metadata["authorization_endpoint"],
            token_endpoint=as_metadata["token_endpoint"],
            registration_endpoint=as_metadata["registration_endpoint"],
            scopes=scopes,
        )

    monkeypatch.setattr(oc, "register_dynamic_client", _public_register)
    r = seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app))
    assert r.status_code == 200, r.text

    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["client_id"] == "cid-second"
    assert row["client_secret"] is None
    assert row["registration_access_token"] is None


def test_a_failed_token_purge_leaves_the_client_row_on_the_old_provider(seeded_app, monkeypatch):
    """The client-row endpoints follow the same purge-before-write ordering as
    the source `url` repoint, for the same reason.

    `connectors/mcp/client.py`'s refresh path reads `token_endpoint`,
    `client_id` and `client_secret` straight off the client row. Purging after
    the write leaves that row addressing the NEW authorization server while the
    OLD server's refresh tokens are still on file, so the next forward POSTs
    the old server's refresh token — and the client secret via Basic auth — to
    the new host (Devin Review on #1124).
    """
    from src.repositories import mcp_source_oauth_clients_repo, mcp_user_oauth_tokens_repo

    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
    sid = _seed_oauth_source(source_id="src_oauth_order")
    _patch_discovery_success(monkeypatch, registered_client_id="cid-old")
    assert (
        seeded_app["client"].post(f"/api/admin/mcp-sources/{sid}/oauth/register", headers=_hdr(seeded_app)).status_code
        == 200
    )
    mcp_user_oauth_tokens_repo().upsert(sid, "admin1", "at-old", refresh_token="rt-old", expires_at=None)

    import app.api.admin_mcp as admin_mcp

    real = admin_mcp.mcp_sources_repo

    def _boom(*a, **k):
        raise RuntimeError("token purge failed mid-flight")

    # Repoint the client at a DIFFERENT authorization server via the manual
    # PUT, with the token purge blowing up.
    monkeypatch.setattr(admin_mcp, "_oauth_identity_changed", _boom)
    body = {
        "client_id": "cid-old",
        "authorization_endpoint": "https://other-as.example.com/authorize",
        "token_endpoint": "https://other-as.example.com/token",
    }
    with pytest.raises(RuntimeError):
        seeded_app["client"].put(f"/api/admin/mcp-sources/{sid}/oauth/client", headers=_hdr(seeded_app), json=body)

    monkeypatch.setattr(admin_mcp, "mcp_sources_repo", real)
    row = mcp_source_oauth_clients_repo().get(sid)
    assert row["token_endpoint"] == "https://as.example.com/token", (
        "client row was repointed while the old provider's tokens were still on file"
    )
