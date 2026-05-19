"""Regression test for POST /api/admin/grants requirement field.

The v49 ``resource_grants.requirement`` enum (``available`` / ``required``)
must round-trip through ``POST /api/admin/grants`` so the inline RBAC
matrices on /admin/tables, /admin/corporate-memory, and /catalog
(Edit Data Package / Edit Memory Domain / Edit Recipe) can set a grant
to ``required`` in one round-trip.

Prior to the fix, ``CreateGrantRequest`` did not declare ``requirement``
so the field was silently dropped — the new grant landed at the DB
column default (``available``), and a re-open of the same modal showed
the admin's ``required`` pick as ``available``. Looked like the save
failed silently.
"""

from __future__ import annotations

import uuid

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_group(name: str = "TestGroup") -> str:
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(
        name=name, description="", created_by="test",
    )
    gid = g["id"] if isinstance(g, dict) else g
    conn.close()
    return gid


def _seed_data_package(slug: str = "test-pkg") -> str:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pid = DataPackagesRepository(conn).create(
        name="Test", slug=slug, description=None,
        icon=None, color=None, created_by="test",
    )
    conn.close()
    return pid


class TestPostGrantRequirement:
    def test_post_default_lands_at_available(self, seeded_app):
        """Omitting ``requirement`` keeps the legacy column default."""
        gid = _seed_group(f"G{uuid.uuid4().hex[:8]}")
        pid = _seed_data_package(f"p-{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].post(
            "/api/admin/grants",
            json={
                "group_id": gid,
                "resource_type": "data_package",
                "resource_id": pid,
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
        assert r.json()["requirement"] == "available"

    def test_post_with_required_persists(self, seeded_app):
        """The matrix sends ``requirement: 'required'`` — endpoint must
        plumb it through to the repo so the DB row reflects it."""
        gid = _seed_group(f"G{uuid.uuid4().hex[:8]}")
        pid = _seed_data_package(f"p-{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].post(
            "/api/admin/grants",
            json={
                "group_id": gid,
                "resource_type": "data_package",
                "resource_id": pid,
                "requirement": "required",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["requirement"] == "required"

        # Confirm GET /api/admin/grants surfaces the same value (rules out
        # response-shape-only fixes that don't actually persist).
        r2 = seeded_app["client"].get(
            "/api/admin/grants?resource_type=data_package",
            headers=_auth(seeded_app["admin_token"]),
        )
        match = next(
            (g for g in r2.json() if g["resource_id"] == pid),
            None,
        )
        assert match is not None
        assert match["requirement"] == "required"

    def test_post_with_explicit_available_persists(self, seeded_app):
        """Symmetric — explicit ``'available'`` still works."""
        gid = _seed_group(f"G{uuid.uuid4().hex[:8]}")
        pid = _seed_data_package(f"p-{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].post(
            "/api/admin/grants",
            json={
                "group_id": gid,
                "resource_type": "data_package",
                "resource_id": pid,
                "requirement": "available",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201
        assert r.json()["requirement"] == "available"

    def test_post_with_invalid_requirement_rejected(self, seeded_app):
        """The enum is the source of truth — anything else → 422."""
        gid = _seed_group(f"G{uuid.uuid4().hex[:8]}")
        pid = _seed_data_package(f"p-{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].post(
            "/api/admin/grants",
            json={
                "group_id": gid,
                "resource_type": "data_package",
                "resource_id": pid,
                "requirement": "mandatory",  # legacy term — not in the enum
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422
