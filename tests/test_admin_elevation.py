"""Admin elevation consent gate (app/auth/elevation.py).

While an admin's elevation is paused (cookie or instance default), the
Admin short-circuit in ``can_access`` is skipped and ``require_admin``
refuses with the distinct ``admin_elevation_paused`` detail. The cookie
can only reduce privilege; non-request contexts always see the elevated
default so scheduler/CLI automation is untouched.
"""

import logging

import pytest

from app.auth import access, elevation
from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_conn(seeded_app):
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit level: contextvar + can_access interplay
# ---------------------------------------------------------------------------


def test_default_is_elevated_outside_requests(system_conn):
    assert elevation.elevation_paused() is False
    assert access.can_access("admin1", "table", "keboola.anything", conn=system_conn)


def test_paused_skips_god_mode_short_circuit(system_conn):
    token = elevation.set_paused_for_request(True)
    try:
        assert not access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn)
    finally:
        elevation.reset_for_request(token)
    # and back to normal after reset
    assert access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn)


def test_paused_admin_keeps_explicit_grants(system_conn):
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    group = UserGroupsRepository(system_conn).ensure("elevation-grantees")
    UserGroupMembersRepository(system_conn).add_member("admin1", group["id"], source="admin")
    ResourceGrantsRepository(system_conn).ensure_grant(group["id"], "table", "keboola.mine")

    token = elevation.set_paused_for_request(True)
    try:
        assert access.can_access("admin1", "table", "keboola.mine", conn=system_conn)
        assert not access.can_access("admin1", "table", "keboola.other", conn=system_conn)
    finally:
        elevation.reset_for_request(token)


def test_paused_never_logs_god_mode_bypass(system_conn, caplog):
    access._god_mode_logged.clear()
    token = elevation.set_paused_for_request(True)
    try:
        with caplog.at_level(logging.INFO, logger="app.auth.access"):
            access.can_access("admin1", "table", "keboola.paused-check", conn=system_conn)
    finally:
        elevation.reset_for_request(token)
    assert not [r for r in caplog.records if "god_mode_bypass" in r.getMessage()]


def test_resolve_from_cookie_precedence(monkeypatch):
    assert elevation.resolve_from_cookie("paused") is True
    assert elevation.resolve_from_cookie("elevated") is False
    # absence/garbage → instance default
    assert elevation.resolve_from_cookie(None) is False
    assert elevation.resolve_from_cookie("junk") is False
    monkeypatch.setattr(elevation, "default_elevation", lambda: elevation.PAUSED)
    assert elevation.resolve_from_cookie(None) is True
    assert elevation.resolve_from_cookie("elevated") is False  # explicit cookie wins


# ---------------------------------------------------------------------------
# HTTP level: middleware + toggle endpoint + require_admin
# ---------------------------------------------------------------------------


def test_admin_api_refuses_while_paused_cookie(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 200

    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.PAUSED)
    try:
        r = c.get("/api/admin/groups", headers=_auth(admin))
        assert r.status_code == 403
        assert r.json()["detail"] == "admin_elevation_paused"
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)

    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 200


def test_toggle_endpoint_sets_cookie_and_audits(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    r = c.post("/api/me/elevation", json={"paused": True}, headers=_auth(admin))
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "paused": True}
    assert c.cookies.get(elevation.ELEVATION_COOKIE) == elevation.PAUSED

    # paused admin can still re-elevate (endpoint gates on RAW membership,
    # not require_admin — otherwise they'd be locked out)
    r = c.post("/api/me/elevation", json={"paused": False}, headers=_auth(admin))
    assert r.status_code == 200
    assert c.cookies.get(elevation.ELEVATION_COOKIE) == elevation.ELEVATED
    c.cookies.delete(elevation.ELEVATION_COOKIE)


def test_toggle_endpoint_refuses_non_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/me/elevation", json={"paused": True}, headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_instance_default_paused_flips_admin_default(seeded_app, monkeypatch):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    monkeypatch.setattr(elevation, "default_elevation", lambda: elevation.PAUSED)

    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 403

    # explicit elevated cookie wins over the paused default
    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.ELEVATED)
    try:
        r = c.get("/api/admin/groups", headers=_auth(admin))
        assert r.status_code == 200
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)
