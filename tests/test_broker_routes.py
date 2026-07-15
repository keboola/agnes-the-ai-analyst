"""App-tier tests for the chat sandbox secret broker routes (Task 6 of the
2026-07-14 chat-sandbox-secret-broker plan).

Exercises ``app/api/broker.py``: ticket-scope enforcement, admin-path
rejection, and that the in-process ASGI replay produces identical results
to a direct call under the same resolved identity (live RBAC, no broker
privilege of its own).

Uses ``asyncio.run`` rather than ``@pytest.mark.asyncio`` — this repo does
not depend on pytest-asyncio (see tests/test_cache_warmup.py for the same
pattern).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.auth.jwt import create_access_token
from app.chat.types import Surface
from src.db import get_system_db
from src.repositories import chat_session_repo, ticket_repo
from src.repositories.users import UserRepository


@pytest.fixture
def broker_session(e2e_env):
    """A seeded user + chat session, standing in for a spawned sandbox."""
    conn = get_system_db()
    UserRepository(conn).create(id="broker_user1", email="broker@test.com", name="Broker User")
    conn.close()

    session = chat_session_repo().create_session(user_email="broker@test.com", surface=Surface.WEB)
    jwt_token = create_access_token(user_id="broker_user1", email="broker@test.com")
    return {"session_id": session.id, "jwt": jwt_token}


@pytest.fixture
def broker_app(e2e_env):
    from app.main import create_app

    return create_app()


def test_expired_ticket_401(broker_app):
    tok = ticket_repo().mint("chat_x", "main", ttl_seconds=-1)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )

    r = asyncio.run(_run())
    assert r.status_code == 401


def test_mcp_ticket_cannot_use_main_route(broker_app):
    tok = ticket_repo().mint("chat_y", "mcp")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )

    r = asyncio.run(_run())
    assert r.status_code == 401  # scope mismatch


def test_admin_mutation_rejected(broker_app, broker_session):
    tok = ticket_repo().mint(broker_session["session_id"], "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": "/api/admin/grant", "body": {}},
            )

    r = asyncio.run(_run())
    assert r.status_code in (403, 401)


def test_agnes_api_replay_uses_live_rbac(broker_app, broker_session):
    tok = ticket_repo().mint(broker_session["session_id"], "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            replayed = await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )
            direct = await c.get(
                "/api/me/home-stats",
                headers={"Authorization": f"Bearer {broker_session['jwt']}"},
            )
            return replayed, direct

    replayed, direct = asyncio.run(_run())
    assert replayed.status_code == 200, replayed.text
    assert direct.status_code == 200, direct.text
    assert replayed.json() == direct.json()


def test_admin_route_off_admin_prefix_rejected(broker_app, e2e_env):
    """A require_admin route that is NOT under /api/admin/ (here /api/sync/trigger)
    must be rejected by the broker's route-introspection gate — even when the
    resolved identity is itself an admin (so downstream require_admin would pass).
    Proves the fix over the old path-prefix check, which missed such routes (§11).
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    UserRepository(conn).create(id="broker_admin1", email="broker_admin@test.com", name="Broker Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("broker_admin1", admin_gid, source="system_seed")
    conn.close()
    session = chat_session_repo().create_session(user_email="broker_admin@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": "/api/sync/trigger", "body": {}},
            )

    r = asyncio.run(_run())
    assert r.status_code == 403, r.text
    # the broker's OWN gate fired (not downstream require_admin), proven by the detail
    assert r.json().get("detail") == "admin_mutations_require_interactive_auth"


def test_anthropic_route_accepts_subpath(broker_app):
    """The Anthropic proxy must match sub-paths — the SDK appends
    ``/v1/messages`` to its base URL, so the real request arrives at
    ``/api/broker/anthropic/v1/messages``. An exact-path-only route 404s every
    real model call (Devin review on #849)."""
    from fastapi.routing import APIRoute

    paths = {r.path for r in broker_app.routes if isinstance(r, APIRoute)}
    assert "/api/broker/anthropic/{subpath:path}" in paths, sorted(p for p in paths if "anthropic" in p)
    assert "/api/broker/anthropic" in paths  # bare path still served


def test_anthropic_proxy_uses_generous_read_timeout(broker_app, monkeypatch):
    """Regression: httpx's 5s default read timeout aborts every real LLM
    completion with ReadTimeout, leaving the sandbox agent an empty response.
    The proxy must build its client with a generous read timeout.

    Captures the ``timeout`` passed to ``httpx.AsyncClient`` on the anthropic
    leg and asserts the read budget is well above the 5s default.
    """
    import app.api.broker as broker_mod

    captured: dict = {}
    real_cls = httpx.AsyncClient

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"{}"

    class _FakeClient:
        """Delegates to the real client for the test harness's own
        transport-backed client; fakes only the broker's outbound anthropic
        client (constructed with ``timeout=`` and no transport)."""

        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None
            if self._real is None:
                captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def request(self, *a, **k):
            if self._real:
                return await self._real.request(*a, **k)
            return _FakeResp()

        def __getattr__(self, name):
            # Proxy any other method (e.g. .post) to the real delegate.
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeClient)
    tok = ticket_repo().mint("chat_ay", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    # Well above httpx's 5s default read timeout.
    assert t.read is not None and t.read >= 60.0, t


class _HeaderCapturingClient:
    """Fake httpx.AsyncClient that delegates the harness's transport-backed
    client to the real one and captures the headers the broker's outbound
    anthropic client sends. Shared by the auth-mode tests below."""

    _captured: dict = {}
    _real_cls = httpx.AsyncClient

    def __init__(self, *a, **k):
        self._real = self._real_cls(*a, **k) if "transport" in k else None

    async def __aenter__(self):
        return await self._real.__aenter__() if self._real else self

    async def __aexit__(self, *a):
        return await self._real.__aexit__(*a) if self._real else False

    async def request(self, *a, **k):
        if self._real:
            return await self._real.request(*a, **k)
        _HeaderCapturingClient._captured = dict(k.get("headers") or {})

        class _R:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = b"{}"

        return _R()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lower_keys(d: dict) -> dict:
    return {k.lower(): v for k, v in d.items()}


def test_anthropic_proxy_api_key_mode_injects_x_api_key(broker_app, monkeypatch):
    """AC-1: default (api_key) mode is unchanged — inject x-api-key, no Authorization."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _HeaderCapturingClient)
    tok = ticket_repo().mint("chat_apikey", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    h = _lower_keys(_HeaderCapturingClient._captured)
    assert h.get("x-api-key") == "sk-ant-static-KEY"
    assert "authorization" not in h


def test_anthropic_proxy_workload_identity_injects_bearer_not_key(broker_app, monkeypatch):
    """AC-2: workload_identity mode injects a federated Bearer token + the oauth
    beta header, and sends NO static x-api-key."""
    import types

    import app.api.broker as broker_mod
    import app.auth.wif as wif

    # Flip the app into workload_identity mode (ChatConfig is frozen; a duck-typed
    # stand-in with the one attribute the broker reads is enough).
    broker_app.state.chat_config = types.SimpleNamespace(llm_auth="workload_identity")
    monkeypatch.setattr(wif, "get_federated_access_token", lambda: "sk-ant-oat01-FED")
    # A static key is present but must be ignored in this mode.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-SHOULD-NOT-BE-USED")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _HeaderCapturingClient)
    tok = ticket_repo().mint("chat_wif", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}", "anthropic-version": "2023-06-01"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    h = _lower_keys(_HeaderCapturingClient._captured)
    assert h.get("authorization") == "Bearer sk-ant-oat01-FED"
    assert "x-api-key" not in h
    assert "oauth-2025-04-20" in h.get("anthropic-beta", "")
    # sanity: the sandbox's SDK header survived
    assert h.get("anthropic-version") == "2023-06-01"


def test_normalize_broker_path_rejects_smuggling():
    """Unit: the path canonicalizer returns the EXACT URL the ASGI dispatch
    routes on (percent-decoded, dot-segments collapsed) and rejects authority
    smuggling (§11, RBAC review #849)."""
    from fastapi import HTTPException

    from app.api.broker import _normalize_broker_path

    # accepted, query preserved; .path is the real dispatch target
    assert _normalize_broker_path("/api/me/home-stats").path == "/api/me/home-stats"
    got = _normalize_broker_path("/api/x?a=1&b=2")
    assert got.path == "/api/x" and got.query == b"a=1&b=2"

    # canonicalization: interior percent-encoding and dot-segment traversal
    # resolve to the SAME path the gate must guard (both = /api/sync/trigger),
    # so the gate can no longer be fooled into reading them as non-admin.
    assert _normalize_broker_path("/api/sync/tri%67ger").path == "/api/sync/trigger"
    assert _normalize_broker_path("/api/foo/../sync/trigger").path == "/api/sync/trigger"

    for bad in (
        "http://evil.example/api/sync/trigger",
        "https://evil.example/api/sync/trigger",
        "//evil.example/api/sync/trigger",
        "http://broker-replay/api/sync/trigger",
        "\\\\evil.example\\api\\sync\\trigger",
        "/%2f%2fevil/api/sync/trigger",
        "relative/no/leading/slash",
        "",
    ):
        with pytest.raises(HTTPException) as ei:
            _normalize_broker_path(bad)
        assert ei.value.status_code == 400, bad
        assert ei.value.detail == "broker_path_must_be_local", bad


def test_admin_route_path_smuggling_rejected(broker_app, e2e_env):
    """A smuggled absolute-URL / protocol-relative / encoded path that the
    ASGI transport would still dispatch to an admin route (/api/sync/trigger)
    must NOT bypass the broker's admin gate — proven with an admin-owner
    ticket, so downstream require_admin would otherwise pass (RBAC review #849).
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    UserRepository(conn).create(id="broker_admin_sm", email="broker_admin_sm@test.com", name="Broker Admin SM")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("broker_admin_sm", admin_gid, source="system_seed")
    conn.close()
    session = chat_session_repo().create_session(user_email="broker_admin_sm@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    smuggled = [
        "http://evil.example/api/sync/trigger",
        "//evil.example/api/sync/trigger",
        "http://broker-replay/api/sync/trigger",
        "\\\\evil.example\\api\\sync\\trigger",
        "/%2f%2fevil/api/sync/trigger",
        # canonicalization-divergence vectors (RBAC review #849 round 2): the
        # ASGI transport decodes %67 -> 'g' and collapses '..', so these reach
        # /api/sync/trigger unless the gate guards the SAME canonical path.
        "/api/sync/tri%67ger",
        "/api/foo/../sync/trigger",
        "/api/sync/%2e%2e/sync/trigger",
    ]

    async def _run(p):
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": p, "body": {}},
            )

    for p in smuggled:
        r = asyncio.run(_run(p))
        # Security invariant: the admin-gated handler must NEVER execute under a
        # smuggled path. Acceptable outcomes: 400 (rejected as authority
        # smuggling), 403 (canonical path guarded by the admin gate), or a
        # 404/405 misroute — never a 200 that actually triggers the sync.
        assert r.status_code != 200, f"{p} -> 200 (admin handler executed): {r.text}"
        body = r.json()
        assert body.get("status") != "triggered", f"{p} REACHED the admin handler: {r.text}"


def test_cosession_ticket_mints_cosession_jwt(broker_app, e2e_env):
    """A co-session's broker replay must mint a co_session JWT (live
    grant-intersection), not resolve to the single stored owner (§11)."""
    from app.api.broker import _mint_identity_jwt
    from app.auth.jwt import verify_token
    from src.db import get_system_db

    conn = get_system_db()
    UserRepository(conn).create(id="co_owner1", email="co_owner@test.com", name="Co Owner")
    conn.close()
    solo = chat_session_repo().create_session(user_email="co_owner@test.com", surface=Surface.WEB)
    co = chat_session_repo().create_session(user_email="co_owner@test.com", surface=Surface.WEB)
    # flip the co-session flag directly (a co-session is otherwise created via fork)
    conn = get_system_db()
    conn.execute("UPDATE chat_sessions SET is_co_session = TRUE WHERE id = ?", [co.id])
    conn.close()

    solo_payload = verify_token(_mint_identity_jwt(solo.id))
    co_payload = verify_token(_mint_identity_jwt(co.id))
    assert solo_payload.get("typ") == "session"
    assert co_payload.get("typ") == "co_session"
    # the co-session JWT carries no real user identity (synthetic sub), only the session
    assert co_payload.get("sub") == f"session:{co.id}"
    assert co_payload.get("chat_session_id") == co.id
    # BOTH broker mints must carry scope="chat" so the per-session BigQuery
    # scan-budget stash (`_stash_chat_session_id_from_token`) fires — it ignores
    # the chat_session_id claim without that scope, silently disabling the cap
    # for brokered chat traffic (security review on #849).
    assert solo_payload.get("scope") == "chat"
    assert co_payload.get("scope") == "chat"
