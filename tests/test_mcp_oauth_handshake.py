"""End-to-end tests for the native OAuth 2.1 remote MCP connector.

Covers the surface a remote MCP client (Claude Desktop, Claude.ai, Cursor,
Cline, ChatGPT connectors, custom MCP SDK clients) drives when adding Agnes
as a custom connector:

  * OAuth discovery metadata published at the ORIGIN ROOT (RFC 8414 + 9728).
  * Unauthenticated MCP request → 401 with a ``WWW-Authenticate: Bearer``
    challenge that points at the protected-resource metadata.
  * RFC 7591 dynamic client registration.
  * The full authorization-code + PKCE flow: register → authorize → consent
    (bridged to the existing Agnes session) → token exchange → a usable
    access token that the provider verifies.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse


MCP_MOUNT = "/api/mcp/http"
MCP_ENDPOINT = f"{MCP_MOUNT}/mcp"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def test_discovery_metadata_at_origin_root(seeded_app):
    client = seeded_app["client"]

    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["authorization_endpoint"].endswith("/api/mcp/http/authorize")
    assert meta["token_endpoint"].endswith("/api/mcp/http/token")
    assert meta["registration_endpoint"].endswith("/api/mcp/http/register")
    assert "S256" in meta["code_challenge_methods_supported"]

    r = client.get("/.well-known/oauth-protected-resource/api/mcp/http")
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["resource"].endswith("/api/mcp/http")
    assert any(s.endswith("/api/mcp/http") for s in pr["authorization_servers"])


def test_discovery_metadata_at_path_aware_locations(seeded_app):
    """Strict clients (Cursor, Copilot, ChatGPT web) build the AS metadata URL
    per RFC 8414 §3 by inserting the well-known segment between host and the
    issuer's ``/api/mcp/http`` path. Lenient clients (Claude) fall back to the
    bare root, so only path-aware probers regressed before this fix."""
    client = seeded_app["client"]

    for path in (
        "/.well-known/oauth-authorization-server/api/mcp/http",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/api/mcp/http",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"
        meta = r.json()
        assert meta["authorization_endpoint"].endswith("/api/mcp/http/authorize")
        assert meta["token_endpoint"].endswith("/api/mcp/http/token")
        assert meta["registration_endpoint"].endswith("/api/mcp/http/register")


def test_discovery_metadata_uses_request_host_when_env_unset(seeded_app, monkeypatch):
    """Production behind a TLS proxy must advertise the public host even when
    AGNES_BASE_URL / SERVER_URL are unset."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    from app.main import create_app
    from starlette.testclient import TestClient

    client = TestClient(create_app(), base_url="https://agnes.keboola.com")
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["issuer"] == "https://agnes.keboola.com/api/mcp/http"
    assert meta["authorization_endpoint"] == "https://agnes.keboola.com/api/mcp/http/authorize"

    r = client.get("/.well-known/oauth-protected-resource/api/mcp/http")
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["resource"] == "https://agnes.keboola.com/api/mcp/http"


def test_unauthenticated_mcp_www_authenticate_uses_request_host(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    from app.main import create_app
    from starlette.testclient import TestClient

    client = TestClient(create_app(), base_url="https://agnes.keboola.com")
    r = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert 'resource_metadata="https://agnes.keboola.com/.well-known/oauth-protected-resource/api/mcp/http"' in www


def test_unauthenticated_mcp_returns_401_challenge(seeded_app):
    client = seeded_app["client"]
    r = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert www.lower().startswith("bearer")
    assert "resource_metadata" in www


def _register_client(client) -> dict:
    r = client.post(
        f"{MCP_MOUNT}/register",
        json={
            "client_name": "Test MCP Client",
            "redirect_uris": ["http://localhost:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_dynamic_client_registration(seeded_app):
    reg = _register_client(seeded_app["client"])
    assert reg["client_id"]
    assert "http://localhost:9999/callback" in reg["redirect_uris"]


def test_full_authorization_code_flow(seeded_app):
    admin_token = seeded_app["admin_token"]
    redirect_uri = "http://localhost:9999/callback"

    # Enter the TestClient context so the app lifespan runs — the streamable
    # MCP session manager must be active for the step-5 JSON-RPC call.
    with seeded_app["client"] as client:
        _run_full_flow(client, admin_token, redirect_uri)


def _run_full_flow(client, admin_token, redirect_uri):
    reg = _register_client(client)
    client_id = reg["client_id"]
    verifier, challenge = _pkce()

    # 1. authorize — the SDK validates params then redirects to our consent
    #    bridge. follow_redirects=False so we can read the Location chain.
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    consent_loc = r.headers["location"]
    assert "/api/mcp/oauth/consent" in consent_loc
    pending = parse_qs(urlparse(consent_loc).query)["pending"][0]

    # 2. consent GET with an authenticated Agnes session → shows the page
    #    (not a redirect to login). We carry the session via Bearer header.
    auth_hdr = {"Authorization": f"Bearer {admin_token}"}
    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending, "state": "xyz"},
        headers=auth_hdr,
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert "Authorize access" in r.text

    # 3. consent POST allow → redirect back to the client with ?code=.
    #    Send a TAMPERED state in the form body to prove it is ignored: the
    #    authoritative state is the one persisted server-side at authorize().
    #    A genuine browser same-origin form POST carries an Origin header; send
    #    one so the consent CSRF gate (_same_origin) sees it as same-origin.
    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "state": "TAMPERED", "action": "allow"},
        headers={**auth_hdr, "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    final = r.headers["location"]
    assert final.startswith(redirect_uri)
    qs = parse_qs(urlparse(final).query)
    assert qs["state"][0] == "xyz", "form-body state must not override the persisted state"
    code = qs["code"][0]

    # 4. token exchange with the PKCE verifier
    r = client.post(
        f"{MCP_MOUNT}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": reg.get("client_secret", ""),
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tok = r.json()
    access_token = tok["access_token"]
    assert tok["token_type"].lower() == "bearer"
    assert tok.get("refresh_token")

    # 5. the minted token authenticates: the OAuth provider verifies it as a
    #    live access token bound to the authorizing user. (Deterministic — does
    #    not depend on the streamable session manager being warm under load.)
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    verified = asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token))
    assert verified is not None, "minted access token must verify"
    assert verified.client_id == client_id
    assert verified.subject  # bound to the authorizing Agnes user


def test_consent_reads_session_from_access_token_cookie(seeded_app):
    """The browser drives consent with the Agnes session COOKIE, not a Bearer
    header. The canonical session cookie is ``access_token`` (set by every
    login provider) — if the consent bridge reads any other cookie name it
    never sees the logged-in user and bounces back to login forever. This
    exercises the cookie branch the Bearer-header happy-path test misses.
    """
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    # Consent GET carrying the session as the access_token cookie (no Bearer
    # header) must render the consent page, NOT redirect to login.
    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending, "state": "xyz"},
        cookies={"access_token": admin_token},
        follow_redirects=False,
    )
    assert r.status_code == 200, (
        f"consent must read the session from the access_token cookie "
        f"(got {r.status_code}, location={r.headers.get('location')})"
    )
    assert "Authorize access" in r.text


def test_consent_post_rejects_cross_origin(seeded_app):
    """The consent POST mints an auth code off the session — cross-origin
    submits (CSRF) must be rejected with 403."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Origin": "https://evil.example.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text


def test_consent_post_rejects_missing_origin_when_unpinned(seeded_app, monkeypatch):
    """A header-less consent POST must be rejected even when no public base URL
    is pinned (AGNES_BASE_URL/SERVER_URL unset). A genuine browser always sends
    Origin/Referer on a cross-origin form POST, so a header-less submit is a
    CSRF signal — the gate must not blanket-trust it just because the host is
    request-derived rather than env-pinned."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}"},  # no Origin/Referer
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text


def test_provider_rejects_unknown_token(seeded_app):
    """A token that was never issued must not verify."""
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    provider = AgnesMCPOAuthProvider()
    assert asyncio.run(provider.load_access_token("not-a-real-token")) is None
