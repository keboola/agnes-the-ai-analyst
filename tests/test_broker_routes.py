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
