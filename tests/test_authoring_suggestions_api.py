"""API tests for the authoring_suggestions queue (v77)."""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _submit(client, token, domain="data-package", payload=None):
    return client.post(
        "/api/studio/suggestions",
        headers=_auth(token),
        json={"domain": domain, "payload": payload or {"name": "X", "slug": "x"}},
    )


def test_non_admin_can_submit_suggestion(seeded_app):
    c = seeded_app["client"]
    r = _submit(c, seeded_app["analyst_token"])
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"


def test_submit_rejects_unknown_domain(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/api/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        json={"domain": "nope", "payload": {"name": "x"}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "unknown_domain"


def test_caller_sees_only_their_own(seeded_app):
    c = seeded_app["client"]
    _submit(c, seeded_app["analyst_token"], payload={"name": "mine", "slug": "mine"})
    r = c.get("/api/studio/suggestions/mine", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    assert all(s["created_by"] == "analyst@test.com" for s in r.json())


def test_admin_queue_and_approve_flow(seeded_app):
    c = seeded_app["client"]
    sid = _submit(c, seeded_app["analyst_token"]).json()["id"]

    # admin lists pending
    q = c.get(
        "/api/admin/authoring-suggestions?status=pending",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert q.status_code == 200
    assert any(s["id"] == sid for s in q.json())

    # admin approves
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={"note": "lgtm"},
    )
    assert a.status_code == 200
    assert a.json()["status"] == "approved"

    # re-approving a resolved row is a 409 guard miss
    a2 = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a2.status_code == 409


def test_admin_endpoints_require_admin(seeded_app):
    c = seeded_app["client"]
    r = c.get("/api/admin/authoring-suggestions", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code in (401, 403)


def test_approve_data_package_auto_creates_resource(seeded_app):
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="data-package",
        payload={"name": "Auto", "slug": "auto-dp", "description": "x"},
    ).json()["id"]
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 200
    rid = a.json()["created_resource_id"]
    assert rid and rid.startswith("pkg_")
    pkgs = c.get("/api/admin/data-packages", headers=_auth(seeded_app["admin_token"])).json()
    assert any(p.get("slug") == "auto-dp" for p in pkgs)


def test_approve_mcp_does_not_auto_create(seeded_app):
    """mcp/marketplace are excluded from auto-replay (command/url privilege
    vector); approve records the disposition without creating the resource."""
    c = seeded_app["client"]
    sid = _submit(
        c,
        seeded_app["analyst_token"],
        domain="mcp",
        payload={"name": "e2e_x", "transport": "http", "url": "https://x"},
    ).json()["id"]
    a = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert a.status_code == 200
    assert a.json()["created_resource_id"] is None
