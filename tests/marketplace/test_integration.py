"""End-to-end tests via the FastAPI TestClient.

Covers:
- /api/marketplace/info with email fallback and PAT.
- /api/marketplace/zip 200 + 304 flow.
- /api/marketplace/git mount reachable (smart-HTTP info/refs).
- Email fallback rejected without the env flag.
- Equivalence: PAT path and email-fallback path return byte-identical ZIP + same ETag.
"""
from __future__ import annotations

import base64

import pytest


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.fixture
def client(configured):
    """Fresh TestClient per test. The `configured` fixture already sets
    TESTING, JWT_SECRET_KEY, DATA_DIR, and marketplace paths, so we just
    build the app here."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


def test_info_email_fallback(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/info?email=admin@test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "admin@test"
    assert {p["name"] for p in body["plugins"]} == {"alpha", "beta", "gamma"}


def test_info_email_fallback_disabled(client, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    r = client.get("/api/marketplace/info?email=admin@test")
    assert r.status_code == 401


def test_info_no_auth(client, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    r = client.get("/api/marketplace/info")
    assert r.status_code == 401


def test_zip_email_fallback_200(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/zip?email=admin@test")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    etag = r.headers["etag"].strip('"')
    assert len(etag) == 16


def test_zip_304_conditional(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r1 = client.get("/api/marketplace/zip?email=admin@test")
    etag = r1.headers["etag"]
    r2 = client.get(
        "/api/marketplace/zip?email=admin@test",
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 304


def test_git_info_refs_401_without_auth(client):
    r = client.get("/api/marketplace/git/info/refs?service=git-upload-pack")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_git_info_refs_200_with_email_fallback(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get(
        "/api/marketplace/git/info/refs?service=git-upload-pack",
        headers={"Authorization": _basic("x", "admin@test")},
    )
    assert r.status_code == 200, r.text
    assert b"# service=git-upload-pack" in r.content
    assert b"refs/heads/main" in r.content


def test_info_missing_source_returns_503(client, configured, monkeypatch):
    """If /data/marketplace/source is gone, endpoints return 503 (not 500)."""
    import shutil
    shutil.rmtree(configured["source"])
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/info?email=admin@test")
    assert r.status_code == 503


def test_zip_missing_source_returns_503(client, configured, monkeypatch):
    import shutil
    shutil.rmtree(configured["source"])
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/zip?email=admin@test")
    assert r.status_code == 503


def test_git_missing_source_returns_503(client, configured, monkeypatch):
    import shutil
    shutil.rmtree(configured["source"])
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get(
        "/api/marketplace/git/info/refs?service=git-upload-pack",
        headers={"Authorization": _basic("x", "admin@test")},
    )
    assert r.status_code == 503


def test_jwt_and_email_fallback_return_identical_zip(client, monkeypatch):
    """Equivalence: JWT-authenticated request and email-fallback request
    return byte-identical ZIP + same ETag for the same user.

    Uses a session token (not a PAT) to avoid needing a DB row in
    `personal_access_tokens`; the endpoint resolution is identical either way
    once `get_current_user` returns the user record. PAT-specific credential
    parsing is covered separately in test_auth.py.
    """
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        if not repo.get_by_email("admin@test"):
            repo.create(id="mkt-admin-1", email="admin@test", name="Admin", role="admin")
    finally:
        conn.close()

    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    token = create_access_token(
        user_id="mkt-admin-1", email="admin@test", role="admin",
    )

    r_email = client.get("/api/marketplace/zip?email=admin@test")
    r_jwt = client.get(
        "/api/marketplace/zip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_email.status_code == 200, r_email.text
    assert r_jwt.status_code == 200, r_jwt.text
    assert r_email.headers["etag"] == r_jwt.headers["etag"]
    assert r_email.content == r_jwt.content
