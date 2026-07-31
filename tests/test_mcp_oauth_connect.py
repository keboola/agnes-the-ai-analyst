"""Per-user OAuth connect flow — authorize + callback + disconnect
(2026-07-30 outbound MCP OAuth sources spec §3, PR 2).

No real network traffic: ``connectors.mcp.oauth_client.exchange_code_for_token``
is monkeypatched at the module level (the handler imports it locally, at
call time — patching the module attribute is what a local ``from X import
Y`` picks up, same convention as ``test_admin_mcp_oauth_register.py``).
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.auth.oauth_connect_state import ConnectStateInvalid, sign_connect_state, verify_connect_state
from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


@pytest.fixture(autouse=True)
def _public_url(monkeypatch):
    # authorize/callback build the redirect_uri from server.public_url —
    # required for the client-registration lookup helper shared with the
    # admin oauth/register endpoint (app.api.admin_mcp._oauth_redirect_uri).
    monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    from app.api.mcp_policy import reset_rate_buckets_for_tests

    reset_rate_buckets_for_tests()
    yield
    reset_rate_buckets_for_tests()


def _seed_oauth_source(
    source_id: str = "src_oauth_connect",
    grant_to: str = "analyst1",
    register_client: bool = True,
) -> str:
    """Seed a per_user oauth source, one grant, and (by default) an already
    registered OAuth client row — the authorize endpoint requires one."""
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url="https://mcp.example.com/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id=f"{source_id}.lookup",
        source_id=source_id,
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="grant target",
    )
    grp = UserGroupsRepository(conn).create(name=f"grant-{source_id}", description=None)
    tools.add_grant(f"{source_id}.lookup", grp["id"])
    UserGroupMembersRepository(conn).add_member(grant_to, grp["id"], source="system_seed")
    conn.close()

    if register_client:
        from src.repositories import mcp_source_oauth_clients_repo

        mcp_source_oauth_clients_repo().upsert(
            source_id,
            issuer="https://as.example.com",
            client_id="agnes-client",
            client_secret="agnes-secret",
            registration_access_token=None,
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            scopes=None,
        )
    return source_id


def _seed_non_oauth_source(source_id: str = "src_bearer_connect") -> str:
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


def _analyst_hdr(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['analyst_token']}"}


def _admin_hdr(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


# ---------------------------------------------------------------------------
# app.auth.oauth_connect_state
# ---------------------------------------------------------------------------


def test_state_round_trip(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 32)
    state = sign_connect_state("src_1", "user_1", "nonce_1")
    data = verify_connect_state(state)
    assert data == {"source_id": "src_1", "user_id": "user_1", "nonce": "nonce_1"}


def test_state_rejects_tampered_signature(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 32)
    state = sign_connect_state("src_1", "user_1", "nonce_1")
    with pytest.raises(ConnectStateInvalid):
        verify_connect_state(state[:-1] + ("a" if state[-1] != "a" else "b"))


def test_state_rejects_expired(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 32)
    state = sign_connect_state("src_1", "user_1", "nonce_1")
    real_time = time.time

    monkeypatch.setattr("itsdangerous.timed.time", type("T", (), {"time": staticmethod(lambda: real_time() + 700)}))
    with pytest.raises(ConnectStateInvalid):
        verify_connect_state(state)


def test_state_signed_with_one_secret_rejected_with_another(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 32)
    state = sign_connect_state("src_1", "user_1", "nonce_1")
    monkeypatch.setenv("JWT_SECRET_KEY", "b" * 32)
    with pytest.raises(ConnectStateInvalid):
        verify_connect_state(state)


# ---------------------------------------------------------------------------
# GET /api/mcp/sources/{id}/oauth/authorize
# ---------------------------------------------------------------------------


def test_authorize_requires_auth(seeded_app):
    source_id = _seed_oauth_source()
    r = seeded_app["client"].get(f"/api/mcp/sources/{source_id}/oauth/authorize", follow_redirects=False)
    assert r.status_code == 401


def test_authorize_404_for_unknown_source(seeded_app):
    r = seeded_app["client"].get(
        "/api/mcp/sources/does-not-exist/oauth/authorize",
        headers=_analyst_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_authorize_400_for_non_oauth_source(seeded_app):
    source_id = _seed_non_oauth_source()
    r = seeded_app["client"].get(
        f"/api/mcp/sources/{source_id}/oauth/authorize",
        headers=_analyst_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_authorize_403_without_grant(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_nogrant", grant_to="nobody")
    r = seeded_app["client"].get(
        f"/api/mcp/sources/{source_id}/oauth/authorize",
        headers=_analyst_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_authorize_409_without_registered_client(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_noclient", register_client=False)
    r = seeded_app["client"].get(
        f"/api/mcp/sources/{source_id}/oauth/authorize",
        headers=_analyst_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 409


def test_authorize_redirects_with_pkce_and_state(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_authok")
    r = seeded_app["client"].get(
        f"/api/mcp/sources/{source_id}/oauth/authorize",
        headers=_analyst_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("https://as.example.com/authorize?")
    qs = parse_qs(urlparse(loc).query)
    assert qs["client_id"][0] == "agnes-client"
    assert qs["response_type"][0] == "code"
    assert qs["code_challenge_method"][0] == "S256"
    assert qs["code_challenge"][0]
    assert qs["redirect_uri"][0].endswith("/api/mcp/oauth-client/callback")
    state = qs["state"][0]

    from app.auth.oauth_connect_state import verify_connect_state

    data = verify_connect_state(state)
    assert data["source_id"] == source_id
    assert data["user_id"] == "analyst1"

    # The PKCE verifier landed in a DB-backed flow row keyed by the state's nonce.
    from src.repositories import mcp_oauth_flows_repo

    flow = mcp_oauth_flows_repo().consume(data["nonce"])
    assert flow is not None
    assert flow["source_id"] == source_id
    assert flow["user_id"] == "analyst1"
    assert flow["pkce_verifier"]


def test_authorize_rate_limited(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_ratelimit")
    client = seeded_app["client"]
    hdr = _analyst_hdr(seeded_app)
    last = None
    for _ in range(10):
        last = client.get(f"/api/mcp/sources/{source_id}/oauth/authorize", headers=hdr, follow_redirects=False)
    assert last.status_code == 429
    assert "Retry-After" in last.headers


# ---------------------------------------------------------------------------
# GET /api/mcp/oauth-client/callback
# ---------------------------------------------------------------------------


def _authorize_and_extract_state(seeded_app, source_id: str, hdr: dict) -> str:
    r = seeded_app["client"].get(
        f"/api/mcp/sources/{source_id}/oauth/authorize",
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    return parse_qs(urlparse(loc).query)["state"][0]


class _FakeTokenSet:
    def __init__(self, access_token="atok", refresh_token="rtok", expires_in=3600, scopes=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.scopes = scopes


def _patch_exchange(monkeypatch, *, result=None, exc=None):
    import connectors.mcp.oauth_client as oc

    captured = {}

    async def _fake_exchange(*, token_endpoint, client_id, client_secret, code, redirect_uri, code_verifier, client):
        captured.update(
            token_endpoint=token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        if exc:
            raise exc
        return result or _FakeTokenSet()

    monkeypatch.setattr(oc, "exchange_code_for_token", _fake_exchange)
    return captured


def test_callback_happy_path_persists_tokens_and_redirects(seeded_app, monkeypatch):
    source_id = _seed_oauth_source(source_id="src_oauth_cb_ok")
    hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, hdr)
    captured = _patch_exchange(monkeypatch)

    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "auth-code-123", "state": state},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/me/connections?connected={source_id}"
    # Mix-up defense: token endpoint + client identity came from the STORED
    # client row, not from request data.
    assert captured["token_endpoint"] == "https://as.example.com/token"
    assert captured["client_id"] == "agnes-client"
    assert captured["client_secret"] == "agnes-secret"
    assert captured["code"] == "auth-code-123"

    from src.repositories import mcp_user_oauth_tokens_repo

    row = mcp_user_oauth_tokens_repo().get(source_id, "analyst1")
    assert row is not None
    assert row["access_token"] == "atok"
    assert row["refresh_token"] == "rtok"
    assert row["expires_at"] is not None


def test_callback_rejects_missing_code_or_state(seeded_app):
    hdr = _analyst_hdr(seeded_app)
    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"state": "whatever"},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error" in r.headers["location"]


def test_callback_surfaces_as_error_param(seeded_app):
    hdr = _analyst_hdr(seeded_app)
    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"error": "access_denied", "error_description": "user declined"},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error=user" in r.headers["location"] or "declined" in r.headers["location"]


def test_callback_rejects_tampered_state(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_cb_tamper")
    hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, hdr)
    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state + "x"},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error" in r.headers["location"]


def test_callback_nonce_is_single_use(seeded_app, monkeypatch):
    source_id = _seed_oauth_source(source_id="src_oauth_cb_replay")
    hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, hdr)
    _patch_exchange(monkeypatch)

    r1 = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state},
        headers=hdr,
        follow_redirects=False,
    )
    assert r1.status_code == 303
    assert "connected=" in r1.headers["location"]

    r2 = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state},
        headers=hdr,
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "connect_error" in r2.headers["location"]


def test_callback_login_csrf_rejects_state_for_a_different_session_user(seeded_app):
    """A state minted for analyst1's flow must not be redeemable by admin1's
    session, even with a syntactically valid signature (the state and flow
    row DO belong to a real, still-live flow — just not this caller's)."""
    source_id = _seed_oauth_source(source_id="src_oauth_cb_mixup", grant_to="analyst1")
    analyst_hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, analyst_hdr)

    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state},
        headers=_admin_hdr(seeded_app),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error" in r.headers["location"]

    from src.repositories import mcp_user_oauth_tokens_repo

    assert mcp_user_oauth_tokens_repo().has(source_id, "admin1") is False


def test_callback_rechecks_grant_revoked_while_away(seeded_app):
    """Grant revoked between authorize and callback → error redirect, no token stored."""
    from src.repositories.tool_registry import ToolRegistryRepository

    source_id = _seed_oauth_source(source_id="src_oauth_cb_revoked")
    hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, hdr)

    conn = get_system_db()
    ToolRegistryRepository(conn).remove_grant(
        f"{source_id}.lookup", UserGroupsRepository(conn).get_by_name(f"grant-{source_id}")["id"]
    )
    conn.close()

    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error" in r.headers["location"]

    from src.repositories import mcp_user_oauth_tokens_repo

    assert mcp_user_oauth_tokens_repo().has(source_id, "analyst1") is False


def test_callback_token_exchange_failure_redirects_with_error(seeded_app, monkeypatch):
    from connectors.mcp.oauth_client import OAuthTokenError

    source_id = _seed_oauth_source(source_id="src_oauth_cb_exfail")
    hdr = _analyst_hdr(seeded_app)
    state = _authorize_and_extract_state(seeded_app, source_id, hdr)
    _patch_exchange(monkeypatch, exc=OAuthTokenError("invalid_grant: code already used"))

    r = seeded_app["client"].get(
        "/api/mcp/oauth-client/callback",
        params={"code": "c", "state": state},
        headers=hdr,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "connect_error" in r.headers["location"]


def test_callback_deny_principal_for_restricted_token(seeded_app):
    """A co-session/agent-session token must never redeem a connect flow."""
    from app.auth.dependencies import get_current_user
    from app.auth.session_principal import SessionPrincipal

    principal = SessionPrincipal(session_id="s1", participant_user_ids=[], participant_emails=[], intersection={})
    app = seeded_app["client"].app
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        r = seeded_app["client"].get(
            "/api/mcp/oauth-client/callback",
            params={"code": "c", "state": "whatever"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/mcp/sources/{id}/oauth/connection
# ---------------------------------------------------------------------------


def test_disconnect_requires_auth(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_disc_auth")
    r = seeded_app["client"].delete(f"/api/mcp/sources/{source_id}/oauth/connection")
    assert r.status_code == 401


def test_disconnect_404_for_unknown_source(seeded_app):
    r = seeded_app["client"].delete(
        "/api/mcp/sources/does-not-exist/oauth/connection",
        headers=_analyst_hdr(seeded_app),
    )
    assert r.status_code == 404


def test_disconnect_403_without_grant(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_disc_nogrant", grant_to="nobody")
    r = seeded_app["client"].delete(
        f"/api/mcp/sources/{source_id}/oauth/connection",
        headers=_analyst_hdr(seeded_app),
    )
    assert r.status_code == 403


def test_disconnect_drops_token_row(seeded_app):
    from src.repositories import mcp_user_oauth_tokens_repo

    source_id = _seed_oauth_source(source_id="src_oauth_disc_ok")
    mcp_user_oauth_tokens_repo().upsert(source_id, "analyst1", "atok", refresh_token="rtok")
    assert mcp_user_oauth_tokens_repo().has(source_id, "analyst1") is True

    r = seeded_app["client"].delete(
        f"/api/mcp/sources/{source_id}/oauth/connection",
        headers=_analyst_hdr(seeded_app),
    )
    assert r.status_code == 204, r.text
    assert mcp_user_oauth_tokens_repo().has(source_id, "analyst1") is False


def test_disconnect_is_idempotent(seeded_app):
    source_id = _seed_oauth_source(source_id="src_oauth_disc_idem")
    r = seeded_app["client"].delete(
        f"/api/mcp/sources/{source_id}/oauth/connection",
        headers=_analyst_hdr(seeded_app),
    )
    assert r.status_code == 204
