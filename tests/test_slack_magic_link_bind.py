"""Magic-link Slack identity binding.

Covers the `bind_prompt`/`bind_link` helpers (the one-click link Agnes DMs)
and the `GET /slack/bind` route that redeems the code server-side for the
signed-in Agnes account — the auth-gated, no-copy-paste replacement for the
old "visit /setup?slack=1 and paste this code" flow (which had no UI).
"""

import pytest
from fastapi.testclient import TestClient


def test_bind_link_and_prompt_build_one_click_link():
    from services.slack_bot.binding import bind_link, bind_prompt

    assert bind_link("https://agnes.example.com", "123456") == "https://agnes.example.com/slack/bind?code=123456"
    # trailing slash on public_url is normalized
    assert bind_link("https://agnes.example.com/", "123456") == "https://agnes.example.com/slack/bind?code=123456"
    # unset public_url → root-relative path (still works behind the right host)
    assert bind_link("", "123456") == "/slack/bind?code=123456"

    msg = bind_prompt("https://agnes.example.com", "123456")
    assert "https://agnes.example.com/slack/bind?code=123456" in msg
    # the old copy-paste instruction is gone
    assert "Paste this" not in msg


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    for sub in ("state", "analytics", "extracts"):
        (tmp_path / sub).mkdir()
    from src.db import close_system_db

    close_system_db()
    from app.main import create_app

    yield TestClient(create_app())
    close_system_db()


@pytest.fixture
def authed_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    pw = "UserPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="u1",
        email="user@test.com",
        name="U",
        password_hash=PasswordHasher().hash(pw),
    )
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "user@test.com", "password": pw})
    assert resp.status_code == 200, resp.text
    return {"access_token": resp.json()["access_token"]}


def test_slack_bind_unauthed_redirects_to_login(web_client):
    resp = web_client.get("/slack/bind?code=123456", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "/login" in loc and "next=" in loc


def test_slack_bind_valid_code_binds_the_signed_in_account(web_client, authed_cookie):
    from services.slack_bot.binding import issue_verification_code
    from src.db import get_system_db

    conn = get_system_db()
    code = issue_verification_code(conn, slack_user_id="U_TESTBIND")
    conn.close()

    resp = web_client.get(f"/slack/bind?code={code}", cookies=authed_cookie)
    assert resp.status_code == 200
    assert "Slack connected" in resp.text

    conn = get_system_db()
    row = conn.execute("SELECT slack_user_id FROM users WHERE email = ?", ["user@test.com"]).fetchone()
    conn.close()
    assert row[0] == "U_TESTBIND"


def test_slack_bind_bad_code_shows_invalid(web_client, authed_cookie):
    resp = web_client.get("/slack/bind?code=000000", cookies=authed_cookie)
    assert resp.status_code == 200
    assert "expired or invalid" in resp.text
