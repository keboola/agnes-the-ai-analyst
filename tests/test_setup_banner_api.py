"""End-to-end tests for /api/admin/setup-banner endpoints."""

import duckdb

from src.db import _ensure_schema
from src.setup_banner import build_setup_banner_context


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_can_set_and_clear_banner(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    # GET initial state
    r = c.get("/api/admin/setup-banner", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["content"] is None

    # PUT banner
    r = c.put(
        "/api/admin/setup-banner",
        json={"content": "<p>VPN required before install.</p>"},
        headers=admin,
    )
    assert r.status_code == 200

    # GET shows new content
    r = c.get("/api/admin/setup-banner", headers=admin)
    assert r.json()["content"] == "<p>VPN required before install.</p>"
    assert r.json()["updated_by"] is not None

    # DELETE = clear
    r = c.delete("/api/admin/setup-banner", headers=admin)
    assert r.status_code == 204

    r = c.get("/api/admin/setup-banner", headers=admin)
    assert r.json()["content"] is None


def test_non_admin_cannot_edit_banner(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.put("/api/admin/setup-banner", json={"content": "<p>x</p>"}, headers=analyst)
    assert r.status_code == 403


def test_put_rejects_invalid_jinja2(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/setup-banner",
        json={"content": "{% for x in y %}"},  # unclosed loop
        headers=admin,
    )
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


def test_put_rejects_undefined_placeholder(seeded_app):
    """Templates that reference unknown placeholders must be rejected at PUT
    time so the admin sees the error immediately."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/setup-banner",
        json={"content": "Hello {{ user.emial }}"},  # typo
        headers=admin,
    )
    assert r.status_code == 400
    assert "emial" in r.json()["detail"] or "undefined" in r.json()["detail"].lower()


def test_preview_renders_arbitrary_content(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/setup-banner/preview",
        json={"content": "<b>Hello {{ user.email }}</b>"},
        headers=admin,
    )
    assert r.status_code == 200
    # autoescape=True: rendered content must contain the escaped or literal email
    assert "admin@test.com" in r.json()["content"]


def test_preview_requires_admin(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.post(
        "/api/admin/setup-banner/preview",
        json={"content": "<p>x</p>"},
        headers=analyst,
    )
    assert r.status_code == 403


def test_preview_rejects_invalid_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/setup-banner/preview",
        json={"content": "{% for x in y %}"},
        headers=admin,
    )
    assert r.status_code == 400


def test_validation_stub_matches_build_context_shape(seeded_app, tmp_path, monkeypatch):
    """If build_setup_banner_context grows new keys, _VALIDATION_STUB_CONTEXT
    must too — otherwise admins can save templates referencing keys the PUT
    validator accepts but the live render rejects."""
    from app.api.setup_banner import _VALIDATION_STUB_CONTEXT

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    conn.close()

    user = {"id": "u1", "email": "admin@test.com", "name": "Admin", "is_admin": True}
    real_ctx = build_setup_banner_context(user=user, server_url="https://example.com")

    # Top-level keys must match (stub has user=dict, real has user=dict when logged in)
    assert set(_VALIDATION_STUB_CONTEXT.keys()) == set(real_ctx.keys()), (
        f"_VALIDATION_STUB_CONTEXT top-level keys differ from build_setup_banner_context output. "
        f"Stub has: {set(_VALIDATION_STUB_CONTEXT.keys())}, "
        f"real has: {set(real_ctx.keys())}"
    )

    # One level deep for nested dicts
    for key in ("instance", "server", "user"):
        if isinstance(real_ctx.get(key), dict):
            assert set(_VALIDATION_STUB_CONTEXT[key].keys()) == set(real_ctx[key].keys()), (
                f"_VALIDATION_STUB_CONTEXT[{key!r}] drifted from build_setup_banner_context output"
            )
