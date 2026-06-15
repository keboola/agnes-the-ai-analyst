"""Route tests for the data-package builder studio page (authoring Slice 0)."""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_studio_page_requires_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code in (302, 401, 403)


def test_studio_page_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "data-package-builder" in body  # profile slug wired into the JS
    assert 'id="studio-create"' in body
    assert "/static/js/studio_data_package.js" in body
