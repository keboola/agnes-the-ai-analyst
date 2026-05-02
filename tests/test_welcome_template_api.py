"""End-to-end tests for /api/admin/welcome-template (banner editor endpoints).

GET /api/welcome has been removed — the analyst-facing endpoint is gone.
These tests cover only the admin CRUD + preview endpoints.
"""

import duckdb

from src.db import _ensure_schema
from src.welcome_template import build_context


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_get_welcome_endpoint_removed(seeded_app):
    """GET /api/welcome must return 404 — the endpoint was deleted."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=_auth(token),
    )
    assert resp.status_code == 404


def test_admin_get_template_initially_null(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    r = c.get("/api/admin/welcome-template", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["content"] is None
    # No longer returns a `default` field — banner default is empty
    assert "default" not in body or body.get("default") is None


def test_admin_can_set_and_reset_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    # PUT override
    r = c.put(
        "/api/admin/welcome-template",
        json={"content": "<p>Hello {{ user.email }}</p>"},
        headers=admin,
    )
    assert r.status_code == 200

    # GET reflects override
    r = c.get("/api/admin/welcome-template", headers=admin)
    assert r.status_code == 200
    assert r.json()["content"] == "<p>Hello {{ user.email }}</p>"

    # DELETE = reset (no banner)
    r = c.delete("/api/admin/welcome-template", headers=admin)
    assert r.status_code == 204
    r = c.get("/api/admin/welcome-template", headers=admin)
    assert r.json()["content"] is None


def test_non_admin_cannot_edit_template(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.put("/api/admin/welcome-template", json={"content": "<p>x</p>"}, headers=analyst)
    assert r.status_code == 403


def test_invalid_jinja2_returns_400(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/welcome-template",
        json={"content": "{% for x in y %}"},  # unclosed loop
        headers=admin,
    )
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


def test_put_rejects_undefined_placeholder(seeded_app):
    """Templates that reference unknown placeholders must be rejected at PUT time."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/welcome-template",
        json={"content": "<p>{{ user.emial }}</p>"},  # typo
        headers=admin,
    )
    assert r.status_code == 400
    assert "emial" in r.json()["detail"] or "undefined" in r.json()["detail"].lower()


def test_admin_preview_renders_html(seeded_app):
    """Preview endpoint renders supplied HTML content without persisting."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/welcome-template/preview",
        json={"content": "<p>Preview for {{ user.email }}</p>"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["content"].startswith("<p>Preview for admin@test.com")


def test_preview_rejects_invalid_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/welcome-template/preview",
        json={"content": "{% for x in y %}"},
        headers=admin,
    )
    assert r.status_code == 400


def test_preview_requires_admin(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.post(
        "/api/admin/welcome-template/preview",
        json={"content": "<p>x</p>"},
        headers=analyst,
    )
    assert r.status_code == 403


def test_validation_stub_matches_build_context_shape(seeded_app, tmp_path, monkeypatch):
    """_VALIDATION_STUB_CONTEXT top-level keys must match build_context() output.

    If build_context gains new keys, the stub must track them so admins can
    save templates that reference those keys without hitting a live-render
    rejection after the PUT validation accepted them.
    """
    from app.api.welcome import _VALIDATION_STUB_CONTEXT

    user = {
        "id": "u1",
        "email": "admin@test.com",
        "name": "Admin",
        "is_admin": True,
        "groups": ["Admin"],
    }
    real_ctx = build_context(user=user, server_url="https://example.com")

    # Top-level keys must match
    assert set(_VALIDATION_STUB_CONTEXT.keys()) == set(real_ctx.keys()), (
        f"_VALIDATION_STUB_CONTEXT top-level keys differ from build_context output. "
        f"Stub has: {set(_VALIDATION_STUB_CONTEXT.keys())}, "
        f"real has: {set(real_ctx.keys())}"
    )

    # One level deep for nested dicts (user may be None in real_ctx — compare stub shape)
    for key in ("instance", "server"):
        assert set(_VALIDATION_STUB_CONTEXT[key].keys()) == set(real_ctx[key].keys()), (
            f"_VALIDATION_STUB_CONTEXT[{key!r}] drifted from build_context output"
        )
    # user sub-keys
    if real_ctx.get("user") and _VALIDATION_STUB_CONTEXT.get("user"):
        assert set(_VALIDATION_STUB_CONTEXT["user"].keys()) == set(real_ctx["user"].keys()), (
            "_VALIDATION_STUB_CONTEXT['user'] drifted from build_context output"
        )
