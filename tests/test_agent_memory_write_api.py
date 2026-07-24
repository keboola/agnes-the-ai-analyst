"""`POST /api/v1/sessions/{id}/memories` — the "remember" tool (V1c Task 4).

Covers write-mode branching (off/propose/auto), the size/rate/pending
guards, and the C2 binding correction: a broker-minted `chat_session_id`
claim must match the path `{id}` or the request is `403 session_mismatch`
— regardless of whether the two sessions share the same owner. Also covers
`app.chat.agent_profile._context_skill` advertising the remember tool only
when `memory_write_mode != "off"`.

Session rows are real (`chat_session_repo()`), same posture as
`tests/test_agent_sessions_api.py` — `require_session_principal` exercises
its actual DB-backed ownership check, not a mock. This endpoint never
touches the chat manager, so no `FakeManager` is needed here.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _chat_claim_token(user_id: str, email: str, chat_session_id: str) -> str:
    """Mint the same shape of JWT `app.api.broker._mint_identity_jwt` mints
    for a solo session: `scope=chat` + `chat_session_id`. This is what
    `app.auth.dependencies._stash_chat_session_id_from_token` parks on
    `request.state.chat_session_id` — the in-sandbox-agent call shape."""
    return create_access_token(
        user_id=user_id,
        email=email,
        extra_claims={"scope": "chat", "chat_session_id": chat_session_id},
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.chat.types import Surface
    from app.main import create_app
    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import (
        agents_repo,
        chat_session_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    user_group_members_repo().add_member("other1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    def _make_agent(slug: str, mode: str) -> str:
        agent_id = str(uuid.uuid4())
        agents_repo().create(
            id=agent_id,
            owner_user_id="owner1",
            name=slug,
            slug=slug,
            memory_write_mode=mode,
        )
        return agent_id

    off_agent = _make_agent("off-agent", "off")
    propose_agent = _make_agent("propose-agent", "propose")
    auto_agent = _make_agent("auto-agent", "auto")

    def _make_session(agent_id: str, user_email: str = "owner@test.com") -> str:
        session = chat_session_repo().create_session(user_email=user_email, surface=Surface.API, agent_id=agent_id)
        return session.id

    off_session = _make_session(off_agent)
    propose_session = _make_session(propose_agent)
    auto_session = _make_session(auto_agent)

    client = TestClient(create_app())
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "off_agent": off_agent,
        "propose_agent": propose_agent,
        "auto_agent": auto_agent,
        "off_session": off_session,
        "propose_session": propose_session,
        "auto_session": auto_session,
        "make_agent": _make_agent,
        "make_session": _make_session,
    }


# ---------------------------------------------------------------------------
# memory_write_mode branching
# ---------------------------------------------------------------------------


def test_off_mode_returns_403(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "note"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "memory_writes_disabled"

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().list_for_agent(env["off_agent"]) == []


def test_propose_mode_returns_201_pending_and_not_active(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "remember this"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["id"]

    from src.repositories import agent_memories_repo

    repo = agent_memories_repo()
    pending = repo.list_for_agent(env["propose_agent"], status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == body["id"]
    assert pending[0]["content"] == "remember this"
    assert pending[0]["activated_at"] is None

    assert repo.list_active(env["propose_agent"]) == []


def test_auto_mode_returns_201_active_and_in_list_active(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "remember this too"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "active"
    assert body["id"]

    from src.repositories import agent_memories_repo

    repo = agent_memories_repo()
    active = repo.list_active(env["auto_agent"])
    assert len(active) == 1
    assert active[0]["id"] == body["id"]
    assert active[0]["activated_at"] is not None


# ---------------------------------------------------------------------------
# Guards (all modes)
# ---------------------------------------------------------------------------


def test_empty_content_returns_422(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "   "},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422


def test_oversize_content_returns_413(env):
    # `request.app.state.chat_config` is what the endpoint reads its limits
    # from (`app.api.agent_memory._chat_config`), falling back to
    # `ChatConfig()` defaults when unset — CHAT-INIT only populates it on a
    # real app *startup* (lifespan), which a bare `TestClient(create_app())`
    # (no `with` block) never triggers, same as the other chat-config tests
    # (e.g. `tests/test_chat_readiness.py`) set it explicitly.
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_max_chars=10)

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "this content is definitely over ten characters"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "memory_too_large"


def test_over_hourly_rate_limit_returns_429(env):
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_writes_per_hour=2)

    for _ in range(2):
        r = env["client"].post(
            f"/api/v1/sessions/{env['auto_session']}/memories",
            json={"content": "note"},
            headers=_auth(env["owner_token"]),
        )
        assert r.status_code == 201

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "one too many"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "memory_rate_limited"


def test_over_pending_cap_returns_429(env):
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_max_pending=2)
    # Rate limit default (20/hr) is well above the pending cap (2) here, so
    # the pending-cap guard is the one that trips.

    for _ in range(2):
        r = env["client"].post(
            f"/api/v1/sessions/{env['propose_session']}/memories",
            json={"content": "note"},
            headers=_auth(env["owner_token"]),
        )
        assert r.status_code == 201
        assert r.json()["status"] == "pending"

    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "one too many"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "memory_pending_full"


# ---------------------------------------------------------------------------
# C2: bind the write to the CALLING session, never the path {id}
# ---------------------------------------------------------------------------


def test_c2_broker_claim_for_different_agent_same_owner_returns_403_and_leaves_target_unchanged(env):
    """Agent A (auto) and agent B (off) both belong to owner1. A broker-
    minted token identifies the CALLING session as A's session
    (`chat_session_id=auto_session`), but the request path targets B's
    session. `require_session_principal` alone would authorize this (same
    owner) — the C2 check must still reject it, and B's notebook must stay
    untouched."""
    token = _chat_claim_token("owner1", "owner@test.com", env["auto_session"])

    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "poison B's notebook"},
        headers=_auth(token),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_mismatch"

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().list_for_agent(env["off_agent"]) == []
    # A's own notebook is untouched too — the call never targeted it either.
    assert agent_memories_repo().list_for_agent(env["auto_agent"]) == []


def test_c2_matching_broker_claim_succeeds(env):
    """Sanity counterpart: when the claim DOES match the path id, the call
    proceeds normally (uses the calling/path agent's own memory_write_mode)."""
    token = _chat_claim_token("owner1", "owner@test.com", env["auto_session"])

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "legit note"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "active"


def test_c2_no_claim_direct_owner_call_uses_path_session_normally(env):
    """No broker claim (plain interactive token) — the path id IS the
    calling session, already ownership-verified by require_session_principal;
    the C2 check must not interfere."""
    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "note via owner token"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "pending"


def test_cross_owner_direct_call_returns_404(env):
    """Baseline: an entirely different owner gets 404 (require_session_principal's
    own ownership check), before C2 even needs to run."""
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "not yours"},
        headers=_auth(env["other_token"]),
    )
    assert r.status_code == 404


def test_unknown_session_returns_404(env):
    r = env["client"].post(
        "/api/v1/sessions/does-not-exist/memories",
        json={"content": "note"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# _context_skill advertises the remember tool only when mode != off
# ---------------------------------------------------------------------------


def test_context_skill_advertises_remember_tool_when_mode_propose():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "propose"}
    assert "Remember" in _context_skill(row)
    assert "/api/v1/sessions/{session_id}/memories" in _context_skill(row)


def test_context_skill_advertises_remember_tool_when_mode_auto():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "auto"}
    assert "Remember" in _context_skill(row)


def test_context_skill_omits_remember_tool_when_mode_off():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "off"}
    body = _context_skill(row)
    assert "Remember" not in body
    assert "/api/v1/sessions/{session_id}/memories" not in body
