"""REST surface for /api/admin/news/* — RBAC, CRUD, sanitizer integration.

Mirrors the test scaffolding in tests/test_api_me_onboarded.py: per-test
fresh DATA_DIR + JWT secret, helper to mint admin / non-admin sessions
through TestClient, hit endpoints with cookie auth.
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


def _make_user(conn, email: str, *, admin: bool):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])

    if admin:
        # Add to the seeded Admin group so require_admin grants access.
        admin_group = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()
        assert admin_group, "Admin system group should be seeded by bootstrap"
        conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
            "VALUES (?, ?, 'admin', 'test')",
            [uid, admin_group[0]],
        )

    token = create_access_token(user_id=uid, email=email)
    return uid, token


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _make_admin_client(conn):
    _, token = _make_user(conn, "admin@example.com", admin=True)
    c = _client()
    c.cookies.set("access_token", token)
    return c


def _make_user_client(conn):
    _, token = _make_user(conn, "user@example.com", admin=False)
    c = _client()
    c.cookies.set("access_token", token)
    return c


def test_get_current_returns_unpublished_envelope_when_empty(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()
    resp = c.get("/api/admin/news/current")
    assert resp.status_code == 200
    assert resp.json() == {"published": False}


def test_non_admin_blocked(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_user_client(conn)
    finally:
        conn.close()
        close_system_db()
    resp = c.get("/api/admin/news/current")
    assert resp.status_code in (401, 403)


def test_full_lifecycle_draft_publish_unpublish(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()

    # No draft yet → 404
    assert c.get("/api/admin/news/draft").status_code == 404

    # Save draft
    r = c.put("/api/admin/news/draft", json={"intro": "<p>v1 intro</p>", "content": "<h1>v1</h1>"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["published"] is False
    assert body["intro"] == "<p>v1 intro</p>"

    # Publish → /current returns it
    r = c.post("/api/admin/news/publish")
    assert r.status_code == 200
    pub = r.json()
    assert pub["published"] is True

    cur = c.get("/api/admin/news/current").json()
    assert cur["version"] == 1
    assert cur["published"] is True

    # Publish again with no draft → 409
    r = c.post("/api/admin/news/publish")
    assert r.status_code == 409
    assert r.json()["detail"] == "no_draft"

    # New draft v2; unpublishing v1 while v2 draft exists → 409
    c.put("/api/admin/news/draft", json={"intro": "<p>v2</p>", "content": "<p>V2</p>"})
    r = c.post("/api/admin/news/unpublish/1")
    assert r.status_code == 409

    # Publish v2 → unpublish v2 → web falls back to v1
    c.post("/api/admin/news/publish")
    r = c.post("/api/admin/news/unpublish/2")
    assert r.status_code == 200
    cur = c.get("/api/admin/news/current").json()
    assert cur["version"] == 1


def test_versions_list_paginated(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()

    # Create 3 versions, all published
    for i in range(1, 4):
        c.put("/api/admin/news/draft", json={"intro": f"<p>v{i}</p>", "content": f"<p>V{i}</p>"})
        c.post("/api/admin/news/publish")

    rows = c.get("/api/admin/news/versions?limit=10").json()["versions"]
    assert [r["version"] for r in rows] == [3, 2, 1]
    assert all(r["status"] == "published" for r in rows)


def test_preview_sanitizes_without_persisting(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()

    r = c.post(
        "/api/admin/news/preview",
        json={
            "intro": "<p>x<script>alert(1)</script></p>",
            "content": '<iframe src="https://evil.com/x"></iframe><p>ok</p>',
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "<script>" not in body["intro"]
    assert "evil.com" not in body["content"]
    # Preview must not persist anything.
    assert c.get("/api/admin/news/draft").status_code == 404


def test_publish_with_expected_version_mismatch_returns_409(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()

    c.put("/api/admin/news/draft", json={"intro": "<p>v1</p>", "content": "V1"})
    # Active draft is v1; publish with ?expected_version=42 must 409.
    r = c.post("/api/admin/news/publish?expected_version=42")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "version_conflict"
    assert detail["expected"] == 42
    assert detail["actual"] == 1


def test_put_draft_with_expected_version_mismatch_returns_409(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()

    # Admin A creates draft v1; admin B (same client here) tries to save
    # believing no draft exists.
    c.put("/api/admin/news/draft", json={"intro": "<p>v1</p>", "content": "V1"})
    r = c.put(
        "/api/admin/news/draft?expected_version=0",
        json={"intro": "<p>fresh</p>", "content": "fresh"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "version_conflict"


def test_unknown_version_returns_404(fresh_db):
    from src.db import get_system_db, close_system_db
    conn = get_system_db()
    try:
        c = _make_admin_client(conn)
    finally:
        conn.close()
        close_system_db()
    r = c.get("/api/admin/news/versions/999")
    assert r.status_code == 404
