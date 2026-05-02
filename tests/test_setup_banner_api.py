"""End-to-end tests for /api/admin/setup-banner endpoints."""


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
