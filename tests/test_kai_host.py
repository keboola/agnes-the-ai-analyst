"""Host-side wiring for the embedded ``kai-agent`` turn engine (app/api/kai.py).

Covers the contract the engine's ``jwt`` host adapter actually enforces — the
claim set it requires, the ``exp`` ceiling it rejects past, and the ticket
payload shape it validates — plus the two things that make the credential
model safe: a broker ticket cannot mint more tickets, and rotating a turn's
egress tickets must not destroy the session credential the engine still holds.
"""

from __future__ import annotations

import base64
import io
import hashlib
import hmac
import json
import time
from unittest import mock

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


def test_mcp_toggle_honours_the_shared_falsey_vocabulary(seeded_app, kai_env, monkeypatch):
    """`KAI_BROKER_MCP_ENABLED=false` must take the tool surface AWAY.

    An inline `os.environ.get(...).strip()` truthiness test reads every
    non-blank value as on, so the operator who spells the disable explicitly —
    `false`, `0`, `off` — hands the sandbox the very scope they meant to
    withhold. Routed through `feature_enabled` (`docs/feature-flags.md`), which
    is the only reason those spellings mean what they say. Found by Devin
    Review on this PR.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]

    def _mcp_scope_issued() -> bool:
        payload = (
            seeded_app["client"]
            .post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
            .json()
        )
        return "mcp" in payload

    for off in ("false", "False", "0", "off", "no", ""):
        monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", off)
        assert not _mcp_scope_issued(), f"{off!r} must leave the mcp scope unissued"

    for on in ("true", "1", "yes", "on"):
        monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", on)
        assert _mcp_scope_issued(), f"{on!r} must issue the mcp scope"


def test_mcp_proxy_never_hands_the_sandbox_undecoded_bytes(seeded_app, kai_env):
    """The forwarded reply must be readable, not gzip under a stripped header.

    `_MCP_DROP_RESPONSE_HEADERS` strips `content-encoding`, which is only sound
    if the body we forward is decoded. Two independent halves, because either
    alone still breaks: the request must ask for `identity` (the sandbox's own
    `accept-encoding` is dropped, but `build_request` re-adds httpx's default
    `gzip, deflate`, so an upstream may compress with nothing downstream
    asking), and the body must stream through `aiter_bytes()` — `aiter_raw()`
    emits the undecoded body, i.e. gzip bytes labelled as plain text. Found by
    Devin Review on this PR.
    """
    import inspect

    from app.api import kai as kai_mod

    # Comments are stripped before the negative assertion: this function's own
    # comment NAMES `aiter_raw` to explain why it is wrong, and a test that
    # cannot tell prose from code would fail on the very explanation.
    source = inspect.getsource(kai_mod.kai_mcp)
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert '"Accept-Encoding"] = "identity"' in code, "must ask the upstream not to compress"
    assert "aiter_bytes()" in code, "must forward the DECODED body"
    assert "aiter_raw()" not in code, "aiter_raw would emit gzip under a stripped content-encoding"
    # And the header it strips is still stripped — the pairing is what matters.
    assert "content-encoding" in kai_mod._MCP_DROP_RESPONSE_HEADERS


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


# ---------------------------------------------------------------------------
# MCP passthrough
# ---------------------------------------------------------------------------


def _mcp_ticket(seeded_app):
    """A ticket in the mcp scope, as the engine's relay would hold."""
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    return ticket_repo().mint(body["chat_id"], "mcp"), body


def test_mcp_requires_an_mcp_scoped_ticket(seeded_app, kai_env):
    """An llm ticket must not reach the tool surface — scope split is the
    whole point of minting two."""
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    llm_ticket = ticket_repo().mint(body["chat_id"], "main")

    resp = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": f"Bearer {llm_ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "ticket_scope_mismatch"


def test_mcp_rejects_an_unknown_ticket(seeded_app, kai_env):
    resp = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_mcp_token_refuses_the_two_narrowed_session_kinds(seeded_app, kai_env, monkeypatch):
    """A co-session guest and a scope-limited agent must NOT get owner authority.

    `/api/kai/mcp` accepts any `mcp`-scoped broker ticket, and the native chat
    runner mints one for every chat sandbox — so without this the two session
    kinds `_mint_identity_jwt` deliberately narrows (live participant
    intersection; owner-grants ∩ agent-scope) could borrow the stored owner's
    whole tool surface through this route. This token is a registered bearer
    with a baked subject and cannot carry either intersection, so it refuses,
    the same call `_ticket_owner_for_git` makes. Found by Devin Review.
    """
    from app.api import kai as kai_mod
    from fastapi import HTTPException

    body = _mint_session(seeded_app)
    chat_id = body["chat_id"]
    real = kai_mod.chat_session_repo().get_session(chat_id)

    # Baseline FIRST, unpatched: a plain solo session still mints, so a failure
    # below is the narrowing and not a broken fixture. (`monkeypatch.undo()`
    # cannot serve here — it would also undo the fixtures' own patches.)
    assert isinstance(kai_mod._mint_mcp_access_token(chat_id), str)

    class _Sess:
        def __init__(self, **kw):
            self.user_email = real.user_email
            self.is_co_session = False
            self.agent_id = None
            self.__dict__.update(kw)

    def _with(session, agent=None):
        kai_mod._mcp_token_cache.clear()  # never serve a pre-narrowing cache hit
        monkeypatch.setattr(kai_mod, "chat_session_repo", lambda: type("R", (), {"get_session": staticmethod(lambda _sid: session)})())
        if agent is not None:
            import src.repositories as repos
            monkeypatch.setattr(repos, "agents_repo", lambda: type("A", (), {"get_by_id": staticmethod(lambda _i: agent)})())
        with pytest.raises(HTTPException) as e:
            kai_mod._mint_mcp_access_token(chat_id)
        return e.value

    # 1. shared conversation -> refused, not resolved to its stored owner
    exc = _with(_Sess(is_co_session=True))
    assert (exc.status_code, exc.detail) == (403, "mcp_not_available_to_co_session")

    # 2. scope-limited agent -> refused
    exc = _with(_Sess(agent_id="ag_1"), agent={"id": "ag_1", "deleted_at": None, "scope_mode": "selected"})
    assert (exc.status_code, exc.detail) == (403, "mcp_not_available_to_scoped_agent")

    # 3. deleted agent -> fails CLOSED rather than falling through to the owner
    exc = _with(_Sess(agent_id="ag_2"), agent={"id": "ag_2", "deleted_at": "2026-01-01T00:00:00Z"})
    assert (exc.status_code, exc.detail) == (401, "ticket_agent_not_found")



def test_mcp_token_is_bound_to_the_mcp_resource_server(seeded_app, kai_env):
    """Parity with the genuine OAuth code exchange, which stores the requested
    `resource`. A stored `None` authenticates today, so this is about not
    breaking later: an SDK that begins enforcing RFC 8707 audience binding
    would 401 brokered engine traffic while real connectors kept working.
    Found by Devin Review."""
    from app.api.kai import _mint_mcp_access_token
    from app.auth.public_url import mcp_issuer_url
    from src.repositories import oauth_clients_repo

    token = _mint_mcp_access_token(_mint_session(seeded_app)["chat_id"])
    row = oauth_clients_repo().get_access_token(token)
    assert row is not None
    assert row["resource"] == mcp_issuer_url()


def test_mcp_mints_a_registered_access_token_for_the_ticket_identity(seeded_app, kai_env):
    """The mounted MCP app resolves a bearer against `oauth_access_tokens`, so
    a bare session JWT would not authenticate — the token has to be minted AND
    registered, carrying scope='mcp-oauth' so resolve_token_to_user stamps the
    agent-surface posture."""
    import base64
    import json as _json

    from app.api.kai import _mint_mcp_access_token
    from src.repositories import oauth_clients_repo

    body = _mint_session(seeded_app)
    token = _mint_mcp_access_token(body["chat_id"])

    row = oauth_clients_repo().get_access_token(token)
    assert row is not None, "token must be resolvable by the MCP verifier"
    assert row["client_id"] == "kai-agent-broker"

    payload = token.split(".")[1]
    claims = _json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["scope"] == "mcp-oauth"
    assert claims["chat_session_id"] == body["chat_id"]


def test_mcp_access_token_is_reused_across_calls_for_one_session(seeded_app, kai_env):
    """Minting is a DB write and an MCP turn makes many JSON-RPC calls."""
    from app.api.kai import _mint_mcp_access_token

    body = _mint_session(seeded_app)
    assert _mint_mcp_access_token(body["chat_id"]) == _mint_mcp_access_token(body["chat_id"])


def test_mcp_token_is_scoped_to_its_own_session(seeded_app, kai_env):
    from app.api.kai import _mint_mcp_access_token

    first = _mint_session(seeded_app)
    second = _mint_session(seeded_app)
    assert _mint_mcp_access_token(first["chat_id"]) != _mint_mcp_access_token(second["chat_id"])


# ---------------------------------------------------------------------------
# workspace payload
# ---------------------------------------------------------------------------


def test_workspace_requires_the_session_credential(seeded_app, kai_env):
    assert seeded_app["client"].get("/api/kai/workspace").status_code == 401


def test_workspace_rejects_a_broker_ticket(seeded_app, kai_env):
    """Only the engine's server fetches this; a sandbox-held ticket must not."""
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    ticket = ticket_repo().mint(body["chat_id"], "mcp")
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "kai_credential_scope_mismatch"


def test_workspace_serves_a_gzipped_tar_of_the_template(seeded_app, kai_env):
    """The engine rejects anything but 200-with-body or 204, and rejects the
    whole payload on an absolute path, a `..` segment or a non-file member."""
    import tarfile as _tarfile

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200
    assert resp.content, "an empty 200 is a contract violation — 204 means 'none'"

    with _tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        members = tar.getmembers()

    names = [m.name for m in members]
    assert members, "archive must not be empty"
    assert all(m.isfile() for m in members), "non-file members fail the engine's validation"
    assert not any(n.startswith("/") for n in names), "absolute paths are rejected"
    assert not any(".." in n.split("/") for n in names), "dot segments are rejected"

    # The three things that make this Agnes's harness rather than bare Claude Code.
    assert "CLAUDE.md" in names
    assert any(n.startswith(".claude/skills/") for n in names)
    assert ".claude/hooks/pre_tool_use.py" in names

    # Sandbox-image build assets describe how to BUILD a sandbox, not how to
    # work in one — they have no place in another engine's workspace.
    assert not any(n.startswith("e2b-template/") for n in names)
    assert not any(n.startswith("docker-sandbox/") for n in names)


def test_workspace_archive_is_byte_stable_across_a_second_boundary(seeded_app, kai_env):
    """The engine re-fetches on every SDK respawn; a payload differing only by
    timestamp would churn the sandbox tree for nothing.

    The second boundary is the point. Pinning only the tar members' mtime
    leaves the gzip *container* header carrying `time.time()`, so two packs
    within the same second compare equal and the bug hides — which is exactly
    how it shipped. Freeze the clock a second apart instead of sleeping.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}

    with mock.patch("time.time", return_value=1_700_000_000.0):
        first = seeded_app["client"].get("/api/kai/workspace", headers=headers).content
    with mock.patch("time.time", return_value=1_700_000_042.0):
        second = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    assert first == second
    # Belt and braces: the gzip header's 4-byte MTIME field must be zeroed.
    assert first[4:8] == b"\x00\x00\x00\x00"


@pytest.mark.parametrize(
    ("method", "path", "auth"),
    [
        # /sessions is user-authenticated; the rest carry the engine's own
        # credential. Each route must be probed with the token it actually
        # accepts, or it 401s before ever reaching the kill switch.
        ("post", "/api/kai/sessions", "user"),
        ("post", "/api/kai/tickets", "credential"),
        ("post", "/api/kai/mcp", "credential"),
        ("get", "/api/kai/workspace", "credential"),
    ],
)
def test_every_route_honours_the_kill_switch(seeded_app, kai_env, monkeypatch, method, path, auth):
    """Unsetting the secret must stop the integration *now*.

    Checking it only on session creation left every already-issued credential
    minting egress tickets and reaching tools for the rest of its 12 h life,
    which is not what an operator disabling the integration expects — and the
    engine's verify path has no revocation to fall back on.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    bearer = seeded_app["analyst_token"] if auth == "user" else credential
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)

    call = getattr(seeded_app["client"], method)
    resp = call(path, headers={"Authorization": f"Bearer {bearer}"})

    assert resp.status_code == 503, f"{method.upper()} {path} still served with the secret unset"
    assert resp.json()["detail"] == "kai_integration_not_configured"


def test_mcp_token_cache_drops_expired_entries(seeded_app, kai_env):
    """The cache must not become an unbounded leak.

    An entry is dead the moment its token expires — the next mint replaces it —
    but a chat that never comes back would otherwise keep a full JWT resident
    for the process lifetime, so memory grew with every engine chat ever served.
    """
    from app.api import kai as kai_mod

    body = _mint_session(seeded_app)
    kai_mod._mcp_token_cache["ghost-chat-that-never-returns"] = ("stale.jwt.value", 1)

    kai_mod._mint_mcp_access_token(body["chat_id"])

    assert "ghost-chat-that-never-returns" not in kai_mod._mcp_token_cache
    assert body["chat_id"] in kai_mod._mcp_token_cache


def test_mcp_proxy_does_not_use_an_in_process_asgi_dispatch(seeded_app, kai_env):
    """`httpx.ASGITransport` looks like it streams and does not.

    It runs the ASGI app to completion, accumulating every body chunk, then
    yields one joined blob — so `stream=True` buys nothing and `httpx.Timeout`
    is inert (no network layer to apply it to). Buffering is not merely slow
    here: the engine's relay bounds time-to-headers, so a tool slower than that
    bound would die on a relay 502. Pin the transport choice so a future
    "simplification" back to ASGITransport has to argue with this test.
    """
    import inspect

    from app.api import kai as kai_mod

    source = inspect.getsource(kai_mod.kai_mcp)
    assert "ASGITransport" not in source
    assert "_mcp_internal_base()" in source


def test_workspace_ignores_a_deregistered_template_clone(seeded_app, kai_env, monkeypatch, tmp_path):
    """The YAML is the source of truth, not the filesystem.

    An admin can unset the template URL while the clone lingers on disk;
    shipping that stale tree to the engine's sandbox is exactly what
    `is_configured()` exists to prevent. Probing `.is_dir()` alone would.
    """
    from app.api import kai as kai_mod
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR

    clone = tmp_path / "iwt"
    (clone / "workspace").mkdir(parents=True)
    (clone / "workspace" / "CLAUDE.md").write_text("# leftover clone", encoding="utf-8")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "get_initial_workspace_dir", lambda: clone)

    # De-registered: the clone exists but no URL is configured.
    monkeypatch.setattr(iw, "is_configured", lambda: False)
    assert kai_mod._workspace_template_root() == BUNDLED_TEMPLATE_DIR

    # Registered: the same clone is now the caller's workspace.
    monkeypatch.setattr(iw, "is_configured", lambda: True)
    assert kai_mod._workspace_template_root() == clone / "workspace"
