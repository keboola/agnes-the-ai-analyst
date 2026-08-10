"""Web-UI coverage for the auto-membership stack model (rail layout).

Every RBAC-granted data package / memory domain is automatically in the
caller's stack. My Stack now presents the stack as persistent context for
the Main Agent: everything shown is already in the stack, so the page
never exposes technical download states ("Download locally" / "Downloaded"
/ "Remove local copy"). Required grants cluster in a locked *Required*
group; everything else lands in *Added by you* with an overflow menu whose
"Remove from My Stack" hits the same unsubscribe endpoint. The
subscribe/unsubscribe API is unchanged — only the rendered wording is.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture(autouse=True)
def _auto_membership_mode(monkeypatch):
    """This suite pins the AUTO-membership semantics, which are opt-in since
    the classic subscribe model became the default again (spec
    2026-08-07-default-chrome-ux-parity). Classic-mode contracts live in
    tests/test_stack_membership_modes.py."""
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_available_package(conn, *, slug: str, name: str, user_id: str) -> str:
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    pkg_id = DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description="d",
        icon=None,
        color=None,
        created_by="test",
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'data_package', ?, 'available', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), gid, pkg_id],
    )
    return pkg_id


class TestAddedByYouGroup:
    def test_available_grant_lands_in_added_by_you_without_download_wording(self, seeded_app, monkeypatch):
        """A granted available package is already in the caller's stack
        (auto-membership) and shows on My Stack in the *Added by you* group
        with a removable overflow menu — never any download wording. The
        reshaped Catalog excludes anything already in the caller's stack, so
        it lives on My Stack, not the Catalog."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        from src.db import get_system_db

        conn = get_system_db()
        _grant_available_package(conn, slug="auto-member-pkg", name="Auto Member Pkg", user_id="analyst1")
        conn.close()

        c = seeded_app["client"]
        # Not on the Catalog — it's already in the stack.
        cat = c.get("/catalog", headers=_auth(seeded_app["analyst_token"]))
        assert cat.status_code == 200
        assert "Auto Member Pkg" not in cat.text
        # ...and on My Stack, in Added by you, with a Remove action wired to
        # the unsubscribe endpoint — no download states.
        resp = c.get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Auto Member Pkg" in body
        assert "Remove from My Stack" in body
        assert 'id="stk-added-body"' in body
        assert "Download locally" not in body
        assert "Downloaded" not in body
        assert 'data-toggle-kind="download"' not in body

    def test_subscribe_does_not_introduce_download_wording(self, seeded_app, monkeypatch):
        """POST /api/stack/subscribe still works (materializes a local copy),
        but My Stack no longer surfaces the download state — the row reads the
        same before and after: present, removable, no "Downloaded" wording."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        from src.db import get_system_db

        conn = get_system_db()
        pkg_id = _grant_available_package(conn, slug="auto-member-pkg-2", name="Auto Member Pkg 2", user_id="analyst1")
        conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        resp = c.get("/stack", headers=_auth(seeded_app["analyst_token"]))
        body = resp.text
        assert "Auto Member Pkg 2" in body
        assert "Downloaded" not in body
        assert "Remove local copy" not in body

    def test_my_stack_lists_granted_available_package(self, seeded_app, monkeypatch):
        """My Stack shows the package even before it's subscribed — it's
        already in the stack via auto-membership."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        from src.db import get_system_db

        conn = get_system_db()
        _grant_available_package(conn, slug="auto-member-pkg-3", name="Auto Member Pkg 3", user_id="analyst1")
        conn.close()

        c = seeded_app["client"]
        resp = c.get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Auto Member Pkg 3" in body
        assert "Remove from My Stack" in body


class TestApiStackMaterializedField:
    def test_stack_endpoint_exposes_materialized(self, seeded_app):
        from src.db import get_system_db

        conn = get_system_db()
        pkg_id = _grant_available_package(
            conn, slug="materialized-field-pkg", name="Materialized Field Pkg", user_id="analyst1"
        )
        conn.close()

        c = seeded_app["client"]
        r = c.get("/api/stack?type=data_package", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        items = r.json()["items"]
        match = next(it for it in items if it["id"] == pkg_id)
        assert match["in_stack"] is True
        assert match["materialized"] is False

        r = c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        r = c.get("/api/stack?type=data_package", headers=_auth(seeded_app["analyst_token"]))
        items = r.json()["items"]
        match = next(it for it in items if it["id"] == pkg_id)
        assert match["materialized"] is True
