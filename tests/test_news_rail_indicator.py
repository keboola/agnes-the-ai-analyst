"""Tests for the /news unread-dot indicator (#1053).

Three layers:

1. ``_compute_has_unread_news`` — pure logic, monkeypatched dependencies
   (no app/DB bootstrap needed).
2. The dot markup (``.app-user-menu-news-dot``) actually renders in the
   user-dropdown chrome — both topnav (``_app_header.html``) and rail
   (``_app_rail.html``) — when ``has_unread_news`` is true, and is absent
   otherwise.
3. The ``/news`` page's mark-seen script — present only when ``can_chat``
   and a published version exist, carrying the right version number.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Layer 1 — _compute_has_unread_news, isolated
# ---------------------------------------------------------------------------


class _FakeNewsRepo:
    def __init__(self, published):
        self._published = published

    def get_current_published(self):
        return self._published


class _FakeJourneyRepo:
    def __init__(self, seen_version):
        self._seen_version = seen_version

    def get(self, user_id):
        return {"news_seen_version": self._seen_version}


@pytest.fixture
def router():
    import app.web.router as router_module

    return router_module


def test_has_unread_news_true_when_published_ahead_of_seen(monkeypatch, router):
    monkeypatch.setattr(router, "_compute_can_chat", lambda request, user: True)
    monkeypatch.setattr(router, "news_template_repo", lambda: _FakeNewsRepo({"version": 5}))
    monkeypatch.setattr(router, "user_journey_repo", lambda: _FakeJourneyRepo(2))
    assert router._compute_has_unread_news(None, {"id": "u1"}) is True


def test_has_unread_news_false_when_nothing_published(monkeypatch, router):
    monkeypatch.setattr(router, "_compute_can_chat", lambda request, user: True)
    monkeypatch.setattr(router, "news_template_repo", lambda: _FakeNewsRepo(None))
    monkeypatch.setattr(router, "user_journey_repo", lambda: _FakeJourneyRepo(0))
    assert router._compute_has_unread_news(None, {"id": "u1"}) is False


def test_has_unread_news_false_when_seen_equals_published(monkeypatch, router):
    monkeypatch.setattr(router, "_compute_can_chat", lambda request, user: True)
    monkeypatch.setattr(router, "news_template_repo", lambda: _FakeNewsRepo({"version": 5}))
    monkeypatch.setattr(router, "user_journey_repo", lambda: _FakeJourneyRepo(5))
    assert router._compute_has_unread_news(None, {"id": "u1"}) is False


def test_has_unread_news_false_when_cannot_chat_even_if_unread(monkeypatch, router):
    """The mark-seen write rides the chat-gated PUT /api/chat/journey — a
    caller without chat access could never clear the dot, so it must never
    light for them even when a newer version is genuinely unseen."""
    monkeypatch.setattr(router, "_compute_can_chat", lambda request, user: False)
    monkeypatch.setattr(router, "news_template_repo", lambda: _FakeNewsRepo({"version": 5}))
    monkeypatch.setattr(router, "user_journey_repo", lambda: _FakeJourneyRepo(0))
    assert router._compute_has_unread_news(None, {"id": "u1"}) is False


def test_has_unread_news_false_when_no_user(router):
    assert router._compute_has_unread_news(None, None) is False


# ---------------------------------------------------------------------------
# Layers 2 & 3 — full app, real templates
# ---------------------------------------------------------------------------


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app()
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


def _enable_chat(web_client, monkeypatch):
    """Make can_chat true: chat enabled AND an explicit CHAT grant (admin
    god-mode does NOT short-circuit has_explicit_grant, so patch it) — same
    recipe as tests/test_ui_layout_theme.py's TestRailChatHistory."""
    from types import SimpleNamespace

    import app.auth.access as access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)


def _publish_news(version_intro: str = "hello") -> None:
    from src.db import get_system_db
    from src.repositories.news_template import NewsTemplateRepository

    conn = get_system_db()
    repo = NewsTemplateRepository(conn)
    repo.save_draft(intro=f"<p>{version_intro}</p>", content="<p>body</p>", by="admin@test.com")
    repo.publish_draft(by="admin@test.com")
    conn.close()


def _set_seen_version(user_id: str, version: int) -> None:
    from src.repositories import user_journey_repo

    user_journey_repo().update(user_id, news_seen_version=version)


class TestDotMarkup:
    def test_dot_absent_by_default(self, web_client, admin_cookie):
        """No published news + no chat grant → the dot never renders."""
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "app-user-menu-news-dot" not in resp.text

    def test_dot_renders_on_topnav_when_unread(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _publish_news()
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "app-user-menu-news-dot" in resp.text
        assert 'href="/news"' in resp.text

    def test_dot_absent_on_topnav_once_seen(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _publish_news()
        _set_seen_version("admin1", 1)  # first published version is 1
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "app-user-menu-news-dot" not in resp.text

    def test_dot_renders_on_rail_when_unread(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _enable_chat(web_client, monkeypatch)
        _publish_news()
        # Non-chat rail page — /dashboard redirects to /chat under rail when
        # can_chat is true, so probe a page that stays put (mirrors
        # TestRailChatHistory.test_rail_renders_history_section).
        resp = web_client.get("/stack", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "app-user-menu-news-dot" in resp.text

    def test_dot_absent_when_chat_access_missing_even_if_unread(self, web_client, admin_cookie):
        """Without a chat grant, _compute_can_chat is false, so the dot must
        stay dark even though a version is genuinely unseen — the mark-seen
        write is chat-gated, so lighting it here would be undismissable."""
        _publish_news()
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "app-user-menu-news-dot" not in resp.text


class TestMarkSeenScript:
    def test_script_present_when_can_chat_and_news_published(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _publish_news()
        resp = web_client.get("/news", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "/api/chat/journey" in resp.text
        assert "news_seen_version" in resp.text
        assert "news_seen_version: 1" in resp.text or '"news_seen_version": 1' in resp.text

    def test_script_absent_when_no_chat_access(self, web_client, admin_cookie):
        _publish_news()
        resp = web_client.get("/news", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "/api/chat/journey" not in resp.text

    def test_script_absent_when_no_news_published(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        resp = web_client.get("/news", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "/api/chat/journey" not in resp.text
