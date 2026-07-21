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
    assert 'id="sug-run-mining"' in resp.text  # admin can trigger a mining run


def test_memory_mining_consent_page_renders_for_user(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/me/memory-mining", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'id="mm-toggle"' in resp.text
    assert "/static/js/me_memory_mining.js" in resp.text


def test_suggestions_review_page_requires_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get(
        "/admin/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307, 401, 403)


def test_skill_domain_registered_as_direct_submit():
    from app.web.studio import STUDIO_DOMAINS, get_domain

    spec = get_domain("skill")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "skill-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]
    # every other domain except "agent" (the store's other direct-submit
    # type) still routes through the suggestions queue
    assert all(not d.submit_directly for s, d in STUDIO_DOMAINS.items() if s not in ("skill", "agent"))


def test_agent_domain_registered_as_direct_submit():
    from app.web.studio import get_domain

    spec = get_domain("agent")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "agent-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]


def test_agent_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body
    assert "Submit for approval" not in body


def test_agent_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the agent content textarea rendered
    assert 'domain: "agent"' in resp.text


def test_skill_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body  # direct-submit domains publish, not suggest
    assert "Submit for approval" not in body
    assert "store" in body.lower()  # footer explains the store review pipeline


def test_skill_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the markdown textarea rendered


def test_existing_domains_keep_suggestion_flow(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["analyst_token"]))
    assert "submitDirect: false" in resp.text
    assert "Submit for approval" in resp.text


def test_studio_index_lists_every_domain(seeded_app):
    from app.web.studio import STUDIO_DOMAINS

    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    for slug, domain in STUDIO_DOMAINS.items():
        assert f"/admin/studio/{slug}" in body
        assert domain.title in body


def test_studio_index_requires_login(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_primary_nav_links_to_studio(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/admin/studio"' in resp.text
