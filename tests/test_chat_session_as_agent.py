"""Running a named agent from web chat (AGT-6).

The headline complaint was "agents can't be used — their tab only configures
them". The runtime was never missing: ``ChatManager.create_session(agent_id=)``
and ``build_profile`` are surface-agnostic and are exactly what
``POST /api/v1/agents/{slug}/sessions`` already used. Web chat was simply never
wired to them, so every browser session ran as the caller's default agent
whatever they had built.

Two things worth pinning beyond "it works":

* **Ownership.** Naming another user's slug must not hand the caller a persona
  assembled from grants that are not theirs. 404, not 403 — a distinct status
  would confirm that someone else's agent exists.
* **The default path is untouched.** No ``agent_slug`` still resolves to the
  caller's default, because every existing session and its attribution depend
  on that.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _chat_granted():
    """Chat is an RBAC resource (``require_resource_access(CHAT, "chat")``).

    Granted to Everyone here rather than stubbed, so these run through the same
    gate a browser does — the point of the feature is the real request path.
    """
    import uuid

    from src.db import get_system_db

    conn = get_system_db()
    grp = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()
    if grp:
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'chat', 'chat', 'available', CURRENT_TIMESTAMP, 'test') "
            "ON CONFLICT DO NOTHING",
            [str(uuid.uuid4()), grp[0]],
        )
    conn.close()


def _make_agent(seeded_app, token: str, name: str) -> dict:
    resp = seeded_app["client"].post("/api/agents", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _chat_or_skip(seeded_app, **body):
    """POST a session, skipping the test when chat is disabled in this env."""
    resp = seeded_app["client"].post("/api/chat/sessions", json=body, headers=_auth(seeded_app["analyst_token"]))
    if resp.status_code in (403, 503):
        pytest.skip(f"chat unavailable in this environment ({resp.status_code})")
    return resp


class TestResolverOwnership:
    """``_resolve_agent_id`` direct — the security-relevant half.

    Tested at the function rather than only through the endpoint because chat
    is RBAC-gated and not enabled for every environment, and a skipped test
    proves nothing about who may run whose agent.
    """

    def test_my_own_slug_resolves_to_my_agent(self, seeded_app):
        from app.api.chat import _resolve_agent_id

        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Mine To Run")
        got = _resolve_agent_id(agent["slug"], {"id": "analyst1", "email": "analyst@example.com"})
        assert got == agent["id"]

    def test_another_users_slug_does_not_resolve(self, seeded_app):
        from fastapi import HTTPException

        from app.api.chat import _resolve_agent_id

        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Not Yours")
        with pytest.raises(HTTPException) as exc:
            _resolve_agent_id(theirs["slug"], {"id": "analyst1", "email": "a@example.com"})
        assert exc.value.status_code == 404, "a foreign slug resolved, or leaked its existence with a 403"

    def test_an_unknown_slug_does_not_resolve(self):
        from fastapi import HTTPException

        from app.api.chat import _resolve_agent_id

        with pytest.raises(HTTPException) as exc:
            _resolve_agent_id("no-such-agent", {"id": "analyst1", "email": "a@example.com"})
        assert exc.value.status_code == 404

    def test_no_slug_falls_back_to_the_default_agent(self):
        from app.api.chat import _resolve_agent_id
        from src.repositories import agents_repo

        got = _resolve_agent_id(None, {"id": "analyst1", "email": "a@example.com"})
        assert got == agents_repo().get_or_create_default("analyst1")["id"]


class TestSpawnAsNamedAgent:
    def test_a_session_can_run_as_one_of_my_agents(self, seeded_app):
        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Release Writer")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=agent["slug"])
        assert resp.status_code == 201, resp.text

        from src.repositories import chat_sessions_repo

        row = chat_sessions_repo().get(resp.json()["id"])
        assert row is not None
        assert row.get("agent_id") == agent["id"], (
            "the session was attributed to the default agent, so the persona the caller built is not the one running"
        )

    def test_no_slug_still_uses_the_default_agent(self, seeded_app):
        """The pre-existing path, unchanged — every old session depends on it."""
        resp = _chat_or_skip(seeded_app, surface="web")
        assert resp.status_code == 201, resp.text

        from src.repositories import agents_repo, chat_sessions_repo

        row = chat_sessions_repo().get(resp.json()["id"])
        default_id = agents_repo().get_or_create_default("analyst1")["id"]
        assert row.get("agent_id") == default_id


class TestOwnership:
    def test_an_unknown_slug_is_refused(self, seeded_app):
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug="no-such-agent")
        assert resp.status_code == 404

    def test_another_users_agent_is_refused(self, seeded_app):
        """Owner-scoped lookup: the admin's agent must not resolve for the analyst."""
        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Admins Agent")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=theirs["slug"])
        assert resp.status_code == 404, (
            "a foreign slug resolved — the caller would get a persona built on grants that are not theirs"
        )


class TestTheWiringIsPresent:
    """The page half — a card that cannot be clicked is still unusable."""

    @pytest.fixture(autouse=True)
    def _rail(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")

    def test_the_agents_page_offers_a_chat_action(self, seeded_app):
        resp = seeded_app["client"].get("/agents", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "/chat?agent=" in resp.text, "no way to reach an agent from its card"

    def test_the_work_in_progress_caveat_is_gone(self, seeded_app):
        """It apologised for exactly the thing that now works."""
        resp = seeded_app["client"].get("/agents", headers=_auth(seeded_app["analyst_token"]))
        assert "Work in progress" not in resp.text
        assert "Actually running them" not in resp.text
