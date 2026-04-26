"""Tests for the internal-role registry, sync, resolver, and require dependency.

Schema v8 adds ``internal_roles`` and ``group_mappings``; the resolver in
``app.auth.role_resolver`` is the integration point between Cloud Identity
groups (external) and Agnes-defined capabilities (internal). End-to-end
exercise rides on LOCAL_DEV_MODE + LOCAL_DEV_GROUPS so we don't need to
mock Google OAuth.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clear_role_registry():
    """Module-level _REGISTRY persists across tests in the same process —
    flush before AND after each test so registrations from one test don't
    leak into the next, regardless of which fixture ran first."""
    from app.auth.role_resolver import _clear_registry_for_tests
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


class TestRegisterInternalRole:
    def test_register_and_list(self):
        from app.auth.role_resolver import (
            register_internal_role, list_registered_roles,
        )
        register_internal_role(
            "context_admin",
            display_name="Context Admin",
            description="Manages the context engineering module.",
            owner_module="context_engineering",
        )
        register_internal_role("agent_operator", display_name="Agent Operator")
        keys = [s.key for s in list_registered_roles()]
        assert keys == ["agent_operator", "context_admin"]  # sorted

    def test_register_same_key_same_fields_is_idempotent(self):
        """Re-importing a module shouldn't blow up — same key + same fields no-ops."""
        from app.auth.role_resolver import (
            register_internal_role, list_registered_roles,
        )
        register_internal_role("x", display_name="X")
        register_internal_role("x", display_name="X")
        assert len(list_registered_roles()) == 1

    def test_register_same_key_different_fields_raises(self):
        """Two modules picking the same key would silently overwrite each
        other's metadata — refuse and force one of them to rename."""
        from app.auth.role_resolver import register_internal_role
        register_internal_role("x", display_name="X")
        with pytest.raises(ValueError, match="already registered"):
            register_internal_role("x", display_name="Different")

    @pytest.mark.parametrize("bad_key", [
        "Context_Admin",   # uppercase
        "1context",        # leading digit
        "context-admin",   # hyphen
        "",                # empty
        "context admin",   # space
        "x" * 65,          # too long
    ])
    def test_register_rejects_invalid_keys(self, bad_key):
        from app.auth.role_resolver import register_internal_role
        with pytest.raises(ValueError, match="Invalid internal role key"):
            register_internal_role(bad_key, display_name="X")


class TestSyncRegisteredRolesToDb:
    def test_inserts_new_roles(self, db_conn):
        from app.auth.role_resolver import (
            register_internal_role, sync_registered_roles_to_db,
        )
        from src.repositories.internal_roles import InternalRolesRepository
        register_internal_role("ctx_admin", display_name="Context Admin")
        sync_registered_roles_to_db(db_conn)
        row = InternalRolesRepository(db_conn).get_by_key("ctx_admin")
        assert row is not None
        assert row["display_name"] == "Context Admin"

    def test_sync_is_idempotent(self, db_conn):
        from app.auth.role_resolver import (
            register_internal_role, sync_registered_roles_to_db,
        )
        register_internal_role("ctx_admin", display_name="Context Admin")
        sync_registered_roles_to_db(db_conn)
        sync_registered_roles_to_db(db_conn)  # second call must not duplicate
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM internal_roles WHERE key = 'ctx_admin'"
        ).fetchone()
        assert rows[0] == 1

    def test_sync_updates_drifted_metadata(self, db_conn):
        """Display name change in code should propagate to DB on next startup."""
        from app.auth.role_resolver import (
            register_internal_role, sync_registered_roles_to_db,
            _clear_registry_for_tests,
        )
        from src.repositories.internal_roles import InternalRolesRepository
        register_internal_role("ctx_admin", display_name="Old Name")
        sync_registered_roles_to_db(db_conn)
        # Simulate a code update: clear the registry and re-register with new name.
        _clear_registry_for_tests()
        register_internal_role("ctx_admin", display_name="New Name")
        sync_registered_roles_to_db(db_conn)
        row = InternalRolesRepository(db_conn).get_by_key("ctx_admin")
        assert row["display_name"] == "New Name"

    def test_sync_does_not_delete_unregistered_roles(self, db_conn):
        """A role disappearing from code (module unloaded) keeps its DB row +
        mappings until an admin explicitly removes it."""
        from app.auth.role_resolver import (
            register_internal_role, sync_registered_roles_to_db,
            _clear_registry_for_tests,
        )
        from src.repositories.internal_roles import InternalRolesRepository
        register_internal_role("legacy_role", display_name="Legacy")
        sync_registered_roles_to_db(db_conn)
        _clear_registry_for_tests()  # module no longer registers this role
        sync_registered_roles_to_db(db_conn)
        row = InternalRolesRepository(db_conn).get_by_key("legacy_role")
        assert row is not None  # still there


class TestResolveInternalRoles:
    def test_returns_empty_when_no_external_groups(self, db_conn):
        from app.auth.role_resolver import resolve_internal_roles
        assert resolve_internal_roles([], db_conn) == []

    def test_returns_empty_when_no_mappings(self, db_conn):
        from app.auth.role_resolver import resolve_internal_roles
        groups = [{"id": "engineers@x.com", "name": "Engineers"}]
        assert resolve_internal_roles(groups, db_conn) == []

    def test_resolves_single_mapping(self, db_conn):
        from app.auth.role_resolver import resolve_internal_roles
        from src.repositories.internal_roles import InternalRolesRepository
        from src.repositories.group_mappings import GroupMappingsRepository
        roles = InternalRolesRepository(db_conn)
        mappings = GroupMappingsRepository(db_conn)
        role_id = str(uuid.uuid4())
        roles.create(id=role_id, key="ctx_admin", display_name="Context Admin")
        mappings.create(
            id=str(uuid.uuid4()),
            external_group_id="engineers@x.com",
            internal_role_id=role_id,
            assigned_by="admin@x.com",
        )
        result = resolve_internal_roles(
            [{"id": "engineers@x.com", "name": "Engineers"}], db_conn,
        )
        assert result == ["ctx_admin"]

    def test_resolves_many_to_many(self, db_conn):
        """Multiple external groups, multiple roles, with overlap — output
        must be sorted + deduplicated."""
        from app.auth.role_resolver import resolve_internal_roles
        from src.repositories.internal_roles import InternalRolesRepository
        from src.repositories.group_mappings import GroupMappingsRepository
        roles = InternalRolesRepository(db_conn)
        mappings = GroupMappingsRepository(db_conn)
        ctx_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        roles.create(id=ctx_id, key="ctx_admin", display_name="C")
        roles.create(id=agent_id, key="agent_operator", display_name="A")
        # engineers → ctx_admin AND agent_operator
        mappings.create(
            id=str(uuid.uuid4()), external_group_id="eng@x", internal_role_id=ctx_id,
        )
        mappings.create(
            id=str(uuid.uuid4()), external_group_id="eng@x", internal_role_id=agent_id,
        )
        # admins → ctx_admin (overlap with engineers)
        mappings.create(
            id=str(uuid.uuid4()), external_group_id="admins@x", internal_role_id=ctx_id,
        )
        result = resolve_internal_roles(
            [{"id": "eng@x", "name": "E"}, {"id": "admins@x", "name": "A"}],
            db_conn,
        )
        assert result == ["agent_operator", "ctx_admin"]  # sorted, deduped

    def test_ignores_malformed_external_group_entries(self, db_conn):
        """Defensive: a stray non-dict or missing-id entry shouldn't crash
        the resolver — those just get skipped."""
        from app.auth.role_resolver import resolve_internal_roles
        result = resolve_internal_roles(
            ["not-a-dict", {"name": "no-id"}, {"id": ""}],  # type: ignore[list-item]
            db_conn,
        )
        assert result == []


class TestRequireInternalRole:
    """End-to-end via LOCAL_DEV_MODE + LOCAL_DEV_GROUPS: dev user with a
    mapped external group passes the gate; without the mapping, 403."""

    @pytest.fixture
    def dev_app_with_mapping(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("SESSION_SECRET", "test-session-secret-32chars-minimum!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.setenv("LOCAL_DEV_USER_EMAIL", "dev@localhost")
        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"id":"engineers@example.com","name":"Engineers"}]',
        )
        # Register a role + map external group → role BEFORE create_app() so
        # the startup sync picks it up and the resolver finds the mapping on
        # the first request.
        from app.auth.role_resolver import register_internal_role
        register_internal_role("ctx_admin", display_name="Context Admin")

        from src.db import get_system_db
        conn = get_system_db()
        try:
            from app.auth.role_resolver import sync_registered_roles_to_db
            sync_registered_roles_to_db(conn)
            from src.repositories.internal_roles import InternalRolesRepository
            from src.repositories.group_mappings import GroupMappingsRepository
            role = InternalRolesRepository(conn).get_by_key("ctx_admin")
            GroupMappingsRepository(conn).create(
                id=str(uuid.uuid4()),
                external_group_id="engineers@example.com",
                internal_role_id=role["id"],
                assigned_by="setup",
            )
        finally:
            conn.close()

        from app.main import create_app
        from fastapi import Depends, FastAPI
        from app.auth.role_resolver import require_internal_role

        app = create_app()
        # Attach two probe endpoints — one gated by ctx_admin, one by a role
        # the dev user does NOT hold.
        @app.get("/_test/needs-ctx")
        async def needs_ctx(user: dict = Depends(require_internal_role("ctx_admin"))):
            return {"ok": True, "email": user["email"]}

        @app.get("/_test/needs-other")
        async def needs_other(user: dict = Depends(require_internal_role("never_granted"))):
            return {"ok": True}

        return TestClient(app)

    def test_grants_access_when_mapped_role_present(self, dev_app_with_mapping):
        resp = dev_app_with_mapping.get("/_test/needs-ctx")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "email": "dev@localhost"}

    def test_denies_access_when_role_missing(self, dev_app_with_mapping):
        resp = dev_app_with_mapping.get("/_test/needs-other")
        assert resp.status_code == 403
        assert "never_granted" in resp.json()["detail"]

    def test_session_internal_roles_populated(self, dev_app_with_mapping):
        """Direct session inspection — the resolver wrote the resolved role
        keys into session.internal_roles, decoupled from any HTML template."""
        # Hit any auth-required endpoint to trigger the resolver.
        dev_app_with_mapping.get("/_test/needs-ctx")
        from itsdangerous import TimestampSigner
        import base64, json as _json
        cookie = dev_app_with_mapping.cookies.get("session")
        assert cookie, "session cookie missing"
        signer = TimestampSigner(os.environ["SESSION_SECRET"])
        unsigned = signer.unsign(cookie, max_age=14 * 24 * 3600)
        payload = _json.loads(base64.b64decode(unsigned))
        assert payload.get("internal_roles") == ["ctx_admin"]

    def test_stale_session_keeps_old_roles_after_mapping_change(self, dev_app_with_mapping):
        """KNOWN LIMITATION (documented in docs/internal-roles.md → Resolution
        timing): roles are resolved at sign-in only. If an admin revokes a
        mapping mid-session, the user keeps the cached role keys until they
        log out + back in. This test pins that behavior so any future cache
        invalidation pathway (admin UI broadcast, deactivate-then-reactivate
        side-effect) is a deliberate change, not an accident."""
        # First request — dev-bypass populates session.internal_roles=["ctx_admin"].
        resp1 = dev_app_with_mapping.get("/_test/needs-ctx")
        assert resp1.status_code == 200

        # Admin revokes the mapping out-of-band.
        from src.db import get_system_db
        from src.repositories.group_mappings import GroupMappingsRepository
        from src.repositories.internal_roles import InternalRolesRepository
        conn = get_system_db()
        try:
            role = InternalRolesRepository(conn).get_by_key("ctx_admin")
            existing = GroupMappingsRepository(conn).list_by_role(role["id"])
            for m in existing:
                GroupMappingsRepository(conn).delete(m["id"])
        finally:
            conn.close()

        # Second request — session still holds the cached role; gate still passes.
        # The dev-bypass write-skip path (groups_changed=False AND
        # internal_roles already in session) keeps the session value intact,
        # mirroring the OAuth flow where session lives until logout.
        resp2 = dev_app_with_mapping.get("/_test/needs-ctx")
        assert resp2.status_code == 200, (
            "Stale-session contract broken: revoking a mapping must NOT "
            "drop access mid-session today. If this assertion starts "
            "failing, decide deliberately whether you've added "
            "invalidation (good — update the doc) or introduced a "
            "regression that double-resolves on every request (bad)."
        )

    def test_pat_caller_with_direct_grant_passes(self, db_conn, monkeypatch):
        """v9 PAT-aware path: a user with a direct user_role_grants row
        passes require_internal_role even without session.internal_roles.
        This is the new admin-CLI-via-PAT contract — without it, all admin
        endpoints would 403 to PAT clients after the require_admin
        wrappers route through require_internal_role('core.admin')."""
        from unittest.mock import MagicMock
        import asyncio
        from app.auth.role_resolver import require_internal_role
        from src.repositories.users import UserRepository
        from src.repositories.internal_roles import InternalRolesRepository

        # UserRepository.create(role="admin") auto-grants core.admin in v9
        # — the explicit grant insert below would violate the UNIQUE
        # (user_id, internal_role_id) constraint. Just create + verify.
        user_id = str(uuid.uuid4())
        UserRepository(db_conn).create(
            id=user_id, email="admin-pat@example.com", name="Admin PAT",
            role="admin",
        )

        # PAT-shape request: session middleware ran (attribute exists) but
        # no internal_roles key. Gate must consult DB and grant access.
        request = MagicMock()
        request.session = {}

        check = require_internal_role("core.admin")
        result = asyncio.run(check(
            request=request, user={"id": user_id, "email": "admin-pat@example.com"},
        ))
        assert result["id"] == user_id

    def test_pat_caller_without_grant_gets_403(self):
        """PAT/headless callers carry no session.internal_roles, but v9
        require_internal_role falls back to user_role_grants in DB. A PAT
        client whose user has no matching grant must still hit 403, not
        slip through. Pins the closed-by-default behavior of the new
        two-path resolver."""
        from unittest.mock import MagicMock
        import asyncio
        from fastapi import HTTPException
        from app.auth.role_resolver import require_internal_role

        # PAT request shape: session middleware ran (session attribute exists)
        # but OAuth callback never fired, so internal_roles is absent.
        request = MagicMock()
        request.session = {}

        check = require_internal_role("ctx_admin")
        with pytest.raises(HTTPException) as exc_info:
            # No matching grant in DB either — empty user dict means user_id
            # is None and the DB lookup returns []. Gate must close.
            asyncio.run(check(request=request, user={"email": "pat@example.com"}))

        assert exc_info.value.status_code == 403
        assert "ctx_admin" in exc_info.value.detail

    def test_oauth_pipeline_groups_to_internal_roles(self, db_conn):
        """End-to-end data flow: fake _fetch_google_groups output (the
        only Cloud Identity touchpoint) → join against group_mappings →
        internal_roles list. The OAuth handshake itself isn't exercised
        here — its failure modes live in _fetch_google_groups, which
        has its own coverage. This test pins the resolver as the
        contract between 'whatever Google returned' and
        'session.internal_roles'."""
        from app.auth.role_resolver import (
            register_internal_role,
            sync_registered_roles_to_db,
            resolve_internal_roles,
        )
        from src.repositories.internal_roles import InternalRolesRepository
        from src.repositories.group_mappings import GroupMappingsRepository

        register_internal_role("ctx_admin", display_name="Context Admin")
        register_internal_role("agent_op", display_name="Agent Operator")
        sync_registered_roles_to_db(db_conn)

        ctx = InternalRolesRepository(db_conn).get_by_key("ctx_admin")
        agent = InternalRolesRepository(db_conn).get_by_key("agent_op")
        gm = GroupMappingsRepository(db_conn)
        gm.create(
            id=str(uuid.uuid4()),
            external_group_id="engineers@example.com",
            internal_role_id=ctx["id"],
        )
        gm.create(
            id=str(uuid.uuid4()),
            external_group_id="ops@example.com",
            internal_role_id=agent["id"],
        )

        # Simulate Google's response: two mapped groups + one unrelated.
        google_groups = [
            {"id": "engineers@example.com", "name": "Engineering"},
            {"id": "ops@example.com", "name": "Operations"},
            {"id": "marketing@example.com", "name": "Marketing"},  # unmapped
        ]
        result = resolve_internal_roles(google_groups, db_conn)
        assert result == ["agent_op", "ctx_admin"]  # sorted, deduped

    def test_dev_bypass_falls_back_to_empty_on_resolver_error(
        self, tmp_path, monkeypatch
    ):
        """If resolve_internal_roles raises mid-request (corrupted DB,
        schema mid-migration, transient lock), the dev-bypass path
        catches and writes []. Auth must never break on resolver
        infrastructure failures — same defensive contract as the OAuth
        callback's try/except wrapper."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("SESSION_SECRET", "test-session-secret-32chars-minimum!!")
        monkeypatch.setenv("LOCAL_DEV_MODE", "1")
        monkeypatch.setenv("LOCAL_DEV_USER_EMAIL", "dev@localhost")
        monkeypatch.setenv(
            "LOCAL_DEV_GROUPS",
            '[{"id":"engineers@example.com","name":"Engineers"}]',
        )
        # Patch the symbol on the module so the lazy import inside the
        # dev-bypass branch picks up the broken stub on call.
        import app.auth.role_resolver as rr

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated resolver failure")

        monkeypatch.setattr(rr, "resolve_internal_roles", boom)

        from app.main import create_app
        from fastapi import Depends, FastAPI
        from app.auth.dependencies import get_current_user

        app = create_app()

        @app.get("/_test/probe")
        async def probe(user: dict = Depends(get_current_user)):
            return {"email": user["email"]}

        client = TestClient(app)
        # Auth still succeeds — resolver failure must not 500/401 the request.
        resp = client.get("/_test/probe")
        assert resp.status_code == 200
        assert resp.json()["email"] == "dev@localhost"
