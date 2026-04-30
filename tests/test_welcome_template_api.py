"""End-to-end tests for /api/welcome and /api/admin/welcome-template."""


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_get_welcome_returns_rendered_markdown(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "content" in body
    assert "AI Data Analyst" in body["content"]
    assert "https://example.com" in body["content"]


def test_get_welcome_requires_auth(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/welcome", params={"server_url": "https://example.com"})
    assert resp.status_code == 401


def test_admin_can_set_and_reset_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    # GET initial state
    r = c.get("/api/admin/welcome-template", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["content"] is None
    # The shipped default starts with the Jinja2 comment block.
    assert body["default"].startswith("{#")

    # PUT override
    r = c.put(
        "/api/admin/welcome-template",
        json={"content": "Hello {{ user.email }}"},
        headers=admin,
    )
    assert r.status_code == 200

    # Verify rendered output uses override
    r = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=admin,  # admin user can also call /api/welcome
    )
    assert r.json()["content"].startswith("Hello ")

    # DELETE = reset
    r = c.delete("/api/admin/welcome-template", headers=admin)
    assert r.status_code == 204
    r = c.get("/api/admin/welcome-template", headers=admin)
    assert r.json()["content"] is None


def test_non_admin_cannot_edit_template(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.put("/api/admin/welcome-template", json={"content": "x"}, headers=analyst)
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
    """Templates that parse but reference unknown placeholders must be rejected
    at PUT time so an admin can fix the typo immediately rather than after an
    analyst's bootstrap blows up."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/welcome-template",
        json={"content": "Hello {{ user.emial }}"},  # typo, would fail StrictUndefined at render
        headers=admin,
    )
    assert r.status_code == 400
    assert "emial" in r.json()["detail"] or "undefined" in r.json()["detail"].lower()


def test_get_welcome_500_includes_reset_hint_on_render_failure(seeded_app, monkeypatch):
    """If an override slips through validation and fails at render time, the
    user-visible 500 must point at /admin/welcome rather than leaking a
    Jinja stack trace."""
    # Stub render_welcome to raise a TemplateError so we exercise the
    # exception path without needing a malformed override (PUT validation
    # blocks those now).
    from jinja2 import UndefinedError
    import app.api.welcome as welcome_module

    def fake_render(*args, **kwargs):
        raise UndefinedError("'foo' is undefined")

    monkeypatch.setattr(welcome_module, "render_welcome", fake_render)

    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=admin,
    )
    assert r.status_code == 500
    assert "/admin/welcome" in r.json()["detail"]


def test_admin_preview_renders_arbitrary_content(seeded_app):
    """Preview endpoint must render the supplied content (not whatever's
    stored), so the admin UI can show pre-save preview."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/welcome-template/preview",
        json={"content": "# Preview {{ user.email }}"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["content"].startswith("# Preview admin@test.com")


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
        json={"content": "# x"},
        headers=analyst,
    )
    assert r.status_code == 403
