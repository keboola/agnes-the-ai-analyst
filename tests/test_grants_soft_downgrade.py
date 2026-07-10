"""Soft-downgrade test for ``PUT /api/admin/grants/{id}`` (v49, Task 5.3).

When an admin flips a grant from ``required`` → ``available``, the API
eagerly materializes ``user_stack_subscriptions`` rows for every user in
the granted group so the resource stays in their stack. Without this,
users would silently lose access on the next refresh (a UX regression
the design doc D11 explicitly avoids).
"""

import uuid


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
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'admin')",
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
            "INSERT INTO user_groups(id, name, description, created_by) VALUES ('g_sales', 'Sales', 'test', 'test')"
        )
        for uid in ("u1", "u2", "u3"):
            conn.execute(
                "INSERT INTO users(id, email) VALUES (?, ?)",
                [uid, f"{uid}@x.test"],
            )
            _add_user_to_group(conn, uid, "g_sales")
        # Seed an existing data package + a required grant for it
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('pkg_sales', 'sales', 'Sales bundle')")
        grant_id = _seed_grant(
            conn,
            "g_sales",
            "data_package",
            "pkg_sales",
            "required",
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
            "INSERT INTO user_groups(id, name, description, created_by) VALUES ('g_eng', 'Eng', 'test', 'test')"
        )
        conn.execute("INSERT INTO users(id, email) VALUES ('u_eng', 'u_eng@x.test')")
        _add_user_to_group(conn, "u_eng", "g_eng")
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('pkg_eng', 'eng', 'Eng bundle')")
        grant_id = _seed_grant(
            conn,
            "g_eng",
            "data_package",
            "pkg_eng",
            "available",
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
            cnt = conn.execute("SELECT COUNT(*) FROM user_stack_subscriptions WHERE resource_id='pkg_eng'").fetchone()[
                0
            ]
        finally:
            conn.close()
        assert cnt == 0

    def test_nochange_is_noop(self, seeded_app):
        """PUT with the current value does nothing — no error, no spurious rows."""
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute("INSERT INTO user_groups(id, name, description, created_by) VALUES ('g_x', 'X', 'test', 'test')")
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('pkg_x', 'x', 'X bundle')")
        grant_id = _seed_grant(
            conn,
            "g_x",
            "data_package",
            "pkg_x",
            "available",
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


class TestMarketplacePluginSoftDowngrade:
    """marketplace_plugin grants fan out into ``user_plugin_optouts`` — the
    subscription table ``resolve_user_marketplace`` reads — NOT into
    ``user_stack_subscriptions``. Without this, a required → available flip
    silently dropped the plugin from every group member's served set."""

    def test_downgrade_materializes_plugin_subscriptions(self, seeded_app):
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) VALUES ('g_plug', 'Pluggers', 'test', 'test')"
        )
        for uid in ("pu1", "pu2"):
            conn.execute(
                "INSERT INTO users(id, email) VALUES (?, ?)",
                [uid, f"{uid}@x.test"],
            )
            _add_user_to_group(conn, uid, "g_plug")
        grant_id = _seed_grant(
            conn,
            "g_plug",
            "marketplace_plugin",
            "mkt/p1",
            "required",
        )
        conn.close()

        c = seeded_app["client"]
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        try:
            subs = conn.execute(
                "SELECT user_id FROM user_plugin_optouts WHERE marketplace_id='mkt' AND plugin_name='p1'"
            ).fetchall()
            stack_rows = conn.execute(
                "SELECT COUNT(*) FROM user_stack_subscriptions WHERE resource_type='marketplace_plugin'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert {row[0] for row in subs} == {"pu1", "pu2"}
        # No dead rows in the stack table — plugins don't live there.
        assert stack_rows == 0


class TestSoftDowngradePerf:
    """Section 4.5 perf gate (Phase 9 / Task 9.5).

    1000-user group flipped from ``required`` → ``available`` MUST
    materialize all 1000 ``user_stack_subscriptions`` rows inside a
    single DuckDB transaction in under 1 second, and emit **exactly
    one** audit row (not 1000) — the per-user fan-out is part of the
    same admin action, not a sequence of separate operations.
    """

    SOFT_DOWNGRADE_PERF_BUDGET_S = float(
        # Allow operators to dial the threshold via env without a code
        # change — useful when the suite runs on a heavily-loaded shared
        # box and the 1s target is too tight for one transient run.
        __import__("os").environ.get("AGNES_PERF_SOFT_DOWNGRADE_S", "1.0")
    )

    def test_thousand_user_downgrade_under_one_second_single_audit(
        self,
        seeded_app,
    ):
        import time as _time
        from src.db import get_system_db

        conn = get_system_db()

        # Group + 1000 users + memberships. The downgrade fan-out is a
        # ``INSERT INTO user_stack_subscriptions ... SELECT m.user_id ...
        # WHERE m.group_id = ?`` — so the cost is dominated by the JOIN
        # and the constraint check, not by individual Python writes.
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) VALUES ('g_perf', 'PerfGroup', '', 'test')"
        )
        for i in range(1000):
            uid = f"uperf_{i:04d}"
            conn.execute(
                "INSERT INTO users(id, email) VALUES (?, ?)",
                [uid, f"{uid}@x.test"],
            )
            conn.execute(
                "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, 'g_perf', 'test')",
                [uid],
            )
        conn.execute("INSERT INTO data_packages(id, slug, name) VALUES ('pkg_perf', 'pkg-perf', 'PerfPkg')")
        grant_id = _seed_grant(
            conn,
            "g_perf",
            "data_package",
            "pkg_perf",
            "required",
        )
        # Baseline audit row count so we can isolate the rows produced by
        # the soft-downgrade alone. Older test fixtures may have seeded
        # other audit lines via the seeded_app setup (admin login bumps,
        # etc.) — measure delta, not absolute count.
        baseline_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?",
            ["resource_grant.requirement_updated"],
        ).fetchone()[0]
        conn.close()

        c = seeded_app["client"]
        t0 = _time.perf_counter()
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        elapsed_s = _time.perf_counter() - t0
        assert r.status_code == 200, r.text

        conn = get_system_db()
        try:
            sub_count = conn.execute(
                "SELECT COUNT(*) FROM user_stack_subscriptions "
                "WHERE resource_type='data_package' AND resource_id='pkg_perf'"
            ).fetchone()[0]
            new_audit = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = ?",
                ["resource_grant.requirement_updated"],
            ).fetchone()[0]
        finally:
            conn.close()

        print(f"\nsoft-downgrade fan-out: {elapsed_s * 1000:.1f} ms for 1000 users")
        assert sub_count == 1000, f"expected 1000 subscription rows materialized, got {sub_count}"
        # Exactly ONE audit row produced — the per-user fan-out is bundled
        # into a single admin action audit line.
        assert new_audit - baseline_audit == 1, (
            f"expected 1 audit row for the requirement update; got "
            f"{new_audit - baseline_audit} (baseline={baseline_audit}, "
            f"after={new_audit})"
        )
        assert elapsed_s < self.SOFT_DOWNGRADE_PERF_BUDGET_S, (
            f"soft-downgrade fan-out took {elapsed_s:.3f}s, exceeds "
            f"{self.SOFT_DOWNGRADE_PERF_BUDGET_S}s. Threshold is a "
            f"guidance target — document the actual time and tune in a "
            f"follow-up if this is a persistent regression."
        )
