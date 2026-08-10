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

The slug follows the name while BOTH hold: the agent is still a draft, and
its slug still tracks that name. Marking it ready is what publishes the
address, so that freezes it; a slug set by any other means has stopped
being a function of the name, so a rename must not relocate it.

Re-deriving on every draft rename, rather than once off the placeholder,
is forced by how the builder saves: it PATCHes each field edit behind a
debounce, so a pause mid-word flushes a PARTIAL name. A once-only rule
latched onto that fragment and gave the finished agent the address `rev` —
worse than the placeholder it replaced, and uncorrectable from the UI.
Caught by Devin Review on #1225, along with the uniqueness search being
scoped to the caller rather than the agent's owner.
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


def test_slug_follows_the_name_through_a_keystroke_debounce(seeded_app):
    """The builder PATCHes while you type, so partial names arrive first.

    `/agents` saves every field edit behind a short debounce, so pausing
    mid-word flushes a PATCH carrying e.g. "Rev". A rule that re-derives
    once and then freezes would hand the finished agent the address `rev`
    — worse than the placeholder it replaced, and uncorrectable from the
    UI. While the agent is a draft whose slug still tracks its name, the
    slug keeps following.
    """
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    assert _patch(seeded_app, agent["id"], tok, name="Rev")["slug"] == "rev"
    assert _patch(seeded_app, agent["id"], tok, name="Revenue An")["slug"] == "revenue-an"
    assert _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")["slug"] == "revenue-analyst"


def test_named_at_creation_still_follows_a_draft_rename(seeded_app):
    """Same rule regardless of where the first name came from."""
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok, name="Finance Bot")
    assert agent["slug"] == "finance-bot"

    renamed = _patch(seeded_app, agent["id"], tok, name="Renamed Bot")
    assert renamed["slug"] == "renamed-bot"


def test_marking_ready_freezes_the_address(seeded_app):
    """Publishing is what makes the slug an address other things may hold."""
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok)
    _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    _patch(seeded_app, agent["id"], tok, status="ready")

    renamed = _patch(seeded_app, agent["id"], tok, name="Something Else")
    assert renamed["slug"] == "revenue-analyst"


def test_a_slug_that_stopped_tracking_its_name_is_left_alone(seeded_app):
    """Only a slug still derived from the current name may move.

    Guards the rule against a slug set by any other means (a future
    user-chosen slug, a migration): it no longer matches the name, so the
    rename must not silently relocate it.
    """
    from src.repositories import agents_repo

    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok, name="Finance Bot")
    agents_repo().update(agent["id"], slug="hand-picked")

    renamed = _patch(seeded_app, agent["id"], tok, name="Renamed Bot")
    assert renamed["slug"] == "hand-picked"


def test_admin_renaming_a_foreign_draft_checks_the_owners_slugs(seeded_app):
    """Uniqueness is per owner — searching the admin's own agents is wrong.

    `_writable` lets an admin PATCH someone else's agent. Scoping the
    free-slug search to the admin reports a candidate free while the real
    owner already holds it, and the UPDATE then hits the
    `(owner_user_id, slug)` UNIQUE as an unhandled 500.
    """
    from src.repositories import agents_repo

    tok = seeded_app["admin_token"]
    owner = "other-user-1"
    agents_repo().create(id="agt_owned_taken", owner_user_id=owner, name="Revenue Analyst", slug="revenue-analyst")
    agents_repo().create(id="agt_owned_draft", owner_user_id=owner, name="", slug="agent")

    renamed = _patch(seeded_app, "agt_owned_draft", tok, name="Revenue Analyst")
    assert renamed["slug"] != "revenue-analyst", "collided with the owner's existing agent"
    assert renamed["slug"].startswith("revenue-analyst")


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
