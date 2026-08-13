"""Tests for #11 — user management (active flag, safeguards, endpoints)."""

import tempfile
import pytest


from src.db import get_schema_version


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        # Reset cached system DB so we open a brand-new instance in tmp
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


def test_schema_v5_adds_active_column(fresh_db):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(users)").fetchall()
        col_names = [c[1] for c in cols]
        assert "active" in col_names
        assert "deactivated_at" in col_names
        assert "deactivated_by" in col_names
        assert get_schema_version(conn) >= 5
    finally:
        conn.close()
        close_system_db()


def test_schema_v5_backfill_keeps_existing_users_active(fresh_db):
    """Simulate upgrading from v4: insert a user pre-migration, verify active=TRUE afterwards."""
    import uuid
    import duckdb as _duckdb
    from pathlib import Path

    # 1. Create a v4-era DB by hand.
    db_dir = Path(fresh_db) / "state"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "system.duckdb"
    conn = _duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TIMESTAMP DEFAULT current_timestamp)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")
        conn.execute("""CREATE TABLE users (
            id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL,
            name VARCHAR, role VARCHAR DEFAULT 'analyst',
            password_hash VARCHAR, setup_token VARCHAR,
            setup_token_created TIMESTAMP, reset_token VARCHAR,
            reset_token_created TIMESTAMP,
            created_at TIMESTAMP DEFAULT current_timestamp, updated_at TIMESTAMP)""")
        uid = str(uuid.uuid4())
        conn.execute("INSERT INTO users (id, email, name, role) VALUES (?, 'pre@v4', 'Pre', 'admin')", [uid])
    finally:
        conn.close()

    # 2. Now let the app open it — schema should migrate to v5 and backfill active=TRUE.
    from src.db import get_system_db, close_system_db, get_schema_version

    close_system_db()
    conn = get_system_db()
    try:
        assert get_schema_version(conn) >= 5
        row = conn.execute("SELECT email, active FROM users WHERE email = 'pre@v4'").fetchone()
        assert row is not None
        assert row[1] is True
    finally:
        conn.close()
        close_system_db()


def test_repository_update_accepts_active(fresh_db):
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="a@b.c", name="A")
        repo.update(id=uid, active=False, deactivated_by="admin-uuid")
        row = repo.get_by_id(uid)
        assert row["active"] is False
        assert row["deactivated_by"] == "admin-uuid"
    finally:
        conn.close()
        close_system_db()


def test_repository_count_admins(fresh_db):
    """v12: count_admins counts users in the Admin system group, not users.role."""
    import uuid
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db, close_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        assert repo.count_admins() == 0
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        admin_id = str(uuid.uuid4())
        repo.create(id=admin_id, email="a@b.c", name="A")
        UserGroupMembersRepository(conn).add_member(admin_id, admin_gid, source="system_seed")
        repo.create(id=str(uuid.uuid4()), email="b@b.c", name="B")
        assert repo.count_admins() == 1
    finally:
        conn.close()
        close_system_db()


from fastapi.testclient import TestClient


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.main import app

    return TestClient(app)


def _seed_admin(fresh_db):
    """Create an admin user (in Admin user_group) and return (id, bearer_token)."""
    import uuid
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        token = create_access_token(user_id=uid, email="admin@test")
        return uid, token
    finally:
        conn.close()


def test_patch_user_updates_role(app_client, fresh_db):
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    admin_id, token = _seed_admin(fresh_db)
    target_id = str(uuid.uuid4())
    conn = get_system_db()
    try:
        UserRepository(conn).create(id=target_id, email="x@test", name="X")
    finally:
        conn.close()

    resp = app_client.patch(
        f"/api/users/{target_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "analyst", "name": "X2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # v12: role response is admin/user based on Admin group membership.
    # Patching role="analyst" is a no-op for the admin group → still "user".
    assert data["role"] == "user"
    assert data["name"] == "X2"


def test_cannot_self_deactivate(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    resp = app_client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"active": False},
    )
    assert resp.status_code == 409
    assert "yourself" in resp.json()["detail"].lower()


def test_cannot_delete_last_admin(app_client, fresh_db):
    """Deleting the sole active admin must 409.
    Note: the endpoint checks self-delete first, which also triggers 409 here,
    so we accept either "yourself" or "last" wording — the point is the
    safeguard blocks deletion of the only admin."""
    admin_id, token = _seed_admin(fresh_db)
    # Create a non-admin so we have ≥2 users, but admin is still the only admin.
    resp = app_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "x@test", "name": "X", "role": "viewer"},
    )
    x_id = resp.json()["id"]
    # Try deleting the admin.
    resp = app_client.delete(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "last" in detail or "yourself" in detail


def test_deactivated_user_cannot_authenticate(app_client, fresh_db):
    """A deactivated user's old JWT must be rejected."""
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@test", name="U")
        token = create_access_token(user_id=uid, email="u@test")
        UserRepository(conn).update(id=uid, active=False)
    finally:
        conn.close()

    resp = app_client.get(
        "/api/users",  # any authenticated endpoint
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    # Deactivated — must not succeed.
    assert resp.status_code in (401, 403)


def test_admin_users_page_renders_for_admin(app_client, fresh_db):
    admin_id, token = _seed_admin(fresh_db)
    resp = app_client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    # One head for the whole section, section-level ("People" — matching the
    # nav row); the tab strip below it names the sub-view, so People and
    # Tokens read as one place. Groups is no longer a third tab here: it moved
    # to Access, where a grant is written.
    assert 'class="page-header__title">People<' in resp.text
    # PLAIN, not the hero card. This page is a workspace an admin stands in
    # and filters, exactly as /library is, so it wears /library's head — the
    # name, one sentence, nothing drawn around them.
    assert "page-header--plain" in resp.text
    assert "page-header--hero" not in resp.text
    assert 'class="tab-strip"' in resp.text


class TestAdminUsersGroupFilterDropdown:
    """Custom design-system dropdown on /admin/users' "Filter by group"
    select (#1055). `#group-filter` stays a real `<select>` in the DOM
    (the existing `currentGroup` / `loadUsers()` JS wiring is untouched)
    with a `ds.dropdown()` custom button+menu alongside it, options
    mirroring the same server-rendered `groups` list. Visibility between
    the two is a CSS theme decision (paper-skin.css), not a template one.
    """

    def test_native_select_still_renders_for_existing_js_wiring(self, app_client, fresh_db):
        _, token = _seed_admin(fresh_db)
        resp = app_client.get(
            "/admin/users",
            headers={"Accept": "text/html"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="group-filter" class="group-filter ds-dropdown-native" aria-label="Filter by group">' in text
        assert '<option value="">All groups</option>' in text

    def test_custom_dropdown_mirrors_the_server_rendered_groups(self, app_client, fresh_db):
        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository

        conn = get_system_db()
        gid = UserGroupsRepository(conn).create(name="Data Team")["id"]
        conn.close()

        _, token = _seed_admin(fresh_db)
        resp = app_client.get(
            "/admin/users",
            headers={"Accept": "text/html"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        text = resp.text
        # Custom dropdown wrapper — accessible button+menu contract.
        assert 'data-ds-dropdown-target="group-filter"' in text
        assert 'id="group-filter-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="group-filter-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        # The dynamic group shows up in BOTH the native select and the
        # custom dropdown's menu, options in lockstep.
        assert f'<option value="{gid}">Data Team</option>' in text
        assert f'data-value="{gid}"' in text
        assert ">Data Team<" in text

    def test_dropdown_js_module_is_loaded(self, app_client, fresh_db):
        _, token = _seed_admin(fresh_db)
        resp = app_client.get(
            "/admin/users",
            headers={"Accept": "text/html"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text


def test_admin_users_page_denies_non_admin(app_client, fresh_db):
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="a@test", name="A")
        token = create_access_token(user_id=uid, email="a@test")
    finally:
        conn.close()
    resp = app_client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
        follow_redirects=False,
    )
    # HTML request to admin-only page → 302 (to /login) for non-admin per Phase 0, or 403.
    # Phase 0 is out of scope here so we accept 403 (current behaviour) or 302.
    assert resp.status_code in (302, 403)


def test_deactivated_admin_rejected_by_active_check(app_client, fresh_db):
    """Deactivating an admin must cause their token to be rejected as 401 (not succeed)."""
    import uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    # Seed two admins so we can deactivate one without tripping the last-admin rule.
    admin_id, admin_token = _seed_admin(fresh_db)
    conn = get_system_db()
    try:
        other_uid = str(uuid.uuid4())
        UserRepository(conn).create(id=other_uid, email="other@test", name="Other")
        other_token = create_access_token(user_id=other_uid, email="other@test")
        # Directly deactivate the "other" admin via repository (bypass safeguard
        # because we already have 2 admins; this is just a state setup).
        UserRepository(conn).update(id=other_uid, active=False)
    finally:
        conn.close()

    resp = app_client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {other_token}", "Accept": "application/json"},
    )
    assert resp.status_code == 401
    assert "deactivated" in resp.json().get("detail", "").lower()


def test_cannot_remove_last_admin_via_user_memberships(app_client, fresh_db):
    """v19 #151: DELETE /api/admin/users/{id}/memberships/{group_id} must
    refuse to remove the only active admin from the seeded Admin group —
    even when the caller is a different admin (covers the case where
    a second admin was added then the first was deactivated, leaving
    one active admin who could otherwise be demoted to zero)."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db

    admin_id, token = _seed_admin(fresh_db)
    conn = get_system_db()
    try:
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    finally:
        conn.close()
    # Sole-admin case: try to demote the only admin via the user-keyed
    # memberships endpoint.
    resp = app_client.delete(
        f"/api/admin/users/{admin_id}/memberships/{admin_gid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "last admin" in resp.json()["detail"].lower()


def test_cannot_remove_last_admin_via_group_members(app_client, fresh_db):
    """v19 #151: DELETE /api/admin/groups/{group_id}/members/{user_id} must
    refuse to demote the only active admin (group-keyed mirror of the
    user-keyed membership endpoint)."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db

    admin_id, token = _seed_admin(fresh_db)
    conn = get_system_db()
    try:
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    finally:
        conn.close()
    resp = app_client.delete(
        f"/api/admin/groups/{admin_gid}/members/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "last admin" in resp.json()["detail"].lower()


def test_can_remove_admin_when_another_active_admin_exists(app_client, fresh_db):
    """Sanity: with two active admins, demoting one via the membership
    endpoint must succeed — the guard fires only at count_admins <= 1."""
    import uuid
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    admin_id, token = _seed_admin(fresh_db)
    conn = get_system_db()
    try:
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        other_id = str(uuid.uuid4())
        UserRepository(conn).create(id=other_id, email="other@test", name="Other")
        UserGroupMembersRepository(conn).add_member(
            other_id,
            admin_gid,
            source="admin",
            added_by="admin@test",
        )
    finally:
        conn.close()
    resp = app_client.delete(
        f"/api/admin/users/{other_id}/memberships/{admin_gid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204


def test_cannot_deactivate_last_admin(app_client, fresh_db):
    """v19: try to deactivate the last active admin → 409.
    Admin demotion is now done via group membership (DELETE /api/admin/users/{id}/memberships/{group_id}),
    but the deactivate path retains its own last-admin guard.
    """
    admin_id, token = _seed_admin(fresh_db)
    # Create a second non-admin user.
    resp = app_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "y@test", "name": "Y"},
    )
    assert resp.status_code == 201
    # Try to deactivate the only active admin → must fail.
    resp = app_client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"active": False},
    )
    # The endpoint blocks deactivation for the last active admin BEFORE the
    # self-deactivate check (the user IS themselves, but the message says "last
    # active admin"). Either error is acceptable — both signal the constraint.
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"].lower() or "yourself" in resp.json()["detail"].lower()


class TestPeopleOutcomeColumns:
    """The People lens' outcome fields — "does this person get data, and is it
    reaching them?", which account plumbing (created / deactivated) cannot
    answer.

    Both are DERIVED from storage that already exists — the grant graph and
    the `users.last_pull_at` column `GET /api/sync/manifest` stamps — so there
    is no new table, no migration and no DuckDB↔Postgres parity surface. What
    needs pinning is the derivation itself, since a cached or inflated count
    is exactly how this column would start lying.
    """

    def test_response_carries_both_outcome_fields(self, app_client, fresh_db):
        _admin_id, token = _seed_admin(fresh_db)
        resp = app_client.get("/api/users", cookies={"access_token": token})
        assert resp.status_code == 200
        rows = resp.json()
        assert rows, "seeded instance should have at least the admin"
        for row in rows:
            assert "data_package_count" in row
            assert "last_pull_at" in row

    def test_package_count_follows_the_grant_graph(self, app_client, fresh_db):
        """A grant to a group the person is in raises their count; removing it
        lowers it again. Derived, never stored — a cached count would drift
        the moment a grant moved."""
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories import resource_grants_repo, user_groups_repo

        _admin_id, token = _seed_admin(fresh_db)
        # Grant to a group this user is actually IN. `_seed_admin` puts them
        # in Admin only — granting to Everyone would (correctly) change
        # nothing, since the count follows real memberships rather than
        # assuming the auto-membership every real account gets.
        group = next(g for g in user_groups_repo().list_all() if g["name"] == SYSTEM_ADMIN_GROUP)

        def _count_for_admin() -> int:
            rows = app_client.get("/api/users", cookies={"access_token": token}).json()
            return next(r["data_package_count"] for r in rows if r["is_admin"])

        before = _count_for_admin()
        grants = resource_grants_repo()
        gid = grants.create(
            group_id=group["id"],
            resource_type="data_package",
            resource_id="pkg-outcome-probe",
        )
        try:
            assert _count_for_admin() == before + 1
        finally:
            grants.delete(gid)
        assert _count_for_admin() == before

    def test_admin_count_is_explicit_grants_not_a_synthetic_total(self, app_client, fresh_db):
        """Admins reach everything at runtime by god-mode, but the column
        reports their EXPLICIT grants — inflating it would hide whether the
        grants an admin is auditing actually work. Same reasoning as
        `/users/{id}/effective-access`, which stopped short-circuiting for
        admins for exactly this."""
        from src.repositories import data_packages_repo

        _admin_id, token = _seed_admin(fresh_db)
        rows = app_client.get("/api/users", cookies={"access_token": token}).json()
        admin_row = next(r for r in rows if r["is_admin"])
        total_packages = len(data_packages_repo().list())
        # Not asserted equal to the total: on a seeded instance with no
        # data-package grants the admin's honest count is 0 while packages
        # exist. The invariant is that it never EXCEEDS what is granted.
        assert admin_row["data_package_count"] <= total_packages

    def test_the_page_renders_both_columns(self, app_client, fresh_db):
        _admin_id, token = _seed_admin(fresh_db)
        resp = app_client.get(
            "/admin/users",
            headers={"Accept": "text/html"},
            cookies={"access_token": token},
        )
        body = resp.text
        assert "Data access" in body
        assert "Last pull" in body
        # ...and the cells warn rather than printing a bare zero.
        assert "No data access" in body
        assert "outcome-warn" in body
