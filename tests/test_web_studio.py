"""Route tests for the generic authoring-agent studio pages."""

import pytest

DOMAINS = ["data-package", "mcp", "marketplace", "corporate-memory"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_admin_in_create_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert 'id="studio-create"' in body
    assert "/static/js/studio.js" in body
    assert "window.STUDIO" in body
    assert "isAdmin: true" in body
    assert ">Create<" in body  # admin sees the direct-create action


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_non_admin_in_submit_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "Submit for approval" in body  # non-admin sees the suggestion action


def test_studio_requires_login(seeded_app):
    c = seeded_app["client"]
    # No auth header → redirect to login (don't follow it) or 401/403.
    resp = c.get("/admin/studio/data-package", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_studio_unknown_domain_404s(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/nope", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 404


def test_suggestions_review_page_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/suggestions", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "/static/js/studio_suggestions.js" in resp.text
    assert 'id="sug-list"' in resp.text


def test_suggestions_review_page_requires_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get(
        "/admin/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307, 401, 403)
