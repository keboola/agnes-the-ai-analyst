"""Tests for the v15 external_id mapping + Google group prefix filter.

Covers:

- ``UserGroupsRepository.resolve_or_create_for_external`` —
  prefix-stripping + capitalize derivation, lookup-or-create flow,
  promote-on-derived-name, conflict handling, ``ExternalIdImmutable``.
- ``UserGroupMembersRepository`` admin guard — refuses ``source='admin'``
  on bound groups; allows ``google_sync`` and ``system_seed``.
- Schema v15 — ``external_id`` column exists, system Admin / Everyone
  rows survive the migration.

These are unit-level — the OAuth-callback integration (gate redirects,
soft-fail pass-through) is exercised via end-to-end tests in
test_auth_providers.py to avoid duplicating Authlib mocking here.
"""

from __future__ import annotations

import pytest

import src.db as db
from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP, get_system_db
from src.repositories.user_groups import (
    ExternalIdConflict,
    ExternalIdImmutable,
    UserGroupsRepository,
)
from src.repositories.user_group_members import (
    ExternalGroupReadOnly,
    UserGroupMembersRepository,
)
from src.repositories.users import UserRepository


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test DATA_DIR and clean system DB connection."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None

    conn = get_system_db()
    yield conn

    try:
        conn.close()
    except Exception:
        pass
    db._system_db_conn = None
    db._system_db_path = None


# ---------------------------------------------------------------------------
# Schema v15
# ---------------------------------------------------------------------------


class TestSchemaV15:
    def test_external_id_column_exists(self, fresh_db):
        row = fresh_db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'user_groups' AND column_name = 'external_id'"
        ).fetchone()
        assert row is not None

    def test_system_groups_present(self, fresh_db):
        for name in (SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP):
            row = fresh_db.execute(
                "SELECT id, is_system FROM user_groups WHERE name = ?", [name]
            ).fetchone()
            assert row is not None, f"system group {name!r} missing"
            assert row[1] is True

    def test_schema_version_is_15(self, fresh_db):
        row = fresh_db.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        assert row[0] >= 15


# ---------------------------------------------------------------------------
# resolve_or_create_for_external — derivation + lookup flow
# ---------------------------------------------------------------------------


class TestResolveOrCreateForExternal:
    def test_creates_new_with_derived_name(self, fresh_db):
        repo = UserGroupsRepository(fresh_db)
        g = repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        assert g["name"] == "Finance"
        assert g["external_id"] == "grp_foundryai_finance@groupon.com"
        assert g["is_system"] is False

    def test_derivation_lowercases_email(self, fresh_db):
        repo = UserGroupsRepository(fresh_db)
        g = repo.resolve_or_create_for_external(
            "GRP_FOUNDRYAI_Sales@GROUPON.COM", prefix="grp_foundryai_"
        )
        assert g["external_id"] == "grp_foundryai_sales@groupon.com"
        assert g["name"] == "Sales"

    def test_promotes_existing_admin_group(self, fresh_db):
        """An Agnes group named `Admin` already exists from the seed; sync of
        `grp_foundryai_admin@…` should attach the external_id, not duplicate."""
        repo = UserGroupsRepository(fresh_db)
        before = repo.get_by_name(SYSTEM_ADMIN_GROUP)
        assert before is not None
        assert before["external_id"] is None

        g = repo.resolve_or_create_for_external(
            "grp_foundryai_admin@groupon.com", prefix="grp_foundryai_"
        )
        assert g["id"] == before["id"]
        assert g["external_id"] == "grp_foundryai_admin@groupon.com"
        assert g["is_system"] is True  # promote preserved is_system

        # No duplicate row.
        rows = fresh_db.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchall()
        assert len(rows) == 1

    def test_idempotent_on_repeat(self, fresh_db):
        repo = UserGroupsRepository(fresh_db)
        first = repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        second = repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        assert first["id"] == second["id"]
        rows = fresh_db.execute(
            "SELECT id FROM user_groups WHERE name = 'Finance'"
        ).fetchall()
        assert len(rows) == 1

    def test_promotes_admin_created_group_with_null_external_id(self, fresh_db):
        """Admin manually created `Marketing` before any Google sync; the
        next sync of `grp_foundryai_marketing@` attaches external_id."""
        repo = UserGroupsRepository(fresh_db)
        manual = repo.create(name="Marketing", description="manual")
        assert manual["external_id"] is None

        g = repo.resolve_or_create_for_external(
            "grp_foundryai_marketing@groupon.com", prefix="grp_foundryai_"
        )
        assert g["id"] == manual["id"]
        assert g["external_id"] == "grp_foundryai_marketing@groupon.com"

    def test_conflict_when_name_already_bound_to_other_email(self, fresh_db):
        """Pre-existing `Finance` is bound to a different email — the second
        sync raises ExternalIdConflict instead of silently re-routing."""
        repo = UserGroupsRepository(fresh_db)
        repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        # Same derived name "Finance" but different external_id should conflict.
        # Force the case by manually inserting a second prefixed email that
        # also strips to "Finance" — contrived, but exercises the guard path.
        with pytest.raises(ExternalIdConflict):
            repo.resolve_or_create_for_external(
                "grp_foundryai_finance@otherdomain.com", prefix="grp_foundryai_"
            )


# ---------------------------------------------------------------------------
# update() — external_id is immutable
# ---------------------------------------------------------------------------


class TestUpdateExternalIdImmutable:
    def test_update_with_external_id_kwarg_raises(self, fresh_db):
        repo = UserGroupsRepository(fresh_db)
        g = repo.create(name="Manual", description="x")
        with pytest.raises(ExternalIdImmutable):
            repo.update(g["id"], external_id="grp_foundryai_manual@groupon.com")

    def test_update_with_external_id_none_also_raises(self, fresh_db):
        """Passing None still triggers the guard — caller meant to clear."""
        repo = UserGroupsRepository(fresh_db)
        g = repo.create(name="Manual2", description="x")
        with pytest.raises(ExternalIdImmutable):
            repo.update(g["id"], external_id=None)

    def test_update_name_on_bound_group_succeeds(self, fresh_db):
        """Display name of a non-system bound group is editable; only the
        external_id link is locked."""
        repo = UserGroupsRepository(fresh_db)
        g = repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        repo.update(g["id"], name="Finance team")
        after = repo.get(g["id"])
        assert after["name"] == "Finance team"
        assert after["external_id"] == "grp_foundryai_finance@groupon.com"


# ---------------------------------------------------------------------------
# Members guard — admin source blocked on external_id-bound groups
# ---------------------------------------------------------------------------


class TestMembersGuard:
    def _seed_user(self, conn, email="u@example.com"):
        repo = UserRepository(conn)
        import uuid
        uid = str(uuid.uuid4())
        repo.create(id=uid, email=email, name="Test")
        return uid

    def test_admin_source_blocked_on_bound_group(self, fresh_db):
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        bound = ug_repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        uid = self._seed_user(fresh_db)
        with pytest.raises(ExternalGroupReadOnly):
            members.add_member(uid, bound["id"], source="admin", added_by="admin@x")

    def test_admin_source_allowed_on_unbound_group(self, fresh_db):
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        manual = ug_repo.create(name="Vendors", description="external partners")
        uid = self._seed_user(fresh_db)
        members.add_member(uid, manual["id"], source="admin", added_by="admin@x")
        assert members.has_membership(uid, manual["id"])

    def test_google_sync_source_allowed_on_bound_group(self, fresh_db):
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        bound = ug_repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        uid = self._seed_user(fresh_db)
        # Direct add_member with google_sync source bypasses the guard.
        members.add_member(uid, bound["id"], source="google_sync", added_by="sys")
        assert members.has_membership(uid, bound["id"])

    def test_system_seed_source_allowed_on_bound_group(self, fresh_db):
        """system_seed bootstrap (SEED_ADMIN_EMAIL) must work even after
        Admin gets bound to grp_foundryai_admin@."""
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        admin = ug_repo.resolve_or_create_for_external(
            "grp_foundryai_admin@groupon.com", prefix="grp_foundryai_"
        )
        assert admin["external_id"] is not None
        uid = self._seed_user(fresh_db)
        members.add_member(uid, admin["id"], source="system_seed", added_by="seed")
        assert members.has_membership(uid, admin["id"])

    def test_remove_admin_source_blocked_on_bound_group(self, fresh_db):
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        bound = ug_repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        uid = self._seed_user(fresh_db)
        # Pre-seed via google_sync so there's a row to attempt to remove.
        members.add_member(uid, bound["id"], source="google_sync", added_by="sys")
        with pytest.raises(ExternalGroupReadOnly):
            members.remove_member(uid, bound["id"], require_source="admin")


# ---------------------------------------------------------------------------
# has_any_google_sync_membership — used by the OAuth soft-fail gate
# ---------------------------------------------------------------------------


class TestHasAnyGoogleSyncMembership:
    def test_returns_false_on_fresh_user(self, fresh_db):
        members = UserGroupMembersRepository(fresh_db)
        repo = UserRepository(fresh_db)
        import uuid
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="x@y.com", name="X")
        assert members.has_any_google_sync_membership(uid) is False

    def test_true_after_replace_google_sync_groups(self, fresh_db):
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        repo = UserRepository(fresh_db)
        import uuid
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="x@y.com", name="X")

        bound = ug_repo.resolve_or_create_for_external(
            "grp_foundryai_finance@groupon.com", prefix="grp_foundryai_"
        )
        members.replace_google_sync_groups(uid, [bound["id"]])
        assert members.has_any_google_sync_membership(uid) is True

    def test_false_when_only_admin_source_rows_exist(self, fresh_db):
        """Admin-added membership doesn't count as cached google_sync."""
        ug_repo = UserGroupsRepository(fresh_db)
        members = UserGroupMembersRepository(fresh_db)
        repo = UserRepository(fresh_db)
        import uuid
        uid = str(uuid.uuid4())
        repo.create(id=uid, email="x@y.com", name="X")

        manual = ug_repo.create(name="Custom")
        members.add_member(uid, manual["id"], source="admin", added_by="a")
        assert members.has_any_google_sync_membership(uid) is False
