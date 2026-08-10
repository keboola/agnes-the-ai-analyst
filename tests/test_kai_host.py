"""Host-side wiring for the embedded ``kai-agent`` turn engine (app/api/kai.py).

Covers the contract the engine's ``jwt`` host adapter actually enforces — the
claim set it requires, the ``exp`` ceiling it rejects past, and the ticket
payload shape it validates — plus the two things that make the credential
model safe: a broker ticket cannot mint more tickets, and rotating a turn's
egress tickets must not destroy the session credential the engine still holds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

KAI_SECRET = "test-kai-host-secret-that-is-at-least-32-chars"


@pytest.fixture
def kai_env(monkeypatch):
    monkeypatch.setenv("KAI_HOST_JWT_SECRET", KAI_SECRET)
    monkeypatch.setenv("KAI_HOST_JWT_ISSUER", "agnes-test")
    monkeypatch.setenv("KAI_HOST_JWT_AUDIENCE", "kai-agent-test")
    monkeypatch.setenv("KAI_TENANT_ID", "tenant-test")


def _decode_segment(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def _claims(token: str) -> dict:
    return _decode_segment(token.split(".")[1])


def _verify(token: str, secret: str = KAI_SECRET) -> bool:
    """Verify the HS256 signature exactly as the engine's ``jose`` verify does:
    over the raw ``header.payload`` ASCII bytes."""
    header_b64, payload_b64, signature_b64 = token.split(".")
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
    padding = "=" * (-len(signature_b64) % 4)
    return hmac.compare_digest(expected, base64.urlsafe_b64decode(signature_b64 + padding))


def _mint_session(seeded_app):
    client = seeded_app["client"]
    resp = client.post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# session minting
# ---------------------------------------------------------------------------


def test_unconfigured_instance_serves_no_kai_routes(seeded_app, monkeypatch):
    """No shared secret ⇒ the integration is off, not half-on."""
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)
    resp = seeded_app["client"].post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "kai_integration_not_configured"


def test_session_requires_authentication(seeded_app, kai_env):
    assert seeded_app["client"].post("/api/kai/sessions").status_code in (401, 403)


def test_session_token_carries_every_claim_the_engine_requires(seeded_app, kai_env):
    """The engine's ``claimsSchema`` fails the token — not degrades it — when
    any identity claim is missing."""
    body = _mint_session(seeded_app)
    claims = _claims(body["token"])

    assert claims["sub"] == "analyst@test.com"
    assert claims["tenant"] == "tenant-test"
    assert claims["scope_id"] == body["chat_id"]
    assert claims["downstream_credential"]
    assert claims["read_only"] is False
    assert claims["iss"] == "agnes-test"
    assert claims["aud"] == "kai-agent-test"


def test_session_token_signature_verifies_under_the_shared_secret(seeded_app, kai_env):
    token = _mint_session(seeded_app)["token"]
    assert _verify(token) is True
    assert _verify(token, secret="a-different-secret-of-sufficient-length") is False


def test_session_token_expiry_is_inside_the_engines_24h_ceiling(seeded_app, kai_env):
    """The engine rejects a token whose ``exp`` is more than 24 h out, and one
    with no ``exp`` at all."""
    claims = _claims(_mint_session(seeded_app)["token"])
    now = int(time.time())
    assert claims["exp"] > now
    assert claims["exp"] - now <= 24 * 60 * 60


def test_session_identity_is_never_taken_from_the_request_body(seeded_app, kai_env):
    """A body-supplied ``sub`` must not become the principal — otherwise this
    is an impersonation endpoint."""
    resp = seeded_app["client"].post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"sub": "admin@test.com", "tenant": "evil"},
    )
    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["token"])
    assert claims["sub"] == "analyst@test.com"
    assert claims["tenant"] == "tenant-test"


def test_session_creates_a_chat_session_row_keyed_by_the_returned_id(seeded_app, kai_env):
    """Agnes owns the chat id; the engine accepts it as ``body.id`` so both
    sides key off one value with no cross-database join."""
    from src.repositories import chat_session_repo

    body = _mint_session(seeded_app)
    session = chat_session_repo().get_session(body["chat_id"])
    assert session is not None
    assert session.user_email == "analyst@test.com"


def test_chat_id_is_a_uuid_because_the_engine_stores_it_as_one(seeded_app, kai_env):
    """Not cosmetic. The engine validates ``body.id`` as a UUID and persists it
    in a Postgres ``uuid`` column, so the repo's default ``chat_<hex>`` id is
    rejected with ``Invalid UUID`` before a turn ever starts — verified against
    a live engine, and the reason `create_session` grew a `session_id` param.
    """
    import uuid as uuid_mod

    body = _mint_session(seeded_app)
    assert not body["chat_id"].startswith("chat_")
    # raises if the id is not a well-formed UUID
    assert str(uuid_mod.UUID(body["chat_id"])) == body["chat_id"]
    # the claim the engine reads as its scope key must carry the same value
    assert _claims(body["token"])["scope_id"] == body["chat_id"]


# ---------------------------------------------------------------------------
# per-turn tickets
# ---------------------------------------------------------------------------


def test_tickets_requires_a_credential(seeded_app, kai_env):
    assert seeded_app["client"].post("/api/kai/tickets").status_code == 401


def test_tickets_rejects_an_unknown_credential(seeded_app, kai_env):
    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": "Bearer not-a-real-credential"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_or_expired_kai_credential"


def test_tickets_returns_the_payload_shape_the_engine_validates(seeded_app, kai_env):
    """``llm`` is mandatory and every value must be a non-empty string, or the
    engine rejects the payload and fails the turn before any prompt reaches
    the sandbox."""
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert isinstance(payload.get("llm"), str) and payload["llm"]
    assert all(isinstance(v, str) and v for v in payload.values())


def test_llm_ticket_authenticates_the_main_scoped_broker_route(seeded_app, kai_env):
    """The engine's ``llm`` scope must land on a ticket the existing LLM broker
    route accepts — that route is the whole reason no new LLM route is needed."""
    from src.repositories import ticket_repo

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    tickets = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()

    resolved = ticket_repo().resolve(tickets["llm"])
    assert resolved is not None
    assert resolved["scope"] == "main"


def test_mcp_ticket_is_omitted_unless_the_instance_brokers_mcp(seeded_app, kai_env, monkeypatch):
    """The engine treats ``mcp`` as optional host data — an instance with no
    MCP upstream simply omits the scope."""
    monkeypatch.delenv("KAI_BROKER_MCP_ENABLED", raising=False)
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    payload = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    assert "mcp" not in payload

    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    payload = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    assert isinstance(payload.get("mcp"), str) and payload["mcp"]


def test_a_broker_ticket_cannot_mint_more_tickets(seeded_app, kai_env):
    """Scope check on the credential route: if a sandbox got hold of one turn's
    ticket, it must not be able to refresh itself indefinitely."""
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    llm_ticket = (
        seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()["llm"]
    )

    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {llm_ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "kai_credential_scope_mismatch"


def test_reminting_retires_the_previous_turns_egress_tickets(seeded_app, kai_env):
    """One live set per chat, so a stale turn's ticket cannot be replayed."""
    from src.repositories import ticket_repo

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    client = seeded_app["client"]
    first = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    second = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()

    assert first["llm"] != second["llm"]
    assert ticket_repo().resolve(first["llm"]) is None
    assert ticket_repo().resolve(second["llm"]) is not None


def test_reminting_does_not_invalidate_the_session_credential(seeded_app, kai_env):
    """The engine has no way to be handed a replacement credential — its
    ticket-response schema is ``{llm, mcp}`` and it keeps using the one baked
    into the session JWT. A scope-blind revoke here 401s every later turn.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    client = seeded_app["client"]

    for turn in range(3):
        resp = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
        assert resp.status_code == 200, f"turn {turn + 1} lost the credential: {resp.text}"


def test_tickets_are_scoped_to_their_own_session(seeded_app, kai_env):
    """Two chats must not share an egress ticket set."""
    from src.repositories import ticket_repo

    client = seeded_app["client"]
    first = _mint_session(seeded_app)
    second = _mint_session(seeded_app)
    assert first["chat_id"] != second["chat_id"]

    first_tickets = client.post(
        "/api/kai/tickets",
        headers={"Authorization": f"Bearer {_claims(first['token'])['downstream_credential']}"},
    ).json()
    client.post(
        "/api/kai/tickets",
        headers={"Authorization": f"Bearer {_claims(second['token'])['downstream_credential']}"},
    )

    # minting for the second chat must not have retired the first chat's set
    still_live = ticket_repo().resolve(first_tickets["llm"])
    assert still_live is not None
    assert still_live["session_id"] == first["chat_id"]
