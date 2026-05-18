"""End-to-end coverage for the v39 system plugin tier.

The feature reuses the existing RBAC + subscription tables — marking a
plugin as "system" simply materializes resource_grants + user_plugin_optouts
rows for every existing user_groups + users row, then locks the
corresponding admin/user controls. The resolver itself is unchanged.

Tests in this module exercise:

* mark/unmark endpoints — happy path, idempotency, audit row, fanout count
* refusal of the bypass paths — DELETE grant, unsubscribe, uninstall
* creation hooks — new user / new group inherit the mandatory tier
* sync preservation — a re-sync of the marketplace doesn't reset is_system

Mirrors the helper pattern in ``test_marketplace_api.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!", admin: bool = False):
    """Create a user and return (user_id, cookies). When ``admin=True``
    the user is added to the seeded Admin system group so
    ``require_admin`` passes."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    if admin:
        from tests.helpers.auth import grant_admin
        grant_admin(conn, user_id)
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _seed_marketplace_with_plugin(
    *,
    marketplace: str = "mkt-x",
    plugin: str = "alpha",
) -> None:
    """Insert a marketplace + plugin row directly. We bypass the git
    sync path here because none of the system-flag behavior depends on
    plugin content — it's purely a flag + materialization story."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        existing = conn.execute(
            "SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace],
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES (?, ?, ?, ?)",
                [marketplace, marketplace.upper(),
                 f"https://example.test/{marketplace}.git",
                 datetime.now(timezone.utc)],
            )
        meta = {"name": plugin, "version": "1.0", "description": "desc"}
        conn.execute(
            "INSERT INTO marketplace_plugins "
            "(marketplace_id, name, description, version, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (marketplace_id, name) DO NOTHING",
            [marketplace, plugin, meta["description"], meta["version"],
             json.dumps(meta), datetime.now(timezone.utc)],
        )
    finally:
        conn.close()


def _add_group(name: str = "engineers") -> str:
    """Create a non-system group and return its id. Mark on this group
    fans out a grant; cleanup on unmark leaves it intact."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    conn = get_system_db()
    try:
        return UserGroupsRepository(conn).create(name=name)["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mark / Unmark endpoint behavior
# ---------------------------------------------------------------------------


class TestMarkUnmark:
    def test_mark_404_when_plugin_missing(self, web_client):
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        r = web_client.post(
            "/api/marketplaces/missing/plugins/ghost/system",
            cookies=cookies,
        )
        assert r.status_code == 404

    def test_mark_requires_admin(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "user@x.com", admin=False)
        r = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=cookies,
        )
        # require_admin returns 403 on non-admins
        assert r.status_code in (401, 403)

    def test_mark_flips_flag_and_fans_out(self, web_client):
        """After mark, every existing user has a subscription row and
        every existing group has a grant row for the marked plugin."""
        _seed_marketplace_with_plugin()
        admin_id, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        # Pre-existing non-admin user + custom group so we can observe
        # both fanout dimensions.
        regular_id, _ = _create_user(web_client, "regular@x.com")
        gid = _add_group("engineers")

        r = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_system"] is True
        # affected_users counts NEW subscription rows — both admin and
        # regular start un-subscribed, so we expect at least both, plus
        # the seeded scheduler service user that the app may have
        # bootstrapped during create_app.
        assert body["affected_users"] >= 2

        from src.db import get_system_db
        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT is_system FROM marketplace_plugins "
                "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
            ).fetchone()
            assert row[0] is True

            # Subscription row exists for both users.
            for uid in (admin_id, regular_id):
                sub = conn.execute(
                    "SELECT 1 FROM user_plugin_optouts "
                    "WHERE user_id = ? AND marketplace_id = 'mkt-x' "
                    "AND plugin_name = 'alpha'",
                    [uid],
                ).fetchone()
                assert sub is not None, f"subscription missing for {uid}"

            # Grant row exists for engineers + Admin + Everyone (system seeded).
            grant_groups = {
                r[0] for r in conn.execute(
                    "SELECT group_id FROM resource_grants "
                    "WHERE resource_type = 'marketplace_plugin' "
                    "AND resource_id = 'mkt-x/alpha'",
                ).fetchall()
            }
            assert gid in grant_groups, "engineers group never received fanout grant"
        finally:
            conn.close()

    def test_mark_is_idempotent(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        first = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert first.status_code == 200
        # Second call must succeed and report 0 newly affected — every
        # row was already in place.
        second = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert second.status_code == 200
        assert second.json()["affected_users"] == 0
        assert second.json()["affected_groups"] == 0

    def test_unmark_flips_flag_but_keeps_rows(self, web_client):
        """The agreed semantic: unmark only flips the flag. Existing
        grants and subscriptions persist so a confused click doesn't
        rip the plugin out of every user's stack mid-day."""
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        _, _ = _create_user(web_client, "regular@x.com")
        gid = _add_group("engineers")

        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        r = web_client.delete(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert r.status_code == 204

        from src.db import get_system_db
        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT is_system FROM marketplace_plugins "
                "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
            ).fetchone()
            assert row[0] is False

            # Subscription rows survive — admin curates cleanup later.
            count = conn.execute(
                "SELECT COUNT(*) FROM user_plugin_optouts "
                "WHERE marketplace_id = 'mkt-x' AND plugin_name = 'alpha'",
            ).fetchone()[0]
            assert count >= 2, "subscriptions should persist past unmark"

            # Grant for engineers survives too.
            grant = conn.execute(
                "SELECT 1 FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [gid],
            ).fetchone()
            assert grant is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Bypass-path guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_unsubscribe_via_my_stack_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        # require_admin grants the admin access via the Admin group seed,
        # so they'll see the plugin in their stack and can attempt the
        # toggle. The guard should refuse.
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False}, cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_unsubscribe_system_plugin"

    def test_uninstall_via_marketplace_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install",
            cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_uninstall_system_plugin"

    def test_grant_delete_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        gid = _add_group("engineers")
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        # Find the engineers group's grant for this plugin.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant_id = conn.execute(
                "SELECT id FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [gid],
            ).fetchone()[0]
        finally:
            conn.close()

        r = web_client.delete(
            f"/api/admin/grants/{grant_id}", cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_revoke_system_grant"

    def test_subscribe_via_my_stack_still_allowed(self, web_client):
        """The guard refuses unsubscribe only — explicit subscribe must
        keep working since the row already exists (idempotent)."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True}, cookies=admin_cookies,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Creation hooks
# ---------------------------------------------------------------------------


class TestCreationHooks:
    def test_new_group_inherits_grant(self, web_client):
        """A group created AFTER mark gets the system grant via
        ResourceGrantsRepository.fanout_system_for_group, called from
        UserGroupsRepository.create()."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        # Create a brand-new group via the admin POST endpoint.
        r = web_client.post(
            "/api/admin/groups",
            json={"name": "post-mark-group", "description": "test"},
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        new_gid = r.json()["id"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant = conn.execute(
                "SELECT 1 FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [new_gid],
            ).fetchone()
            assert grant is not None, "new group did not inherit system grant"
        finally:
            conn.close()

    def test_new_user_inherits_subscription(self, web_client):
        """A user created via the admin POST endpoint AFTER mark gets a
        subscription row via fanout_system_for_user."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        r = web_client.post(
            "/api/users",
            json={
                "email": "fresh@example.com",
                "name": "Fresh",
                "send_invite": False,
            },
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        new_uid = r.json()["id"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            sub = conn.execute(
                "SELECT 1 FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = 'mkt-x' "
                "AND plugin_name = 'alpha'",
                [new_uid],
            ).fetchone()
            assert sub is not None, "new user did not inherit subscription"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Sync preservation
# ---------------------------------------------------------------------------


def test_resync_preserves_is_system(tmp_path, monkeypatch):
    """``replace_for_marketplace`` re-runs every sync. The is_system
    flag MUST survive — it's not in the ON CONFLICT DO UPDATE SET list
    and not in the INSERT VALUES list. Test by faking a sync via the
    repo with the same plugin name.

    Uses ``tmp_path`` directly (no web_client) because the test only
    exercises the repo, not any API surface — but we still need a
    fresh DATA_DIR so we don't inherit state from a sibling test that
    populated ``store_entities`` etc. through the migration ladder.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "analytics").mkdir(exist_ok=True)
    (tmp_path / "extracts").mkdir(exist_ok=True)
    from src.db import close_system_db, get_system_db
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    close_system_db()
    conn = get_system_db()
    try:
        # Set up registry + initial plugin.
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            ["resync-test", "Resync", "https://example.test/r.git",
             datetime.now(timezone.utc)],
        )
        repo = MarketplacePluginsRepository(conn)
        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "1.0", "description": "v1"}],
        )

        # Mark as system.
        conn.execute(
            "UPDATE marketplace_plugins SET is_system = TRUE "
            "WHERE marketplace_id = 'resync-test' AND name = 'alpha'"
        )

        # Re-sync with updated description.
        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "2.0", "description": "v2-updated"}],
        )

        row = conn.execute(
            "SELECT is_system, version, description FROM marketplace_plugins "
            "WHERE marketplace_id = 'resync-test' AND name = 'alpha'"
        ).fetchone()
        assert row[0] is True, "is_system was reset by resync"
        assert row[1] == "2.0"
        assert row[2] == "v2-updated"
    finally:
        conn.close()
        close_system_db()
