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


def test_marketplace_curated_tab_cta_text(fresh_db):
    """The action-row CTA on /marketplace?tab=curated reads
    'Submit a skill or plugin' (renamed from 'Submit a plugin' so skills
    aren't an afterthought) and links to the curated guide. Empty-state
    fallback in JS uses the same string so both surfaces stay in sync."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()
    body = _client().get(
        "/marketplace?tab=curated", cookies={"access_token": sess}
    ).text

    # Action-row anchor — primary discovery path. Assertion is order-
    # agnostic now that the anchor is rendered via `ds.button` (which
    # emits href before the class attribute); the intent is unchanged:
    # an <a> exists carrying the canonical .btn .btn-secondary classes,
    # the data-actions-for="curated" hook the tab-switcher JS reads,
    # the curated-guide href, AND the renamed CTA text.
    import re
    cta_match = re.search(
        r'<a\b[^>]*\bclass="btn btn-secondary[^"]*"[^>]*>'
        r'\s*Submit a skill or plugin\s*</a>',
        body,
    )
    assert cta_match, "action-row CTA anchor (.btn .btn-secondary) missing or text changed"
    cta_html = cta_match.group(0)
    assert 'data-actions-for="curated"' in cta_html
    assert 'href="/marketplace/guide/curated"' in cta_html
    # Empty-state JS innerHTML — same string, no drift.
    assert "Submit a skill or plugin →" in body
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
    resp = _client().get(
        "/marketplace/guide/curated", cookies={"access_token": sess}
    )
    assert resp.status_code == 200
    body = resp.text

    # Title carries the new 'skill or plugin' wording.
    assert "Submit a skill or plugin to Curated Marketplace" in body
    # Lede surfaces the gatekeeping concept.
    assert "Named Curators" in body
    # Three-step ordered list under `.guide-steps`.
    assert '<ol class="guide-steps">' in body
    assert "Find a Curator" in body
    assert "Hand off your skill or plugin" in body
    assert "Curator publishes" in body
    # Fast-path callout exists and the CTA inside it points at the flea
    # guide (NOT /store/new directly — we want users to read the flea
    # context before they upload).
    assert '<div class="guide-fastpath">' in body
    assert 'href="/marketplace/guide/flea"' in body
    # Primary CTA at the bottom also surfaces the flea path. Renders
    # via `ds.button(variant='primary', href='/marketplace/guide/flea')`
    # which emits href before class; the assertion checks both attributes
    # without locking attribute order.
    assert '<a href="/marketplace/guide/flea" class="btn btn-primary"' in body


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
    resp = _client().get(
        "/marketplace/guide/flea", cookies={"access_token": sess}
    )
    assert resp.status_code == 200
    body = resp.text

    assert "Upload to Flea Market" in body
    # Four-step ordered list (no fast-path callout on flea — it IS the
    # fast path, the curated guide is what links here).
    assert '<ol class="guide-steps">' in body
    assert "Package what you" in body
    assert "Upload via the form" in body
    assert "Automated review" in body
    assert "Published" in body
    # Primary CTA goes straight to /store/new (flea is one click away
    # from being live, no intermediate handoff). Renders via
    # `ds.button(variant='primary', href='/store/new')` which emits
    # href before class.
    assert '<a href="/store/new" class="btn btn-primary"' in body
    # No fast-path callout here — sanity check the asymmetry sticks.
    assert '<div class="guide-fastpath">' not in body
