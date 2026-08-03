"""GET /marketplace/guide/{curated,flea} — submission flow guides.

Both routes are authed (`get_current_user` dependency). The curated guide
documents the Named Curator handoff and has a fast-path callout pointing
at the flea self-service guide; the flea guide documents the /store/new
upload flow. Together with the action-row CTA on /marketplace?tab=curated,
this trio is the discovery surface for "how do I get my plugin published".
"""

from __future__ import annotations

import tempfile
import uuid

import pytest

from tests._template_assertions import assert_element, ElementNotFound


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email="u@example.com"):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    return uid, create_access_token(user_id=uid, email=email)


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_marketplace_browse_tab_cta_text(fresh_db):
    """The action-row CTA on the Browse shelf reads 'Submit a skill or plugin'
    (renamed from 'Submit a plugin' so skills aren't an afterthought) and links
    to the Skill Builder with the coach-mark armed
    (`/skills?spotlight=new-skill`) — the guide describes a process, /skills
    starts one. Empty-state fallback in JS uses the same string and the same
    href so both surfaces stay in sync.

    v104: the CTA moved from `data-actions-for="curated"` to `"browse"` when the
    Curated / Flea shelves collapsed into one Publisher-faceted shelf. `?tab=
    curated` still resolves — old links redirect onto Browse with the
    Organization facet pre-applied — so this asserts against the merged tab."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/marketplace", cookies={"access_token": sess}).text

    # Action-row anchor — primary discovery path. Renders via
    # `ds.button(variant='secondary', href=..., attrs='data-actions-for=...')`
    # which emits href before class; assertion is order-agnostic.
    import re

    cta_match = re.search(
        r'<a\b[^>]*\bclass="btn btn-secondary[^"]*"[^>]*>'
        r"\s*Submit a skill or plugin\s*</a>",
        body,
    )
    assert cta_match, "action-row CTA anchor (.btn .btn-secondary) missing or text changed"
    cta_html = cta_match.group(0)
    assert 'data-actions-for="browse"' in cta_html
    assert 'href="/skills?spotlight=new-skill"' in cta_html
    # Empty-state JS innerHTML — same string and same destination, no drift.
    assert "Submit a skill or plugin →" in body
    assert '<a href="/skills?spotlight=new-skill">Submit a skill or plugin →</a>' in body
    # Old wording must be gone — guards against partial rename.
    assert ">Submit a plugin<" not in body


def test_marketplace_guide_curated_page(fresh_db):
    """Curated guide page documents the Named Curator handoff. Three-step
    flow (find → handoff → publish) lives inside `.guide-steps`. The
    fast-path callout points users at the flea guide as the lighter
    review-bar alternative; the primary CTA at the bottom does the same
    so users who skim past the callout still see the escape hatch."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    resp = _client().get("/marketplace/guide/curated", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text

    # Title carries the new 'skill or plugin' wording.
    assert "Submit a skill or plugin to Curated Marketplace" in body
    # Lede surfaces the gatekeeping concept.
    assert "Named Curators" in body
    # Three-step ordered list under `.guide-steps`.
    assert_element(body, "ol", class_="guide-steps")
    assert "Find a Curator" in body
    assert "Hand off your skill or plugin" in body
    assert "Curator publishes" in body
    # Fast-path callout exists and the CTA inside it points at the flea
    # guide (NOT /store/new directly — we want users to read the flea
    # context before they upload).
    assert_element(body, "div", class_="guide-fastpath")
    assert 'href="/marketplace/guide/flea"' in body
    # Primary CTA at the bottom also surfaces the flea path. Renders
    # via `ds.button(variant='primary', href='/marketplace/guide/flea')`
    # which emits href before class.
    assert_element(body, "a", class_="btn btn-primary", href="/marketplace/guide/flea")


def test_marketplace_guide_flea_page(fresh_db):
    """Flea guide documents the /store/new self-service flow. Four-step
    body (package → upload → automated review → published) replaces the
    earlier stub. Primary CTA goes directly to /store/new since users
    landing on the flea guide have already chosen the self-service path."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    resp = _client().get("/marketplace/guide/flea", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text

    assert "Upload to Flea Market" in body
    # Four-step ordered list (no fast-path callout on flea — it IS the
    # fast path, the curated guide is what links here).
    assert_element(body, "ol", class_="guide-steps")
    assert "Package what you" in body
    assert "Upload via the form" in body
    assert "Automated review" in body
    assert "Published" in body
    # Primary CTA goes straight to /store/new (flea is one click away
    # from being live, no intermediate handoff). Renders via
    # `ds.button(variant='primary', href='/store/new')` which emits
    # href before class.
    assert_element(body, "a", class_="btn btn-primary", href="/store/new")
    # No fast-path callout here — sanity check the asymmetry sticks.
    with pytest.raises(ElementNotFound):
        assert_element(body, "div", class_="guide-fastpath")


def test_marketplace_shelves_collapsed_into_publisher_facet(fresh_db):
    """v104: one Browse shelf, and provenance is a facet.

    The Curated / Flea tabs and a Publisher facet asked the same question with
    different answers — a user filtering Publisher=Organization while standing
    on the Flea tab would see an empty grid even though organization-published
    authored items exist. This pins the retirement so the shelves cannot quietly
    return alongside the facet.
    """
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get("/marketplace", cookies={"access_token": sess}).text

    # One discovery shelf + My Stack.
    assert 'data-tab="browse"' in body
    assert 'data-tab="my"' in body
    assert 'data-tab="curated"' not in body
    assert 'data-tab="flea"' not in body

    # The facet that replaced them.
    assert 'data-publisher="organization"' in body
    assert 'data-publisher="me"' in body
    assert 'data-publisher="other_users"' in body

    # The scope checkboxes were the other half of the same question.
    assert 'id="mp-scope-curated"' not in body
    assert 'id="mp-scope-flea"' not in body

    # Verification is opt-in per instance and OFF by default, so its row must
    # not render here — an instance with no reviewer shows no such vocabulary.
    assert 'data-verification="verified"' not in body
    assert "Not verified" not in body
