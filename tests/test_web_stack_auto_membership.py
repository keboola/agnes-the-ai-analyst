"""API coverage for the auto-membership stack model.

Every RBAC-granted data package / memory domain is automatically in the
caller's stack. The standalone My Stack page (which used to render this as a
locked *Required* group vs. an overflow-removable *Added by you* group) is
retired — folded into the Library (#1088; /stack now 302s to
/library?stack=in_stack). The Library's OWN auto-membership rendering is not
a byte-for-byte port of My Stack's: droppability is keyed purely on
membership mode there (auto-membership never offers Remove, for ANY tier —
see ``tests/test_web_library.py::test_library_available_grant_reads_in_stack_and_offers_no_toggle``),
where My Stack additionally offered Remove for non-required grants. That
Library behavior is the current product truth and is covered in
``tests/test_web_library.py``; nothing from this module's retired
``TestAddedByYouGroup`` was carried forward, since re-asserting My Stack's
wording against Library would just pin a UI that no longer exists.

What remains here is genuinely API-level and orthogonal to which page (if
any) renders it — the subscribe/unsubscribe API is unchanged.
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
