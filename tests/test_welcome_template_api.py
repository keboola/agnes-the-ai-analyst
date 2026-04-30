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
    assert "syntax" in r.json()["detail"].lower()
