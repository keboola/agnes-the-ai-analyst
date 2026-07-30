"""REST-level guard for the oauth/per_user coupling (spec §1 Rules).

``auth_method='oauth'`` is only valid with a network transport AND
``scope='per_user'`` — every fail-closed per-user path keys off ``scope``,
so an oauth+shared source would silently no-op all of them. The repo layer
enforces this too (tests/db_pg/test_mcp_oauth_contract.py); these tests pin
the admin API surface, including the partial-update path that could flip a
field without mentioning the others.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")


def _hdr(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _create(client, hdr, **overrides):
    body = {
        "name": "oauth_probe",
        "transport": "http",
        "url": "https://upstream.example.com/mcp",
        "auth_method": "oauth",
        "scope": "per_user",
    }
    body.update(overrides)
    return client.post("/api/admin/mcp-sources", headers=hdr, json=body)


def test_create_oauth_per_user_http_is_accepted(seeded_app):
    r = _create(seeded_app["client"], _hdr(seeded_app))
    assert r.status_code == 201, r.text


def test_create_oauth_shared_scope_rejected(seeded_app):
    r = _create(seeded_app["client"], _hdr(seeded_app), name="oauth_bad1", scope="shared")
    assert r.status_code == 422
    assert "per_user" in r.text


def test_create_oauth_default_scope_rejected(seeded_app):
    # scope omitted defaults to 'shared' — must still be rejected, not
    # silently accepted with the default.
    r = _create(seeded_app["client"], _hdr(seeded_app), name="oauth_bad2", scope=None)
    assert r.status_code == 422


def test_create_oauth_stdio_rejected(seeded_app):
    r = _create(
        seeded_app["client"],
        _hdr(seeded_app),
        name="oauth_bad3",
        transport="stdio",
        url=None,
        command="some-server",
    )
    assert r.status_code == 422


def test_partial_update_cannot_break_the_coupling(seeded_app):
    client, hdr = seeded_app["client"], _hdr(seeded_app)
    r = _create(client, hdr, name="oauth_flip")
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # Flipping scope away from per_user on an oauth source must fail…
    r2 = client.put(f"/api/admin/mcp-sources/{sid}", headers=hdr, json={"scope": "shared"})
    assert r2.status_code in (400, 422), r2.text

    # …and flipping auth_method to oauth on a shared source must fail too.
    r3 = client.post(
        "/api/admin/mcp-sources",
        headers=hdr,
        json={
            "name": "plain_bearer",
            "transport": "http",
            "url": "https://upstream.example.com/mcp",
            "auth_method": "bearer",
            "scope": "shared",
        },
    )
    assert r3.status_code == 201
    sid2 = r3.json()["id"]
    r4 = client.put(f"/api/admin/mcp-sources/{sid2}", headers=hdr, json={"auth_method": "oauth"})
    assert r4.status_code in (400, 422), r4.text
