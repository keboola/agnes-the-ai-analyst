"""Tests for the v8→v9 schema migration: user_role_grants, internal_roles
extensions, core.* seed, and the legacy users.role backfill.

Tests are scenario-shaped: each one stands up a synthetic v8 DB (or a fresh
DB at v9) and asserts the post-migration state. All run via the same
_ensure_schema entry point that production uses on first connect, so a
green test means the on-disk migration also works.
"""

import json
import os
import uuid

import duckdb
import pytest


@pytest.fixture
def fresh_data_dir(tmp_path, monkeypatch):
    """Isolate DATA_DIR per test so the module-level connection cache in
    src.db doesn't leak v8 state between tests."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force the module to release any cached connection from a previous test.
    import src.db as _db
    if _db._system_db_conn is not None:
        try:
            _db._system_db_conn.close()
        except Exception:
            pass
        _db._system_db_conn = None
        _db._system_db_path = None
    yield tmp_path


def _v8_state(db_path) -> duckdb.DuckDBPyConnection:
    """Hand-craft a minimal v8 DB so the v8→v9 migration has something to
    operate on. Mirrors only the tables the migration touches."""
    os.makedirs(os.path.dirname(str(db_path)), exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version "
        "(version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (8)")
    conn.execute(
        """CREATE TABLE users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            name VARCHAR,
            role VARCHAR DEFAULT 'analyst',
            active BOOLEAN DEFAULT TRUE
        )"""
    )
    conn.execute(
        """CREATE TABLE internal_roles (
            id VARCHAR PRIMARY KEY,
            key VARCHAR UNIQUE NOT NULL,
            display_name VARCHAR NOT NULL,
            description TEXT,
            owner_module VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )"""
    )
    conn.execute(
        """CREATE TABLE group_mappings (
            id VARCHAR PRIMARY KEY,
            external_group_id VARCHAR NOT NULL,
            internal_role_id VARCHAR NOT NULL,
            assigned_at TIMESTAMP DEFAULT current_timestamp,
            assigned_by VARCHAR
        )"""
    )
    return conn


class TestFreshInstall:
    """Fresh DB → v9 directly via _SYSTEM_SCHEMA + INSERT version + seed."""

    def test_schema_version_is_9(self, fresh_data_dir):
        # Test name retained for git history; assertion uses SCHEMA_VERSION
        # so it survives future schema bumps (e.g. PR #72 takes v10/v11/v12).
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION
        conn = get_system_db()
        assert get_schema_version(conn) == SCHEMA_VERSION

    def test_core_roles_seeded_with_implies_hierarchy(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT key, display_name, implies, is_core "
                "FROM internal_roles WHERE is_core = true ORDER BY key"
            ).fetchall()
        }
        # All four core.* roles seeded.
        assert set(rows.keys()) == {
            "core.admin", "core.analyst", "core.km_admin", "core.viewer",
        }
        # Implies chain: admin → km_admin → analyst → viewer.
        assert json.loads(rows["core.admin"][1]) == ["core.km_admin"]
        assert json.loads(rows["core.km_admin"][1]) == ["core.analyst"]
        assert json.loads(rows["core.analyst"][1]) == ["core.viewer"]
        assert json.loads(rows["core.viewer"][1]) == []
        # is_core flag is True on all four.
        for key in rows:
            assert rows[key][2] is True, f"{key} should have is_core=true"

    def test_user_role_grants_table_exists_and_empty(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        # Smoke-query — table exists.
        result = conn.execute(
            "SELECT COUNT(*) FROM user_role_grants"
        ).fetchone()
        assert result[0] == 0


class TestV8ToV9Migration:
    """Existing v8 DB with users.role values → v9 backfill assertions."""

    def test_backfills_user_role_grants_from_legacy_role(self, fresh_data_dir):
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        # Seed one user per legacy role.
        for role_str in ("viewer", "analyst", "km_admin", "admin"):
            conn.execute(
                "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
                [str(uuid.uuid4()), f"{role_str}@example.com", role_str],
            )
        conn.close()

        # Trigger migration.
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION
        conn = get_system_db()
        assert get_schema_version(conn) == SCHEMA_VERSION

        rows = conn.execute(
            """SELECT u.email, r.key, g.source
               FROM users u
               JOIN user_role_grants g ON g.user_id = u.id
               JOIN internal_roles r ON g.internal_role_id = r.id
               ORDER BY u.email"""
        ).fetchall()
        assert rows == [
            ("admin@example.com", "core.admin", "auto-seed"),
            ("analyst@example.com", "core.analyst", "auto-seed"),
            ("km_admin@example.com", "core.km_admin", "auto-seed"),
            ("viewer@example.com", "core.viewer", "auto-seed"),
        ]

    def test_legacy_role_column_nulled_after_migration(self, fresh_data_dir):
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        conn.execute(
            "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), "admin@example.com", "admin"],
        )
        conn.close()

        from src.db import get_system_db
        conn = get_system_db()
        # users.role column still exists (DuckDB FK blocks DROP) but is NULL.
        cols = [
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            ).fetchall()
        ]
        assert "role" in cols, "legacy column kept as deprecated artifact"

        result = conn.execute(
            "SELECT role FROM users WHERE email = 'admin@example.com'"
        ).fetchone()
        assert result[0] is None, "legacy role value NULL-ed by v9 migration"

    def test_unknown_legacy_role_falls_back_to_viewer(self, fresh_data_dir):
        """A user with users.role='custom_thing' (not in the legacy enum)
        should still get a grant — fall back to core.viewer rather than
        leaving them ungranted."""
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        conn.execute(
            "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), "weird@example.com", "custom_thing"],
        )
        conn.close()

        from src.db import get_system_db
        conn = get_system_db()
        result = conn.execute(
            """SELECT r.key
               FROM user_role_grants g
               JOIN internal_roles r ON g.internal_role_id = r.id
               JOIN users u ON g.user_id = u.id
               WHERE u.email = 'weird@example.com'"""
        ).fetchone()
        assert result == ("core.viewer",)


class TestLegacyRoleHydration:
    """Regression coverage for the Devin-flagged scenario: a v8 DB upgraded
    to v9 NULLs `users.role`, which would otherwise break every callsite
    that still reads `user["role"]` (admin nav, dashboard UserInfo,
    catalog/sync admin bypass paths). `get_current_user` rehydrates the
    field via `_hydrate_legacy_role` so those callsites keep working
    without a mass refactor."""

    def test_hydration_recovers_role_from_user_role_grants(self, fresh_data_dir):
        """Post-v9, an existing admin user should still see role='admin' on
        their session-loaded user dict — even though the DB column is NULL."""
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        conn.execute(
            "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), "admin@example.com", "admin"],
        )
        conn.close()

        # Trigger migration via get_system_db.
        from src.db import get_system_db
        from app.auth.dependencies import _hydrate_legacy_role
        conn = get_system_db()

        # Reload the user the way get_current_user does — column is NULL.
        from src.repositories.users import UserRepository
        user = UserRepository(conn).get_by_email("admin@example.com")
        assert user["role"] is None, "v9 backfill leaves the column NULL"

        # Hydrate. Admin must come back with role='admin'.
        hydrated = _hydrate_legacy_role(user, conn)
        assert hydrated["role"] == "admin", (
            "post-v9 admin must hydrate back to role='admin' so existing "
            "user.get('role') == 'admin' callsites (admin nav, catalog "
            "bypass, etc.) continue to work after migration"
        )

    def test_hydration_returns_highest_grant(self, fresh_data_dir):
        """User with both core.km_admin (auto-seed) and core.admin (added
        later) should hydrate to 'admin' — the highest level wins."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.internal_roles import InternalRolesRepository
        from app.auth.dependencies import _hydrate_legacy_role
        conn = get_system_db()

        user_id = str(uuid.uuid4())
        UserRepository(conn).create(
            id=user_id, email="multi@example.com", name="Multi", role="km_admin",
        )
        # Add a second grant — admin — directly.
        admin_role = InternalRolesRepository(conn).get_by_key("core.admin")
        conn.execute(
            "INSERT INTO user_role_grants "
            "(id, user_id, internal_role_id, granted_by, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), user_id, admin_role["id"], "test", "direct"],
        )

        # Force role NULL to simulate the post-migration session reload state.
        conn.execute("UPDATE users SET role = NULL WHERE id = ?", [user_id])
        user = UserRepository(conn).get_by_id(user_id)
        assert user["role"] is None

        hydrated = _hydrate_legacy_role(user, conn)
        assert hydrated["role"] == "admin"

    def test_hydration_falls_back_to_viewer_when_no_grants(self, fresh_data_dir):
        """A user with zero core.* grants (edge case: imported via raw SQL
        without going through UserRepository.create, or grants revoked) must
        not crash — fall back to the safest enum value."""
        from src.db import get_system_db
        from app.auth.dependencies import _hydrate_legacy_role
        conn = get_system_db()

        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, role, active) VALUES (?, ?, NULL, TRUE)",
            [user_id, "lonely@example.com"],
        )
        user = {"id": user_id, "email": "lonely@example.com", "role": None}
        hydrated = _hydrate_legacy_role(user, conn)
        assert hydrated["role"] == "viewer"

    def test_hydration_ignores_stale_legacy_role_after_grant_revoke(
        self, fresh_data_dir,
    ):
        """Devin review #73: privilege-retention regression.

        Scenario: an admin downgrades a user via the new role-management
        UI. ``changeCoreRole`` JS does ``DELETE /api/admin/users/{id}/
        role-grants/{grant_id}`` followed by ``POST /api/admin/users/{id}/
        role-grants {role_key: 'core.viewer'}``. Neither endpoint touches
        the legacy ``users.role`` column, so it stays at the old value
        ('admin'). On the next request, ``_hydrate_legacy_role`` was
        previously short-circuiting on the truthy stale value — leaving
        ``user["role"] = "admin"`` even though the grants table only had
        ``core.viewer``. ``_is_admin_user_dict`` and the catalog/sync
        admin-bypass short-circuits would then silently retain elevated
        access. Fix: always re-resolve from grants, ignore the legacy
        column. This test pins the contract."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.internal_roles import InternalRolesRepository
        from src.repositories.user_role_grants import UserRoleGrantsRepository
        from app.auth.dependencies import _hydrate_legacy_role

        conn = get_system_db()

        # Step 1: create user as admin — both legacy column + grant land.
        user_id = str(uuid.uuid4())
        UserRepository(conn).create(
            id=user_id, email="ex-admin@example.com", name="ExAdmin", role="admin",
        )
        # Sanity: legacy column AND grant both populated.
        u = UserRepository(conn).get_by_id(user_id)
        assert u["role"] == "admin", "fresh create must set legacy column"
        admin_role = InternalRolesRepository(conn).get_by_key("core.admin")
        grants = UserRoleGrantsRepository(conn).list_for_user(user_id)
        admin_grant = next(g for g in grants if g["role_key"] == "core.admin")

        # Step 2: simulate "admin downgrades user via new UI" — DELETE the
        # core.admin grant directly (mimicking the role-management endpoint),
        # WITHOUT touching users.role.
        UserRoleGrantsRepository(conn).delete(admin_grant["id"])
        viewer_role = InternalRolesRepository(conn).get_by_key("core.viewer")
        conn.execute(
            "INSERT INTO user_role_grants "
            "(id, user_id, internal_role_id, granted_by, source) "
            "VALUES (?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), user_id, viewer_role["id"], "admin@x", "direct"],
        )
        # users.role is still 'admin' — exactly the stale state the bug describes.
        stale = conn.execute(
            "SELECT role FROM users WHERE id = ?", [user_id]
        ).fetchone()[0]
        assert stale == "admin", (
            "scenario only reproduces if the legacy column is left stale by "
            "the role-management endpoints — guards against an unrelated fix "
            "that also nulls the column making this test trivially pass"
        )

        # Step 3: load + hydrate exactly the way get_current_user does.
        u = UserRepository(conn).get_by_id(user_id)
        assert u["role"] == "admin", "loader returns the stale column verbatim"
        hydrated = _hydrate_legacy_role(u, conn)

        # The contract: hydration trusts the grants, NOT the stale column.
        assert hydrated["role"] == "viewer", (
            "after revoking core.admin and granting core.viewer, hydration "
            "must overwrite the stale 'admin' value — otherwise downstream "
            "is_admin checks (catalog/sync bypass, _is_admin_user_dict) keep "
            "elevated table access alive against require_internal_role's gate"
        )


class TestImpliesEndpointsExist:
    """Sanity checks that the new schema columns are usable by other modules."""

    def test_internal_roles_has_implies_and_is_core(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        cols = [
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'internal_roles'"
            ).fetchall()
        ]
        assert "implies" in cols
        assert "is_core" in cols

    def test_user_role_grants_unique_constraint_holds(self, fresh_data_dir):
        """(user_id, internal_role_id) uniqueness — second INSERT raises."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.internal_roles import InternalRolesRepository

        conn = get_system_db()
        user_id = str(uuid.uuid4())
        UserRepository(conn).create(
            id=user_id, email="dup@example.com", name="Dup", role="analyst",
        )
        # create() already inserted a grant for core.analyst — the duplicate
        # explicit insert below must raise.
        analyst = InternalRolesRepository(conn).get_by_key("core.analyst")
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                """INSERT INTO user_role_grants
                   (id, user_id, internal_role_id, granted_by, source)
                   VALUES (?, ?, ?, 'test', 'direct')""",
                [str(uuid.uuid4()), user_id, analyst["id"]],
            )


class TestAPIUsersPostMigration:
    """End-to-end regression: /api/users (and admin endpoints reading users)
    must not 500 on legacy users.role being NULL after v8→v9 migration.

    Original bug: ``UserResponse.role`` is a required ``str`` Pydantic field,
    but the migration NULL-s the legacy column. ``_to_response`` previously
    passed ``u["role"]`` straight through to validation, which raised
    ``string_type`` for every migrated user → ``HTTP 500`` on ``GET /api/users``,
    which made ``/admin/users`` unable to render the list and hid the new
    ``Detail`` link to ``/admin/users/{id}``. The fix routes user dicts
    through ``_hydrate_legacy_role`` before response serialization (and
    before the ``target['role'] == 'admin'`` short-circuit in
    ``update_user`` / ``delete_user`` so last-admin protection still
    triggers on migrated admins)."""

    def _seed_v8_admin(self, db_path):
        """Seed a v8 DB with an admin user; trigger v9 migration; return
        (admin_id, bearer_token) ready for API calls."""
        conn = _v8_state(db_path)
        admin_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, name, role) VALUES (?, ?, ?, ?)",
            [admin_id, "admin@v8", "V8 Admin", "admin"],
        )
        conn.close()
        # Trigger migration via get_system_db; emit token for API access.
        from src.db import get_system_db
        from app.auth.jwt import create_access_token
        get_system_db()  # runs v8→v9 migration
        token = create_access_token(user_id=admin_id, email="admin@v8", role="admin")
        return admin_id, token

    def test_list_users_returns_200_for_v8_migrated_users(
        self, fresh_data_dir, monkeypatch,
    ):
        """``GET /api/users`` must hydrate every user's role from grants
        rather than 500 on the NULL legacy column."""
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        db_path = fresh_data_dir / "state" / "system.duckdb"
        admin_id, token = self._seed_v8_admin(db_path)

        # Confirm the legacy column really is NULL — otherwise this test
        # isn't actually exercising the regression scenario.
        from src.db import get_system_db
        conn = get_system_db()
        legacy = conn.execute(
            "SELECT role FROM users WHERE id = ?", [admin_id]
        ).fetchone()
        assert legacy[0] is None, (
            "v8→v9 migration must NULL legacy users.role for this test "
            "to actually cover the original 500-on-validation regression"
        )

        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        users = resp.json()
        assert len(users) == 1
        assert users[0]["email"] == "admin@v8"
        assert users[0]["role"] == "admin", (
            "_to_response must hydrate role from user_role_grants when the "
            "legacy column is NULL"
        )

    def test_last_admin_protection_still_triggers_on_v8_admin_demote(
        self, fresh_data_dir, monkeypatch,
    ):
        """PATCH that demotes the sole v8-migrated admin must 409. The bug:
        ``target['role'] == 'admin'`` short-circuit in ``update_user`` would
        evaluate False on a NULL legacy role, silently skipping the
        ``count_admins`` guard and letting the operator lock themselves out."""
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        db_path = fresh_data_dir / "state" / "system.duckdb"
        admin_id, token = self._seed_v8_admin(db_path)

        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.patch(
            f"/api/users/{admin_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"role": "viewer"},
        )
        assert resp.status_code == 409, (
            f"expected 409 last-admin protection, got {resp.status_code}: "
            f"{resp.text}"
        )
        assert "admin" in resp.json()["detail"].lower()

    def test_list_users_hydrates_role_for_every_legacy_role(
        self, fresh_data_dir, monkeypatch,
    ):
        """All four legacy roles (viewer/analyst/km_admin/admin) must
        round-trip through the API after v9 migration — proves the
        hydration covers every backfilled grant, not just admin."""
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        admin_id = str(uuid.uuid4())
        # Seed one user per legacy role; admin is the caller.
        conn.execute(
            "INSERT INTO users (id, email, name, role) VALUES (?, ?, ?, ?)",
            [admin_id, "admin@v8", "Admin", "admin"],
        )
        for role_str in ("viewer", "analyst", "km_admin"):
            conn.execute(
                "INSERT INTO users (id, email, name, role) VALUES (?, ?, ?, ?)",
                [str(uuid.uuid4()), f"{role_str}@v8", role_str.title(), role_str],
            )
        conn.close()

        from src.db import get_system_db
        from app.auth.jwt import create_access_token
        get_system_db()  # trigger v8→v9
        token = create_access_token(user_id=admin_id, email="admin@v8", role="admin")

        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        roles = {u["email"]: u["role"] for u in resp.json()}
        assert roles == {
            "admin@v8": "admin",
            "viewer@v8": "viewer",
            "analyst@v8": "analyst",
            "km_admin@v8": "km_admin",
        }, "every legacy role must hydrate back via _to_response"


class TestAuthLoginFlowsPostMigration:
    """Devin review #73 (round 3): every auth login flow must hydrate
    ``user["role"]`` from ``user_role_grants`` before passing it to
    ``create_access_token`` / Pydantic response models / login cookies.

    The most severe failure mode was ``POST /auth/token``:
    ``TokenResponse.role`` is required ``str``, but post-v9 the legacy
    column is NULL — so any v8-migrated user logging in via password got
    HTTP 500 (``ValidationError: string_type``). The Google/email/web
    cookie flows didn't crash but wrote ``role: null`` into the issued
    JWT, which downstream ``_hydrate_legacy_role`` in
    ``get_current_user`` would correct on every request — but the token
    itself stayed semantically wrong. Fix: hydrate inline in each login
    flow before reading ``user["role"]``."""

    def _seed_v8_user_with_password(
        self, db_path, email: str, role: str, password: str = "TestPass1!",
    ) -> str:
        """Seed a v8 DB with a password-set user, run v9 migration,
        return the user id."""
        from argon2 import PasswordHasher
        conn = _v8_state(db_path)
        user_id = str(uuid.uuid4())
        # v8 schema in _v8_state doesn't include password_hash — patch the
        # column on so we can store a bcrypt/argon2 hash for the login.
        conn.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR")
        conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            [user_id, email, email.split("@")[0], role, PasswordHasher().hash(password)],
        )
        conn.close()
        # Trigger v8→v9 migration.
        from src.db import get_system_db
        get_system_db()
        return user_id

    def test_post_auth_token_returns_200_for_v8_migrated_admin(
        self, fresh_data_dir, monkeypatch,
    ):
        """``POST /auth/token`` with valid email + password for a
        v8-migrated admin must return 200 with the hydrated role string,
        NOT crash on ``TokenResponse.role: str`` validating against
        None."""
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        db_path = fresh_data_dir / "state" / "system.duckdb"
        password = "TestPass1!"
        self._seed_v8_user_with_password(db_path, "admin@v8", "admin", password)

        # Confirm the legacy column really is NULL post-migration —
        # otherwise this test isn't covering the regression scenario.
        from src.db import get_system_db
        conn = get_system_db()
        legacy = conn.execute(
            "SELECT role FROM users WHERE email = ?", ["admin@v8"]
        ).fetchone()[0]
        assert legacy is None, (
            "v9 migration must NULL legacy users.role for this test to "
            "actually exercise the Devin review #73 round-3 regression"
        )

        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post(
            "/auth/token",
            json={"email": "admin@v8", "password": password},
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text} — "
            "TokenResponse.role: str must receive the hydrated value, "
            "not None from the NULL legacy column"
        )
        body = resp.json()
        assert body["role"] == "admin", (
            "TokenResponse.role must hydrate to the actual core.* grant "
            f"for a v8-admin user, got {body['role']!r}"
        )
        assert body["access_token"], "non-empty JWT issued on success"

    def test_post_auth_token_returns_correct_role_for_each_legacy_value(
        self, fresh_data_dir, monkeypatch,
    ):
        """All four legacy roles (viewer/analyst/km_admin/admin) must
        round-trip through ``POST /auth/token`` with their hydrated
        string value — not None, not the wrong level."""
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        db_path = fresh_data_dir / "state" / "system.duckdb"
        password = "TestPass1!"

        # Seed all four levels in one v8 DB.
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        conn = _v8_state(db_path)
        conn.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR")
        for role_str in ("viewer", "analyst", "km_admin", "admin"):
            conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    str(uuid.uuid4()),
                    f"{role_str}@v8",
                    role_str.title(),
                    role_str,
                    ph.hash(password),
                ],
            )
        conn.close()
        from src.db import get_system_db
        get_system_db()

        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        observed: dict[str, str] = {}
        for role_str in ("viewer", "analyst", "km_admin", "admin"):
            resp = client.post(
                "/auth/token",
                json={"email": f"{role_str}@v8", "password": password},
            )
            assert resp.status_code == 200, (
                f"login for {role_str} crashed: {resp.status_code} {resp.text}"
            )
            observed[f"{role_str}@v8"] = resp.json()["role"]

        assert observed == {
            "viewer@v8": "viewer",
            "analyst@v8": "analyst",
            "km_admin@v8": "km_admin",
            "admin@v8": "admin",
        }


class TestSeedCoreRolesSafetyNet:
    """The unconditional _seed_core_roles call at the tail of _ensure_schema
    is the on-every-connect safety net the function's docstring promises.

    Pin the contract so a future refactor that moves the call back inside the
    migration guard fails loudly: a deleted/modified core.* row must be
    restored on the next process start, without bumping the schema version
    or running migrations."""

    def test_deleted_core_role_is_reseeded_on_next_ensure_schema(
        self, fresh_data_dir,
    ):
        """An accidental DELETE on internal_roles WHERE key='core.admin'
        should be self-healing: the next _ensure_schema call restores the row
        even though schema_version is already at SCHEMA_VERSION."""
        from src.db import get_system_db, _ensure_schema, SCHEMA_VERSION, get_schema_version

        conn = get_system_db()
        assert get_schema_version(conn) == SCHEMA_VERSION

        # Sanity: row is there after fresh install.
        before = conn.execute(
            "SELECT key FROM internal_roles WHERE key = 'core.admin'"
        ).fetchone()
        assert before is not None

        # Simulate accidental deletion.
        conn.execute("DELETE FROM internal_roles WHERE key = 'core.admin'")
        gone = conn.execute(
            "SELECT key FROM internal_roles WHERE key = 'core.admin'"
        ).fetchone()
        assert gone is None

        # Second _ensure_schema call (what happens on next process start)
        # — migration guard skips because version is already current, but the
        # tail seed call must still run and restore the row.
        _ensure_schema(conn)

        restored = conn.execute(
            "SELECT key, is_core, owner_module FROM internal_roles "
            "WHERE key = 'core.admin'"
        ).fetchone()
        assert restored is not None, (
            "core.admin must be re-seeded by the tail _seed_core_roles call"
        )
        assert restored[0] == "core.admin"
        assert restored[1] is True
        assert restored[2] == "core"

    def test_mutated_core_role_display_name_is_resynced(
        self, fresh_data_dir,
    ):
        """If an operator hand-edits a core.* row's display_name, the next
        startup must rewrite it from the in-code _CORE_ROLES_SEED — that's
        how doc tweaks ship without manual SQL."""
        from src.db import get_system_db, _ensure_schema

        conn = get_system_db()
        # Stomp the display_name to something wrong.
        conn.execute(
            "UPDATE internal_roles SET display_name = 'WRONG' "
            "WHERE key = 'core.admin'"
        )
        wrong = conn.execute(
            "SELECT display_name FROM internal_roles WHERE key = 'core.admin'"
        ).fetchone()[0]
        assert wrong == "WRONG"

        _ensure_schema(conn)

        restored = conn.execute(
            "SELECT display_name FROM internal_roles WHERE key = 'core.admin'"
        ).fetchone()[0]
        assert restored == "Administrator", (
            "tail _seed_core_roles must rewrite display_name from in-code seed"
        )

    def test_seed_runs_on_already_v9_db_without_bumping_version(
        self, fresh_data_dir,
    ):
        """Pin the no-op behavior: on a current-version DB, _ensure_schema
        runs the tail seed but does not re-apply migrations or change the
        version row's applied_at timestamp."""
        from src.db import get_system_db, _ensure_schema, SCHEMA_VERSION

        conn = get_system_db()
        version_before = conn.execute(
            "SELECT version, applied_at FROM schema_version"
        ).fetchone()
        assert version_before[0] == SCHEMA_VERSION

        _ensure_schema(conn)

        version_after = conn.execute(
            "SELECT version, applied_at FROM schema_version"
        ).fetchone()
        # applied_at must NOT change — the migration guard short-circuits
        # before the UPDATE schema_version statement when current ==
        # SCHEMA_VERSION.
        assert version_after == version_before, (
            "schema_version row must not be touched on a current-version DB"
        )
