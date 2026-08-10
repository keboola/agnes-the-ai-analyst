"""A builder-created agent gets a real slug once it is named.

The `/agents` builder creates the row on "New agent" — before the user has
typed anything — so `create_agent` falls back to the literal slug `"agent"`
(`_auto_slug(name or "agent")`). `update_agent` then wrote every field the
builder sent EXCEPT the slug, so the agent kept `"agent"` forever while its
display name said something else entirely.

That slug is not cosmetic: it is the address in
`POST /api/v1/agents/{slug}/responses` and `agnes chat <slug>`. An agent
shown as "Revenue Analyst" answered on `/agent`, the second such agent on
`/agent-2`, and the builder shows the slug nowhere — so the only way to
find it was to enumerate the API.

Renaming re-derives the slug only while BOTH hold: the agent is still a
draft, and its slug is still the untouched placeholder. A slug that ever
reflected a real name is an address someone may already have wired up, so
it stays put.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, token: str, **fields) -> dict:
    r = seeded_app["client"].post("/api/agents", json=fields, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _patch(seeded_app, agent_id: str, token: str, **fields) -> dict:
    r = seeded_app["client"].patch(f"/api/agents/{agent_id}", json=fields, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_unnamed_draft_gets_a_real_slug_when_named(seeded_app):
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)  # exactly what "New agent" posts
    assert agent["slug"] == "agent", "precondition: the placeholder slug"

    renamed = _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    assert renamed["slug"] == "revenue-analyst"


def test_renaming_again_keeps_the_first_real_slug(seeded_app):
    """Once the slug reflects a name, it is an address — it stops moving."""
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    first = _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    assert first["slug"] == "revenue-analyst"

    second = _patch(seeded_app, agent["id"], tok, name="Something Else")
    assert second["slug"] == "revenue-analyst"


def test_named_at_creation_is_untouched_by_rename(seeded_app):
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok, name="Finance Bot")
    assert agent["slug"] == "finance-bot"

    renamed = _patch(seeded_app, agent["id"], tok, name="Renamed Bot")
    assert renamed["slug"] == "finance-bot"


def test_ready_agent_keeps_its_placeholder_slug(seeded_app):
    """A published agent's address must not move under its callers.

    Even the placeholder is load-bearing once the agent is ready: someone
    may have scripted `agnes chat agent` against it.
    """
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    _patch(seeded_app, agent["id"], tok, status="ready")

    renamed = _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    assert renamed["slug"] == "agent"


def test_second_unnamed_draft_named_the_same_gets_a_unique_slug(seeded_app):
    """Re-derivation goes through the same per-owner uniqueness search."""
    tok = seeded_app["admin_token"]
    a = _patch(seeded_app, _create(seeded_app, tok)["id"], tok, name="Revenue Analyst")
    b = _patch(seeded_app, _create(seeded_app, tok)["id"], tok, name="Revenue Analyst")
    assert a["slug"] == "revenue-analyst"
    assert b["slug"] != a["slug"]
    assert b["slug"].startswith("revenue-analyst")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_rename_does_not_move_the_slug(seeded_app, blank):
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    renamed = _patch(seeded_app, agent["id"], tok, name=blank)
    assert renamed["slug"] == "agent"


def test_unsluggable_rename_does_not_produce_a_duplicate_placeholder(seeded_app):
    """A name with no alphanumerics re-derives to the placeholder base.

    It must not collide with the agent's own current slug and get suffixed
    to `agent-2` — that would rename the address for no user-visible reason.
    """
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    renamed = _patch(seeded_app, agent["id"], tok, name="!!!")
    assert renamed["slug"] == "agent"
