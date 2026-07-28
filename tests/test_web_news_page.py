"""Web tests for /news + the /home news section.

Verifies the route gating + that /home only renders the news block when a
published version with a non-empty intro exists.
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


def _user_session(conn, email: str = "u@example.com"):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    return uid, create_access_token(user_id=uid, email=email)


def _client_with_session(conn):
    from fastapi.testclient import TestClient
    from app.main import app
    _, token = _user_session(conn)
    c = TestClient(app)
    c.cookies.set("access_token", token)
    return c


def _publish(conn, *, intro: str, content: str):
    from src.repositories.news_template import NewsTemplateRepository
    repo = NewsTemplateRepository(conn)
    repo.save_draft(intro=intro, content=content, by="alice@x")
    repo.publish_draft(by="alice@x")


def test_news_page_empty_state(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _client_with_session(conn)
    finally:
        conn.close()
        close_system_db()
    r = c.get("/news")
    assert r.status_code == 200
    assert "No news yet" in r.text


def test_news_page_renders_published_content(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        _publish(conn, intro="<p>Big release intro</p>", content="<h1>Hello</h1><div class=\"callout\">ok</div>")
        c = _client_with_session(conn)
    finally:
        conn.close()
        close_system_db()
    r = c.get("/news")
    assert r.status_code == 200
    assert "Big release intro" in r.text
    assert "Hello" in r.text
    assert 'class="callout"' in r.text


def test_news_page_redirects_anon(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.get("/news", follow_redirects=False)
    # Either 302 to login OR 401 — both are acceptable for the auth gate.
    assert r.status_code in (302, 303, 401)


def test_home_renders_news_section_when_intro_present(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        _publish(conn, intro="<p>Bottom-of-home perex</p>", content="<p>full body</p>")
        c = _client_with_session(conn)
    finally:
        conn.close()
        close_system_db()
    r = c.get("/home")
    assert r.status_code == 200
    assert "What&#39;s new" in r.text or "What's new" in r.text
    assert "Bottom-of-home perex" in r.text
    assert "/news" in r.text


def test_home_omits_news_section_when_no_intro(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _client_with_session(conn)
    finally:
        conn.close()
        close_system_db()
    r = c.get("/home")
    assert r.status_code == 200
    # The CSS selectors are in the <style> block regardless; the rendered
    # `<section class="home-news">` block is the actual visible artifact
    # gated by `{% if news_intro %}`. Look for the section header copy.
    assert "What's new" not in r.text and "What&#39;s new" not in r.text
    assert "Read more &rarr;" not in r.text and "Read more →" not in r.text
