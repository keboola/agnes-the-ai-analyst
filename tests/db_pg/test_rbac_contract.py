"""Cross-engine contract tests for the RBAC quartet.

Targets: user_groups_repo, user_group_members_repo, resource_grants_repo.
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the pattern established in test_audit_contract.py.
"""
from __future__ import annotations

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repos(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "groups": UserGroupsRepository(conn),
        "members": UserGroupMembersRepository(conn),
        "grants": ResourceGrantsRepository(conn),
        "users": UserRepository(conn),
    }, conn


def _make_pg_repos(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    import sqlalchemy as sa

    # Seed system groups — PG doesn't have DuckDB's _seed_system_groups
    # auto-run; the contract tests need Admin + Everyone to exist so that
    # the DuckDB side (which does auto-seed) and PG side start with the
    # same baseline.
    import uuid as _uuid
    with pg_engine.begin() as conn_:
        for name, description in (
            ("Admin", "System: full access"),
            ("Everyone", "System: default group"),
        ):
            conn_.execute(
                sa.text(
                    "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                    "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                    "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                ),
                {"id": _uuid.uuid4().hex, "name": name, "desc": description},
            )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    return {
        "groups": UserGroupsPgRepository(engine),
        "members": UserGroupMembersPgRepository(engine),
        "grants": ResourceGrantsPgRepository(engine),
        "users": UsersPgRepository(engine),
    }, None


@pytest.fixture(params=["duckdb", "pg"])
def rbac_repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repos_dict, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repos, conn = _make_duckdb_repos(tmp_path)
        yield repos, conn, backend
        if conn is not None:
            conn.close()
    else:
        repos, _ = _make_pg_repos(pg_engine, monkeypatch)
        yield repos, None, backend


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------

def test_create_group_then_get_returns_it(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grp = groups.create(name="data-team", description="Data Team", created_by="admin@x.com")
    assert grp is not None
    assert grp["name"] == "data-team"
    fetched = groups.get(grp["id"])
    assert fetched is not None
    assert fetched["name"] == "data-team"
    assert fetched["description"] == "Data Team"


def test_add_member_then_list_members_returns_user(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u1", email="alice@example.com", name="Alice")
    grp = groups.create(name="analytics", created_by="admin@x.com")
    members.add_member("u1", grp["id"], source="admin", added_by="admin@x.com")

    listed = members.list_members_for_group(grp["id"])
    assert len(listed) == 1
    assert listed[0]["id"] == "u1"
    assert listed[0]["email"] == "alice@example.com"


def test_has_membership_true_after_add(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u2", email="bob@example.com", name="Bob")
    grp = groups.create(name="eng", created_by="admin@x.com")
    assert not members.has_membership("u2", grp["id"])
    members.add_member("u2", grp["id"], source="admin")
    assert members.has_membership("u2", grp["id"])


def test_remove_member_then_has_membership_false(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u3", email="carol@example.com", name="Carol")
    grp = groups.create(name="ops", created_by="admin@x.com")
    members.add_member("u3", grp["id"], source="admin")
    assert members.has_membership("u3", grp["id"])
    members.remove_member("u3", grp["id"])
    assert not members.has_membership("u3", grp["id"])


def test_resource_grant_create_then_has_grant(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="readers", created_by="admin@x.com")
    # Use 'marketplace_plugin' so no table_registry row is needed — the
    # per-type FK columns are NULL for this type (application-validated).
    grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="my-marketplace/web_sessions",
        assigned_by="admin@x.com",
    )
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/web_sessions") is True


def test_resource_grant_delete_then_has_grant_false(rbac_repos):
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    grants = repos["grants"]

    grp = groups.create(name="writers", created_by="admin@x.com")
    # Use 'marketplace_plugin' so no table_registry row is needed — the
    # per-type FK columns are NULL for this type (application-validated).
    grant_id = grants.create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id="my-marketplace/orders",
        assigned_by="admin@x.com",
    )
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/orders") is True
    grants.delete(grant_id)
    assert grants.has_grant([grp["id"]], "marketplace_plugin", "my-marketplace/orders") is False


def test_list_groups_for_user_returns_joined_groups(rbac_repos):
    repos, _, _ = rbac_repos
    users = repos["users"]
    groups = repos["groups"]
    members = repos["members"]

    users.create(id="u4", email="dave@example.com", name="Dave")
    g1 = groups.create(name="g-alpha", created_by="admin@x.com")
    g2 = groups.create(name="g-beta", created_by="admin@x.com")
    members.add_member("u4", g1["id"], source="admin")
    members.add_member("u4", g2["id"], source="admin")

    group_ids = members.list_groups_for_user("u4")
    assert set(group_ids) == {g1["id"], g2["id"]}


def test_system_group_rename_raises(rbac_repos):
    """Both engines must protect system groups from rename."""
    repos, _, _ = rbac_repos
    groups = repos["groups"]
    from src.repositories.user_groups import SystemGroupProtected

    # DuckDB _ensure_schema seeds Admin; PG fixture seeds it above
    admin = groups.get_by_name("Admin")
    assert admin is not None
    with pytest.raises(SystemGroupProtected):
        groups.update(admin["id"], name="Hacked")
