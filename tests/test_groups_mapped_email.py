"""Tests for /api/admin/groups origin + mapped_email surface.

Covers the admin-UI rule: when AGNES_GROUP_ADMIN_EMAIL /
AGNES_GROUP_EVERYONE_EMAIL map a Workspace group onto the seeded Admin /
Everyone system row, the row carries:

  - ``origin = 'google_sync'`` (the seed badge is suppressed —
    Workspace is the authoritative source for membership)
  - ``mapped_email`` = the Workspace group email

so the list / detail templates can render `Admin / admins@workspace.test`
with a green `google_sync` chip instead of `Admin / Admin` with the
yellow system chip. Without the env mapping, the same row stays a plain
`'system'` with no mapped_email.
"""

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


def _seed_admin():
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin", role="admin")
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid, create_access_token(user_id=uid, email="admin@test", role="admin")
    finally:
        conn.close()


def _groups_by_name(client: TestClient, token: str) -> dict:
    """Fetch /api/admin/groups, return {name: row} for assertion brevity."""
    resp = client.get("/api/admin/groups", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return {g["name"]: g for g in resp.json()}


def test_admin_row_origin_is_google_sync_when_env_mapped(fresh_db, monkeypatch):
    """When AGNES_GROUP_ADMIN_EMAIL is set, the seeded Admin row reports
    origin='google_sync' — the system badge is suppressed because
    Workspace is the authoritative source of membership for this row."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    groups = _groups_by_name(client, token)
    admin = groups["Admin"]

    assert admin["origin"] == "google_sync"
    assert admin["mapped_email"] == "admins@workspace.test"
    assert admin["is_google_managed"] is True


def test_everyone_row_origin_is_google_sync_when_env_mapped(fresh_db, monkeypatch):
    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    groups = _groups_by_name(client, token)
    everyone = groups["Everyone"]

    assert everyone["origin"] == "google_sync"
    assert everyone["mapped_email"] == "everyone@workspace.test"
    assert everyone["is_google_managed"] is True


def test_admin_row_is_plain_system_without_env_mapping(fresh_db):
    """Without AGNES_GROUP_ADMIN_EMAIL set, the seeded Admin row is just a
    regular system row — system chip, no mapped_email."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    groups = _groups_by_name(client, token)
    admin = groups["Admin"]

    assert admin["origin"] == "system"
    assert admin["mapped_email"] is None
    assert admin["is_google_managed"] is False


def test_user_created_google_sync_group_origin(fresh_db):
    """A Workspace-derived group whose `name` is the email itself reports
    origin='google_sync' and has null mapped_email — the email is already
    the canonical name."""
    from app.main import app
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    try:
        UserGroupsRepository(conn).create(
            name="finance@workspace.test",
            created_by="system:google-sync",
        )
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    groups = _groups_by_name(client, token)
    g = groups["finance@workspace.test"]

    assert g["origin"] == "google_sync"
    assert g["mapped_email"] is None
    assert g["is_google_managed"] is True


def test_admin_created_custom_group_origin(fresh_db):
    """Admin-created groups report origin='custom' — the value is named
    after the *origin* of the row, not the creator's role, so the chip
    doesn't visually clash with the seeded `Admin` system group."""
    from app.main import app
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    try:
        UserGroupsRepository(conn).create(name="data-team", created_by="admin@test")
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    groups = _groups_by_name(client, token)
    g = groups["data-team"]

    assert g["origin"] == "custom"
    assert g["mapped_email"] is None
    assert g["is_google_managed"] is False


# ── UI ────────────────────────────────────────────────────────────────────


def test_admin_groups_template_uses_mapped_email_in_subtitle(fresh_db):
    """List view JS must consult `g.mapped_email` for the subtitle so
    mapped Admin/Everyone show the Workspace email under the canonical
    name instead of `Admin / Admin`."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        "/admin/groups",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "g.mapped_email" in body


def test_access_overview_returns_origin_and_mapped_email(fresh_db, monkeypatch):
    """`/api/admin/access-overview` powers the /admin/access sidebar; the
    groups payload must carry the same origin / mapped_email / is_google_managed
    fields the dedicated /api/admin/groups endpoint exposes, so the sidebar
    can render the identical pill + subtitle treatment."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()

    resp = client.get(
        "/api/admin/access-overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    by_name = {g["name"]: g for g in data["groups"]}

    admin = by_name["Admin"]
    assert admin["origin"] == "google_sync"
    assert admin["mapped_email"] == "admins@workspace.test"
    assert admin["is_google_managed"] is True

    everyone = by_name["Everyone"]
    assert everyone["origin"] == "system"
    assert everyone["mapped_email"] is None
    assert everyone["is_google_managed"] is False


def test_admin_access_template_renders_origin_pill_and_mapped_email(fresh_db, monkeypatch):
    """The /admin/access page JS must read `origin` / `mapped_email` from
    each group so the sidebar gets the same pill + subtitle as
    /admin/groups. Pin the JS contract so a renderer regression that
    drops the consult on these fields fails CI."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        "/admin/access",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # JS reads these fields per group when rendering the sidebar.
    assert "g.origin" in body
    assert "g.mapped_email" in body
    assert "g.is_google_managed" in body
    # Origin chip CSS classes (multi-color) must be present so the pill renders.
    assert ".origin-google_sync" in body
    assert ".origin-system" in body
    assert ".origin-custom" in body


def test_user_groups_payload_carries_origin(fresh_db, monkeypatch):
    """`/api/users` returns each membership chip's origin so the user-list
    page can color the pill (yellow / gray / green / purple) without a
    second fetch."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        # Target user belongs to: Admin (mapped → google_sync), Everyone
        # (system, unmapped), data-team (custom), eng@workspace.test (google_sync).
        ug_repo = UserGroupsRepository(conn)
        custom_g = ug_repo.create(name="data-team", created_by="admin@test")
        gsync_g = ug_repo.create(name="eng@workspace.test", created_by="system:google-sync")
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        everyone_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]
        ).fetchone()[0]
        target_uid = str(uuid.uuid4())
        UserRepository(conn).create(
            id=target_uid, email="t@test", name="T", role="analyst",
        )
        members = UserGroupMembersRepository(conn)
        members.add_member(target_uid, admin_gid, source="google_sync")
        members.add_member(target_uid, everyone_gid, source="admin")
        members.add_member(target_uid, custom_g["id"], source="admin")
        members.add_member(target_uid, gsync_g["id"], source="google_sync")
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    target = next(u for u in resp.json() if u["id"] == target_uid)
    by_name = {g["name"]: g for g in target["groups"]}

    # Admin row is env-mapped → origin='google_sync' (matches /api/admin/groups).
    assert by_name["Admin"]["origin"] == "google_sync"
    # Everyone has no env mapping → stays 'system'.
    assert by_name["Everyone"]["origin"] == "system"
    # Custom + google-sync user-created groups carry their respective tags.
    assert by_name["data-team"]["origin"] == "custom"
    assert by_name["eng@workspace.test"]["origin"] == "google_sync"


def test_user_memberships_payload_carries_origin(fresh_db, monkeypatch):
    """`/api/admin/users/{id}/memberships` must carry `origin` so the
    user detail page can color-code the membership chips identically to
    the user list."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        ug_repo = UserGroupsRepository(conn)
        custom_g = ug_repo.create(name="data-team", created_by="admin@test")
        gsync_g = ug_repo.create(name="legal@workspace.test", created_by="system:google-sync")
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        target_uid = str(uuid.uuid4())
        UserRepository(conn).create(
            id=target_uid, email="t@test", name="T", role="analyst",
        )
        members = UserGroupMembersRepository(conn)
        members.add_member(target_uid, admin_gid, source="google_sync")
        members.add_member(target_uid, custom_g["id"], source="admin")
        members.add_member(target_uid, gsync_g["id"], source="google_sync")
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        f"/api/admin/users/{target_uid}/memberships",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    by_name = {m["group_name"]: m for m in resp.json()}

    # env-mapped Admin → google_sync (matches /api/admin/groups behavior)
    assert by_name["Admin"]["origin"] == "google_sync"
    assert by_name["data-team"]["origin"] == "custom"
    assert by_name["legal@workspace.test"]["origin"] == "google_sync"


def test_effective_access_lists_explicit_grants_for_admin_user(fresh_db):
    """`/api/admin/users/{id}/effective-access` no longer short-circuits
    for admins — they get the same per-resource breakdown as everyone
    else, so an operator auditing a target user can see precisely which
    grants the Admin group carries via which group, instead of a flat
    "Full access" pill that hides the wiring. Authorization at runtime
    still gives Admin god-mode regardless of this list."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    # Set up: a target admin user belongs to the Admin group AND a
    # custom "data-team" group. The Admin group has no explicit grants;
    # data-team has one. The endpoint should list the data-team grant.
    conn = get_system_db()
    try:
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        custom_g = UserGroupsRepository(conn).create(
            name="data-team", created_by="admin@test",
        )
        target_uid = str(uuid.uuid4())
        UserRepository(conn).create(
            id=target_uid, email="t-admin@test", name="T", role="admin",
        )
        members = UserGroupMembersRepository(conn)
        members.add_member(target_uid, admin_gid, source="admin")
        members.add_member(target_uid, custom_g["id"], source="admin")
        ResourceGrantsRepository(conn).create(
            group_id=custom_g["id"],
            resource_type="plugin",
            resource_id="agnes/foo",
        )
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        f"/api/admin/users/{target_uid}/effective-access",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # is_admin still reflects reality; the UI just doesn't short-circuit on it.
    assert data["is_admin"] is True
    # The actual grant list is no longer empty — `data-team` carries one.
    rids = [it["resource_id"] for it in data["items"]]
    assert "agnes/foo" in rids


def test_profile_template_renders_color_coded_membership_chips(fresh_db, monkeypatch):
    """The /profile page must render group memberships with the same
    chip vocabulary used on the user list / detail pages: a colored
    .group-chip with class derived from name (Admin / Everyone) first
    and origin (google_sync / custom) second. google_sync chip text is
    shortened via the prefix-strip logic and the raw email sits on the
    chip's title attribute for hover reveal."""
    monkeypatch.setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_foundryai_")
    from app.main import app
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    admin_uid, token = _seed_admin()
    conn = get_system_db()
    try:
        # Add the seeded admin to a Workspace-derived group so the
        # rendered profile page actually has a green chip we can grep
        # for. The chip text should be "Legal" (prefix stripped,
        # capitalized); the title attribute should keep the raw email.
        ug_repo = UserGroupsRepository(conn)
        gsync = ug_repo.create(
            name="grp_foundryai_legal@workspace.test",
            created_by="system:google-sync",
        )
        UserGroupMembersRepository(conn).add_member(
            admin_uid, gsync["id"], source="google_sync",
        )
    finally:
        conn.close()

    client = TestClient(app)
    resp = client.get(
        "/profile",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Chip CSS classes from the shared vocabulary.
    assert ".group-chip.is-admin" in body
    assert ".group-chip.is-google_sync" in body
    assert ".group-chip.is-custom" in body
    # Admin row gets the canonical-name chip class (server-side rendered).
    assert 'class="group-chip is-admin"' in body
    # Workspace-derived group's chip text is the shortened display name;
    # the raw email lives in the title attribute for hover reveal.
    assert ">Legal<" in body
    assert 'title="grp_foundryai_legal@workspace.test"' in body


def test_my_effective_access_lists_explicit_grants_for_admin_user(fresh_db):
    """`/api/me/effective-access` (the /profile page surface) mirrors
    /api/admin/users/{id}/effective-access — admins see their explicit
    grant breakdown rather than a flat "Full access" short-circuit. Same
    rationale as the admin-side endpoint: audit the grant graph, not the
    runtime god-mode."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    # _seed_admin already adds the admin to the Admin group; we layer on
    # a custom group with one grant so the response actually has items.
    admin_uid, token = _seed_admin()
    conn = get_system_db()
    try:
        custom_g = UserGroupsRepository(conn).create(
            name="data-team", created_by="admin@test",
        )
        UserGroupMembersRepository(conn).add_member(
            admin_uid, custom_g["id"], source="admin",
        )
        ResourceGrantsRepository(conn).create(
            group_id=custom_g["id"],
            resource_type="plugin",
            resource_id="agnes/foo",
        )
    finally:
        conn.close()

    client = TestClient(app)
    resp = client.get(
        "/api/me/effective-access",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_admin"] is True
    rids = [it["resource_id"] for it in data["items"]]
    assert "agnes/foo" in rids


def test_profile_template_drops_full_access_pill(fresh_db):
    """The /profile page no longer renders the gold "Full access via
    Admin" empty-state for admin users — it should fall through to the
    grant list (or the generic "no resource access" message). Pinning
    the absence of the old branch."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        "/profile",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Full access via Admin" not in body
    assert "You can read and write everything in Agnes regardless" not in body


def test_admin_user_detail_template_drops_full_access_pill(fresh_db):
    """The `ea-admin-pill` short-circuit branch is gone — a regression
    that re-adds a special-case render for admins would slip through if
    we don't pin its absence."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    target_uid = _create_user("v3@test")
    resp = client.get(
        f"/admin/users/{target_uid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # No longer references the "Full access via the Admin group" branch.
    assert "Full access via the Admin group" not in body
    assert "ea-admin-pill" not in body


def test_user_detail_dropdown_hides_google_managed_groups(fresh_db):
    """The "Add to group" dropdown on /admin/users/{id} must skip any
    row with `is_google_managed=true` — membership for those groups is
    owned by Workspace and the API 409s on POST anyway. Pin the JS
    contract so a regression that drops the filter (and floods the
    picker with un-grantable options) surfaces in CI."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    target_uid = _create_user("victim2@test")
    resp = client.get(
        f"/admin/users/{target_uid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The filter sits inside the picker-population loop.
    assert "g.is_google_managed" in body


def test_admin_user_detail_template_uses_color_coded_chips(fresh_db):
    """Detail page must declare the same chip CSS classes + reference
    `m.origin` and `deriveDisplayName` in the membership renderer so a
    regression that drops the rebuild surfaces in CI."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    target_uid = _create_user("victim@test")
    resp = client.get(
        f"/admin/users/{target_uid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Color classes match the user list's chip vocabulary.
    assert ".group-chip.is-admin" in body
    assert ".group-chip.is-everyone" in body
    assert ".group-chip.is-google_sync" in body
    assert ".group-chip.is-custom" in body
    # JS reads m.origin to pick the chip class.
    assert "m.origin" in body
    # google_sync chip text runs through deriveDisplayName.
    assert "deriveDisplayName" in body


def _create_user(email: str) -> str:
    """Inline helper for the membership UI test — not reused above."""
    import uuid as _uuid
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        uid = str(_uuid.uuid4())
        UserRepository(conn).create(id=uid, email=email, name="V", role="analyst")
        return uid
    finally:
        conn.close()


def test_admin_users_template_renders_color_coded_chips(fresh_db):
    """Pin the JS contract: the user list assigns chip classes based on
    name (Admin / Everyone) first and falls back to `is-${origin}` so
    google_sync chips go green and custom chips go purple. A renderer
    regression that drops the consult on g.origin would surface here.
    Also pin the deriveDisplayName shortening for google-sync chips —
    they must show "Legal" rather than the raw Workspace email so the
    membership cell stays readable."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    body = resp.text
    # The four chip-color classes that style the pills.
    assert ".group-chip.is-admin" in body
    assert ".group-chip.is-everyone" in body
    assert ".group-chip.is-google_sync" in body
    assert ".group-chip.is-custom" in body
    # JS reads g.origin to pick the class for non-Admin / non-Everyone rows.
    assert "g.origin" in body
    # google_sync chips run their name through deriveDisplayName so the
    # cell shows "Legal" rather than the full Workspace email; the raw
    # email goes into the chip's `title` (hover reveal).
    assert "deriveDisplayName" in body


def test_admin_group_detail_template_uses_mapped_email_subtitle(fresh_db, monkeypatch):
    """Detail page Jinja must render `mapped_email` as the subtitle when
    the row is the env-mapped Admin/Everyone, instead of the canonical
    name (which would yield `Admin / Admin`)."""
    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db

    conn = get_system_db()
    try:
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
    finally:
        conn.close()

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        f"/admin/groups/{admin_gid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The mapped Workspace email shows up as the gd-title-email subtitle.
    assert "admins@workspace.test" in body
    # The data attribute the JS reads to skip the deriveDisplayName rewrite.
    assert 'data-mapped-email="admins@workspace.test"' in body
