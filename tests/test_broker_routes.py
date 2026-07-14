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
