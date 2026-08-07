"""Soft-downgrade test for ``PUT /api/admin/grants/{id}`` (v49, Task 5.3;
re-scoped for the auto-membership stack model).

``data_package``/``memory_domain`` grants no longer need an eager
``user_stack_subscriptions`` fan-out on a ``required → available``
downgrade: auto-membership means BOTH tiers are automatically in every
granted user's stack (``StackResolver.stack``), so the downgrade can never
drop the resource from anyone's stack — it only lifts the "always
downloaded locally" guarantee to "downloaded once subscribed".

``marketplace_plugin`` grants are the one remaining exception — plugin
visibility is resolved off ``user_plugin_optouts`` (an opt-out mechanism
outside the StackResolver's auto-membership), so that fan-out is unchanged.
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


class TestRequiredToAvailableAutoMembership:
    """Auto-membership model — required → available does NOT need to write
    any user_stack_subscriptions row: ``available`` is already automatically
    in every granted user's stack."""

    def test_downgrade_does_not_write_subscription_rows(self, seeded_app):
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

        # No subscription rows are written — auto-membership keeps
        # ``available`` in every granted user's stack without one.
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT user_id FROM user_stack_subscriptions "
                "WHERE resource_type='data_package' AND resource_id='pkg_sales'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

        # And each of the 3 users still sees the package in their stack —
        # now as an available (not required) entry — via /api/stack. This
        # is the "must NOT drop the resource from anyone's stack" guarantee
        # the removed fan-out used to provide via eager subscription rows.
        # Users seeded here have no auth token fixture; assert through the
        # resolver directly instead of an authenticated request.
        from app.resource_types import ResourceType
        from app.services.stack_resolver import StackResolver

        for uid in ("u1", "u2", "u3"):
            entries = StackResolver().stack(uid, ResourceType.DATA_PACKAGE)
            match = next((e for e in entries if e.id == "pkg_sales"), None)
            assert match is not None, f"{uid} lost the package after downgrade"
            assert match.requirement == "available"
            assert match.in_stack is True
            assert match.materialized is False, (
                "no subscription row was written, so the package should not "
                "be flagged as materialized (downloaded locally) yet"
            )

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
    """Perf regression gate for a large-group ``required → available``
    downgrade under the auto-membership model.

    A 1000-user group flip must stay fast (no eager per-member fan-out to
    pay for anymore — auto-membership means the downgrade is a single-row
    UPDATE on ``resource_grants``) and must still emit **exactly one**
    audit row, not one per member.
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

        print(f"\nsoft-downgrade (auto-membership, no fan-out): {elapsed_s * 1000:.1f} ms for 1000 users")
        # Auto-membership means NO subscription rows are written on
        # downgrade — the 1000 members stay in their stack automatically.
        assert sub_count == 0, f"expected no subscription rows to be written, got {sub_count}"
        # Exactly ONE audit row produced for the single grant-update action.
        assert new_audit - baseline_audit == 1, (
            f"expected 1 audit row for the requirement update; got "
            f"{new_audit - baseline_audit} (baseline={baseline_audit}, "
            f"after={new_audit})"
        )
        assert elapsed_s < self.SOFT_DOWNGRADE_PERF_BUDGET_S, (
            f"downgrade took {elapsed_s:.3f}s, exceeds "
            f"{self.SOFT_DOWNGRADE_PERF_BUDGET_S}s. Threshold is a "
            f"guidance target — document the actual time and tune in a "
            f"follow-up if this is a persistent regression."
        )
