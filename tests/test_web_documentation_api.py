"""GET /documentation/api — curated API guide page.

Auth-gated (any logged-in user, NO admin requirement — same audience
rationale as /marketplace/format-guide), renders docs/api-reference.md.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_api_guide_requires_login(seeded_app):
    """Anonymous user does not get the page."""
    client = seeded_app["client"]
    r = client.get("/documentation/api", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401)


def test_api_guide_renders_for_non_admin(seeded_app):
    """Any logged-in user (analyst, not admin) sees the rendered guide."""
    client = seeded_app["client"]
    r = client.get("/documentation/api", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    body = r.text
    # Markdown h1 rendered to HTML
    assert "API Reference" in body
    # Links out to the auto-generated references
    assert 'href="/docs"' in body
    assert 'href="/redoc"' in body
    # Live version stamp from template context, not from the markdown
    assert "Running version" in body


def test_documentation_section_in_admin_menu(seeded_app):
    """Admin dropdown carries a Documentation section linking guide + Swagger + ReDoc."""
    client = seeded_app["client"]
    r = client.get("/dashboard", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    body = r.text
    assert 'data-section="documentation"' in body
    assert 'href="/documentation/api"' in body
    assert ">API Guide<" in body
    assert 'href="/docs"' in body
    assert 'href="/redoc"' in body
