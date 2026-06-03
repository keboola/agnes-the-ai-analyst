"""TDD-first endpoint integration tests for the users + access cluster.

Written BEFORE the callsite swap is fully proven. These tests must pass
on BOTH backends through ``seeded_app_both`` — the same test runs once
against DuckDB (legacy) and once against Postgres (post-cutover).

If any test regresses after a callsite swap, the swap is wrong — revert.

Covered endpoints (load-bearing, not exhaustive):

  * GET   /api/users                          — admin lists every user
  * GET   /api/users/{id}                     — admin reads one user
  * GET   /api/admin/groups                   — admin lists user groups
  * POST  /api/admin/groups                   — admin creates a group
  * GET   /api/admin/groups/{id}              — admin reads a group
  * POST  /api/admin/groups/{id}/members      — adds a user to a group
  * GET   /api/admin/groups/{id}/members      — lists group members
  * POST  /api/me/onboarded                   — flips users.onboarded
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# /api/users (admin-only)
# ---------------------------------------------------------------------------

def test_get_users_lists_seeded_users_for_admin(seeded_app_both):
    """GET /api/users returns the seeded users for an admin caller."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    r = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    # response is a bare list of UserResponse per `response_model=List[UserResponse]`
    users = r.json()
    assert isinstance(users, list)
    emails = {u["email"] for u in users}
    assert "admin@test.com" in emails
    assert "analyst@test.com" in emails


def test_get_users_forbids_non_admin(seeded_app_both):
    """GET /api/users 403s for non-admin callers (analyst is Everyone-only)."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    r = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 403, r.text


def test_get_user_by_id(seeded_app_both):
    """GET /api/users/{id} returns a single user for admin."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    r = client.get(
        "/api/users/analyst1",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    user = r.json()
    assert user["id"] == "analyst1"
    assert user["email"] == "analyst@test.com"


# ---------------------------------------------------------------------------
# /api/me/onboarded — exercises both auth lookup AND a state mutation
# ---------------------------------------------------------------------------

def test_post_onboarded_flips_flag(seeded_app_both):
    """POST /api/me/onboarded persists the flag for the authenticated user."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    r = client.post(
        "/api/me/onboarded",
        json={"onboarded": True, "source": "self_acknowledged"},
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["onboarded"] is True

    # Verify via the repo factory — backend-correct lookup
    from src.repositories import users_repo
    row = users_repo().get_by_id("analyst1")
    assert row["onboarded"] is True


# ---------------------------------------------------------------------------
# /api/admin/groups
# ---------------------------------------------------------------------------

def test_list_groups_returns_system_seeds(seeded_app_both):
    """The seed step (DuckDB _seed_system_groups OR the PG fixture seed)
    must produce both Admin and Everyone groups."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    r = client.get(
        "/api/admin/groups",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    groups = r.json()
    assert isinstance(groups, list)
    names = {g["name"] for g in groups}
    assert "Admin" in names
    assert "Everyone" in names


def test_create_group_then_list_includes_it(seeded_app_both):
    """POST /api/admin/groups → GET shows the new group."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    r = client.post(
        "/api/admin/groups",
        json={"name": "data-team", "description": "Data analysts"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201), r.text
    created = r.json()
    assert created["name"] == "data-team"
    assert "id" in created

    r = client.get(
        "/api/admin/groups",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    names = {g["name"] for g in r.json()}
    assert "data-team" in names


def test_create_group_forbidden_for_non_admin(seeded_app_both):
    """Non-admin cannot create a group (require_admin gate)."""
    client = seeded_app_both["client"]
    analyst_token = seeded_app_both["analyst_token"]
    r = client.post(
        "/api/admin/groups",
        json={"name": "rogue", "description": "..."},
        headers={"Authorization": f"Bearer {analyst_token}"},
    )
    assert r.status_code == 403, r.text


def test_get_group_by_id(seeded_app_both):
    """GET /api/admin/groups/{id} returns a single group."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    create = client.post(
        "/api/admin/groups",
        json={"name": "for-detail-test", "description": "x"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code in (200, 201), create.text
    gid = create.json()["id"]

    r = client.get(
        f"/api/admin/groups/{gid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "for-detail-test"


# ---------------------------------------------------------------------------
# group memberships
# ---------------------------------------------------------------------------

def test_add_user_to_group_then_membership_visible(seeded_app_both):
    """End-to-end: create group, add analyst, verify membership."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]

    create = client.post(
        "/api/admin/groups",
        json={"name": "members-test", "description": "y"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code in (200, 201), create.text
    gid = create.json()["id"]

    r = client.post(
        f"/api/admin/groups/{gid}/members",
        json={"email": "analyst@test.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201, 204), r.text

    # GET list
    r = client.get(
        f"/api/admin/groups/{gid}/members",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    members = r.json()
    # MemberResponse uses user_id; repo dicts use id — accept either
    member_user_ids = {m.get("user_id") or m.get("id") for m in members}
    assert "analyst1" in member_user_ids

    # Cross-check via factory — agnostic to backend
    from src.repositories import user_group_members_repo
    factory_members = user_group_members_repo().list_members_for_group(gid)
    factory_ids = {m.get("user_id") or m.get("id") for m in factory_members}
    assert "analyst1" in factory_ids


# ---------------------------------------------------------------------------
# audit_log written by endpoint mutations
# ---------------------------------------------------------------------------

def test_create_group_writes_audit_entry(seeded_app_both):
    """Mutations land in audit_log regardless of backend."""
    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]

    r = client.post(
        "/api/admin/groups",
        json={"name": "audited-group", "description": "x"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201), r.text

    from src.repositories import audit_repo
    rows, _ = audit_repo().query(action_prefix="user_group.", limit=20)
    actions = [r_["action"] for r_ in rows]
    assert any(a.startswith("user_group.") for a in actions), (
        f"no user_group.* audit row found in {actions}"
    )
