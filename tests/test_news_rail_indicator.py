"""Tests for #1053 — rail "What's new" nav entry + unread indicator.

Covers the pure `_compute_has_unread_news` logic, the static template/CSS
contract (row placement + accent token), and a full TestClient round trip
through the rail layout.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# _compute_has_unread_news — pure logic
# ---------------------------------------------------------------------------


def test_both_context_builders_expose_has_unread_news():
    """`_build_context` and `_chrome_ctx` must both set `has_unread_news` —
    same class of gap as the `can_chat` / `instance_brand_short` omissions
    that dropped chrome elements on whichever builder's pages skipped them."""
    from app.web import router as _router

    for builder in (_router._build_context, _router._chrome_ctx):
        src = inspect.getsource(builder)
        assert '"has_unread_news"' in src, f"{builder.__name__} must supply has_unread_news"


def test_compute_has_unread_news_true_when_newer_version_and_can_chat(monkeypatch):
    from app.web import router as r

    monkeypatch.setattr(r, "news_template_repo", lambda: _Repo(get_current_published=lambda: {"version": 3}))
    monkeypatch.setattr(r, "user_journey_repo", lambda: _Repo(get=lambda uid: {"news_seen_version": 1}))
    assert r._compute_has_unread_news({"id": "u1"}, True) is True


def test_compute_has_unread_news_false_without_can_chat(monkeypatch):
    """Gated on can_chat: the mark-seen write rides the chat-gated journey
    endpoint, so a caller who can't write there must never see a dot they
    have no way to clear. Must short-circuit before touching either repo."""
    from app.web import router as r

    def _boom():
        raise AssertionError("must not query repos when can_chat is False")

    monkeypatch.setattr(r, "news_template_repo", _boom)
    assert r._compute_has_unread_news({"id": "u1"}, False) is False


def test_compute_has_unread_news_false_when_no_published_news(monkeypatch):
    from app.web import router as r

    monkeypatch.setattr(r, "news_template_repo", lambda: _Repo(get_current_published=lambda: None))
    assert r._compute_has_unread_news({"id": "u1"}, True) is False


def test_compute_has_unread_news_false_when_already_seen(monkeypatch):
    from app.web import router as r

    monkeypatch.setattr(r, "news_template_repo", lambda: _Repo(get_current_published=lambda: {"version": 3}))
    monkeypatch.setattr(r, "user_journey_repo", lambda: _Repo(get=lambda uid: {"news_seen_version": 3}))
    assert r._compute_has_unread_news({"id": "u1"}, True) is False


def test_compute_has_unread_news_false_without_user():
    from app.web import router as r

    assert r._compute_has_unread_news(None, True) is False


class _Repo:
    def __init__(self, **methods):
        for name, fn in methods.items():
            setattr(self, name, fn)


# ---------------------------------------------------------------------------
# Static template / CSS contract
# ---------------------------------------------------------------------------

RAIL_TEMPLATE = Path("app/web/templates/_app_rail.html")
RAIL_CSS = Path("app/web/static/css/rail.css")


def test_rail_whats_new_row_present_and_outside_can_chat_gate():
    """The row must sit between "How it works" and the can_chat-gated
    onboarding card — i.e. visible to every authenticated user, not just
    chat-access ones (the page itself has nothing to do with chat)."""
    html = RAIL_TEMPLATE.read_text(encoding="utf-8")
    assert 'href="/news"' in html
    assert "rail-news-dot" in html
    how_it_works_idx = html.index("How {{ instance_brand_short }} works")
    news_idx = html.index('href="/news"')
    getstarted_idx = html.index('id="railGetStarted"')
    assert how_it_works_idx < news_idx < getstarted_idx


def test_rail_news_dot_gated_on_has_unread_news():
    html = RAIL_TEMPLATE.read_text(encoding="utf-8")
    assert "{% if has_unread_news %}" in html


def test_rail_news_dot_uses_info_accent_not_wayfinding_primary():
    """The accent (`--rail-active-bg` / `--ds-primary`) is reserved for
    "you are here" — see the COLOUR RULE atop rail.css. Unread is a
    different meaning and must not borrow it."""
    css = RAIL_CSS.read_text(encoding="utf-8")
    block = css.split(".rail-news-dot {", 1)[1].split("}", 1)[0]
    assert "--ds-accent-info-ink" in block
    assert "--ds-primary" not in block
    assert "--rail-active-bg" not in block


# ---------------------------------------------------------------------------
# Full round trip (TestClient, rail layout)
#
# Uses a minimal app (web router + chat API router only, `get_current_user`
# overridden) rather than `app.main.app` — mirrors test_chat_web_route.py's
# `_make_app`. The real app's startup lifespan (scheduler, marketplace sync,
# …) has nothing to do with this feature and is too heavy to boot per test.
# ---------------------------------------------------------------------------

TEST_USER = {"id": "u1", "email": "u1@example.com", "is_admin": False}


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        yield tmp


def _make_app(monkeypatch, *, chat_access: bool):
    from types import SimpleNamespace

    from fastapi import FastAPI

    from app.api.chat import router as chat_router
    from app.auth.dependencies import get_current_user
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)
    app.include_router(chat_router)
    app.state.chat_config = SimpleNamespace(enabled=chat_access)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER

    if chat_access:
        import app.auth.access as _access

        monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
        monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: True)

    return app


def _publish(conn):
    from src.repositories.news_template import NewsTemplateRepository

    repo = NewsTemplateRepository(conn)
    repo.save_draft(intro="intro", content="content", by="alice@x")
    return repo.publish_draft(by="alice@x")


def test_rail_shows_whats_new_row_for_every_authenticated_user(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    c = TestClient(_make_app(monkeypatch, chat_access=False))
    r = c.get("/library")
    assert r.status_code == 200
    assert 'href="/news"' in r.text


def test_rail_unread_dot_absent_without_chat_access(fresh_db, monkeypatch):
    """A caller with no chat grant still reaches /news from the row, but
    never sees a dot it has no way to clear (mark-seen writes through the
    chat-gated journey endpoint)."""
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    _publish(conn)
    conn.close()
    close_system_db()

    from fastapi.testclient import TestClient

    c = TestClient(_make_app(monkeypatch, chat_access=False))
    r = c.get("/library")
    assert r.status_code == 200
    assert "rail-news-dot" not in r.text


def test_rail_unread_dot_lit_then_cleared_by_marking_seen(fresh_db, monkeypatch):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    published = _publish(conn)
    conn.close()
    close_system_db()

    from fastapi.testclient import TestClient

    c = TestClient(_make_app(monkeypatch, chat_access=True))

    r = c.get("/library")
    assert r.status_code == 200
    assert "rail-news-dot" in r.text

    # /news renders the mark-seen script with the published version baked in.
    r = c.get("/news")
    assert r.status_code == 200
    assert f"news_seen_version: {published['version']}" in r.text

    # Simulate the browser firing that script's fetch() call.
    r = c.put("/api/chat/journey", json={"news_seen_version": published["version"]})
    assert r.status_code == 200

    r = c.get("/library")
    assert r.status_code == 200
    assert "rail-news-dot" not in r.text
