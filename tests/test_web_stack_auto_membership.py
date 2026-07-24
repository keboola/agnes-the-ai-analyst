"""Web-UI coverage for the auto-membership stack model (rail layout).

Every RBAC-granted data package / memory domain is now automatically in
the caller's stack — the Add/Remove toggle on /catalog and /stack no
longer controls membership, it controls the LOCAL DOWNLOAD state
(``mode: 'download'`` on the shared catalog_card component). These tests
pin the rendered wording + state transition end to end through the real
subscribe/unsubscribe endpoints.
"""

from __future__ import annotations

import uuid


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


class TestDownloadToggleWording:
    def test_my_stack_renders_download_locally_for_granted_not_downloaded(self, seeded_app, monkeypatch):
        """A granted-but-not-downloaded package is already in the caller's
        stack (auto-membership) and shows on My Stack with a 'Download
        locally' toggle — not 'Add to stack', which now only labels
        Marketplace install/uninstall. Post-reshape the Catalog excludes
        anything already in the caller's stack, so this wording lives on
        My Stack, not the Catalog."""
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
        # ...and on My Stack with the Download-locally toggle.
        resp = c.get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Auto Member Pkg" in body
        assert "Download locally" in body
        assert 'data-toggle-kind="download"' in body

    def test_subscribe_flips_to_downloaded_state(self, seeded_app, monkeypatch):
        """After POST /api/stack/subscribe the card renders the
        'Downloaded' / 'Remove local copy' state instead — on /stack,
        where a fully-materialized package now lives (the Catalog reshape
        excludes anything already in the caller's stack from the Data
        grid, and a materialized package also drops out of the
        Catalog's "Recommended for you" row since there's nothing left
        to nudge the user to download)."""
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
        assert "Downloaded" in body
        assert "Remove local copy" in body

    def test_my_stack_lists_granted_but_not_downloaded_package(self, seeded_app, monkeypatch):
        """My Stack shows the package even before it's subscribed — it's
        already in the stack via auto-membership, just not downloaded."""
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
        assert "Download locally" in body


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
