"""Soft-downgrade test for ``PUT /api/admin/grants/{id}`` (v49, Task 5.3).

When an admin flips a grant from ``required`` → ``available``, the API
eagerly materializes ``user_stack_subscriptions`` rows for every user in
the granted group so the resource stays in their stack. Without this,
users would silently lose access on the next refresh (a UX regression
the design doc D11 explicitly avoids).
"""

import uuid

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_grant(conn, group_id, resource_type, resource_id, requirement):
    """Insert a grant with explicit requirement enum value."""
    gid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [gid, group_id, resource_type, resource_id, requirement],
    )
    return gid


def _add_user_to_group(conn, user_id, group_id):
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) "
        "VALUES (?, ?, 'admin')",
        [user_id, group_id],
    )


class TestRequiredToAvailableMaterializesSubscriptions:
    """Section 4.5 of the spec — required → available eagerly inserts
    user_stack_subscriptions for every user in the group."""

    def test_downgrade_materializes_subscriptions(self, seeded_app):
        from src.db import get_system_db

        conn = get_system_db()
        # Create group + 3 users
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) "
            "VALUES ('g_sales', 'Sales', 'test', 'test')"
        )
        for uid in ("u1", "u2", "u3"):
            conn.execute(
                "INSERT INTO users(id, email) VALUES (?, ?)",
                [uid, f"{uid}@x.test"],
            )
            _add_user_to_group(conn, uid, "g_sales")
        # Seed an existing data package + a required grant for it
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) "
            "VALUES ('pkg_sales', 'sales', 'Sales bundle')"
        )
        grant_id = _seed_grant(
            conn, "g_sales", "data_package", "pkg_sales", "required",
        )
        conn.close()

        # Admin flips the grant from required → available
        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        # All 3 users now have a subscription row
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT user_id FROM user_stack_subscriptions "
                "WHERE resource_type='data_package' AND resource_id='pkg_sales'"
            ).fetchall()
        finally:
            conn.close()
        assert {r[0] for r in rows} == {"u1", "u2", "u3"}

    def test_available_to_required_does_not_materialize(self, seeded_app):
        """Going the OTHER direction (available → required) should NOT
        write subscription rows — required is the always-in-stack tier."""
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) "
            "VALUES ('g_eng', 'Eng', 'test', 'test')"
        )
        conn.execute(
            "INSERT INTO users(id, email) VALUES ('u_eng', 'u_eng@x.test')"
        )
        _add_user_to_group(conn, "u_eng", "g_eng")
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) "
            "VALUES ('pkg_eng', 'eng', 'Eng bundle')"
        )
        grant_id = _seed_grant(
            conn, "g_eng", "data_package", "pkg_eng", "available",
        )
        conn.close()

        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "required"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM user_stack_subscriptions "
                "WHERE resource_id='pkg_eng'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert cnt == 0

    def test_nochange_is_noop(self, seeded_app):
        """PUT with the current value does nothing — no error, no spurious rows."""
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) "
            "VALUES ('g_x', 'X', 'test', 'test')"
        )
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) "
            "VALUES ('pkg_x', 'x', 'X bundle')"
        )
        grant_id = _seed_grant(
            conn, "g_x", "data_package", "pkg_x", "available",
        )
        conn.close()

        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200

    def test_put_nonexistent_grant_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.put(
            "/api/admin/grants/no-such-grant",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_put_non_admin_403(self, seeded_app):
        c = seeded_app["client"]
        r = c.put(
            "/api/admin/grants/anything",
            json={"requirement": "available"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403
