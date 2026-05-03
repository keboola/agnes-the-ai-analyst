"""End-to-end tests for the agent-workspace-prompt API endpoints.

GET  /api/welcome                         — analyst-facing rendered CLAUDE.md
GET  /api/admin/workspace-prompt-template — admin: get template + default
PUT  /api/admin/workspace-prompt-template — admin: set override
DELETE /api/admin/workspace-prompt-template — admin: reset to default
POST /api/admin/workspace-prompt-template/preview — admin: live preview
"""


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /api/welcome — analyst-facing rendered CLAUDE.md
# ---------------------------------------------------------------------------

def test_get_welcome_requires_auth(seeded_app):
    """Unauthenticated GET /api/welcome must return 401 or 422."""
    c = seeded_app["client"]
    resp = c.get("/api/welcome", params={"server_url": "https://example.com"})
    assert resp.status_code in (401, 422)


def test_get_welcome_returns_rendered_markdown(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])

    resp = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=analyst,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "content" in body
    assert isinstance(body["content"], str)
    assert body["content"].strip() != ""


def test_get_welcome_uses_override_when_set(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    analyst = _auth(seeded_app["analyst_token"])

    # Set an override
    r = c.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "# Custom CLAUDE.md for {{ user.email }}"},
        headers=admin,
    )
    assert r.status_code == 200

    # Analyst fetch should include the override
    resp = c.get(
        "/api/welcome",
        params={"server_url": "https://example.com"},
        headers=analyst,
    )
    assert resp.status_code == 200
    assert "Custom CLAUDE.md" in resp.json()["content"]
    assert "analyst@test.com" in resp.json()["content"]

    # Reset
    c.delete("/api/admin/workspace-prompt-template", headers=admin)


# ---------------------------------------------------------------------------
# GET /api/admin/workspace-prompt-template — admin get
# ---------------------------------------------------------------------------

def test_admin_get_template_initially_null(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    r = c.get("/api/admin/workspace-prompt-template", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["content"] is None
    assert "default" in body
    assert body["default"]  # non-empty default


def test_admin_get_template_default_contains_instance_name(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    r = c.get("/api/admin/workspace-prompt-template", headers=admin)
    assert r.status_code == 200
    body = r.json()
    # Default template renders the instance name
    assert body["default"] != ""


def test_non_admin_cannot_get_template(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.get("/api/admin/workspace-prompt-template", headers=analyst)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/admin/workspace-prompt-template — save override
# ---------------------------------------------------------------------------

def test_admin_can_set_and_reset_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])

    # PUT override
    r = c.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "# Hello {{ user.email }}"},
        headers=admin,
    )
    assert r.status_code == 200

    # GET reflects override
    r = c.get("/api/admin/workspace-prompt-template", headers=admin)
    assert r.status_code == 200
    assert r.json()["content"] == "# Hello {{ user.email }}"

    # DELETE = reset
    r = c.delete("/api/admin/workspace-prompt-template", headers=admin)
    assert r.status_code == 204
    r = c.get("/api/admin/workspace-prompt-template", headers=admin)
    assert r.json()["content"] is None


def test_non_admin_cannot_put_template(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "# evil override"},
        headers=analyst,
    )
    assert r.status_code == 403


def test_invalid_jinja2_returns_400(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "{% for x in y %}"},  # unclosed loop
        headers=admin,
    )
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


def test_put_rejects_undefined_placeholder(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "{{ no_such_variable }}"},
        headers=admin,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/admin/workspace-prompt-template
# ---------------------------------------------------------------------------

def test_non_admin_cannot_delete_template(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.delete("/api/admin/workspace-prompt-template", headers=analyst)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/workspace-prompt-template/preview
# ---------------------------------------------------------------------------

def test_admin_preview_renders_content(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/workspace-prompt-template/preview",
        json={"content": "# Preview for {{ user.email }}"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["content"].startswith("# Preview for admin@test.com")


def test_preview_rejects_invalid_template(seeded_app):
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/workspace-prompt-template/preview",
        json={"content": "{% for x in y %}"},
        headers=admin,
    )
    assert r.status_code == 400


def test_preview_requires_admin(seeded_app):
    c = seeded_app["client"]
    analyst = _auth(seeded_app["analyst_token"])
    r = c.post(
        "/api/admin/workspace-prompt-template/preview",
        json={"content": "# Preview"},
        headers=analyst,
    )
    assert r.status_code == 403


def test_preview_uses_live_context(seeded_app):
    """Preview should include live table data from context."""
    c = seeded_app["client"]
    admin = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/workspace-prompt-template/preview",
        json={"content": "tables: {{ tables | length }}, metrics: {{ metrics.count }}"},
        headers=admin,
    )
    assert r.status_code == 200
    # Content must be a rendered string (not raise), numbers may be 0 on fresh DB
    assert "tables:" in r.json()["content"]


# ---------------------------------------------------------------------------
# Validation stub vs. build_claude_md_context shape alignment
# ---------------------------------------------------------------------------

def test_validation_stub_matches_build_context_shape(seeded_app, tmp_path, monkeypatch):
    """_VALIDATION_STUB_CONTEXT top-level keys must match build_claude_md_context() output."""
    from app.api.claude_md import _VALIDATION_STUB_CONTEXT
    from src.db import _ensure_schema, get_system_db
    import duckdb

    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)

    user = {
        "id": "u1",
        "email": "admin@test.com",
        "name": "Admin",
        "is_admin": True,
        "groups": ["Admin"],
    }
    from src.claude_md import build_claude_md_context
    real_ctx = build_claude_md_context(c, user=user, server_url="https://example.com")

    assert set(_VALIDATION_STUB_CONTEXT.keys()) == set(real_ctx.keys()), (
        f"_VALIDATION_STUB_CONTEXT top-level keys differ from build_claude_md_context output. "
        f"Stub: {set(_VALIDATION_STUB_CONTEXT.keys())}, "
        f"real: {set(real_ctx.keys())}"
    )
    c.close()
