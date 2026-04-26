"""Tests for the /admin/role-mapping UI page (v9 role management).

The page renders shell HTML; all role data (internal_roles, group_mappings)
is loaded client-side via the admin REST API. These tests cover the auth
gate + page-shell markers + sanity checks that the form, the role list
table, and the mappings list table are all in the rendered DOM.

Direct API behavior is owned by the API agent's tests — here we only verify
that the page renders the right shell so the JS can drive against it.
"""

import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email: str, role: str):
    """Create a user and return (uid, session_jwt). Mirrors test_admin_tokens_ui."""
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0], role=role)
    token = create_access_token(user_id=uid, email=email, role=role)
    return uid, token


# ── Auth gate ────────────────────────────────────────────────────────────


def test_admin_can_render_role_mapping_page(fresh_db):
    """Admin GET /admin/role-mapping: 200 with all section markers."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Page-shell markers
    assert 'data-page="role-mapping"' in body
    assert "Role mapping" in body

    # Section 1: Internal roles list
    assert 'id="roles-table"' in body
    assert 'id="roles-tbody"' in body
    assert "Internal roles" in body

    # Section 2: Group → role mappings list
    assert 'id="mappings-table"' in body
    assert 'id="mappings-tbody"' in body
    assert "Group to role mappings" in body

    # Section 2: Create-mapping form
    assert 'id="mapping-form"' in body
    assert 'id="new-external-group"' in body
    assert 'id="new-role-key"' in body
    assert 'id="create-mapping-btn"' in body

    # JS-side endpoint constants
    assert "/api/admin/internal-roles" in body
    assert "/api/admin/group-mappings" in body


def test_non_admin_cannot_access_role_mapping_page(fresh_db):
    """Non-admin GET /admin/role-mapping: 401/403 (admin-only route)."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, "user@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        headers={"Accept": "text/html"},
        cookies={"access_token": sess},
        follow_redirects=False,
    )
    # require_role(Role.ADMIN) → require_internal_role("core.admin") returns 403
    assert resp.status_code in (302, 401, 403), resp.text


def test_unauthenticated_redirects_or_blocks(fresh_db):
    """Unauthenticated GET /admin/role-mapping: 401/redirect, never 200."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 401, 403), resp.text


# ── Internal roles section: shows core.* rows correctly ──────────────────


def test_role_mapping_page_lists_core_roles_from_seed(fresh_db):
    """The internal_roles seed runs at DB init; the page JS fetches them
    from /api/admin/internal-roles. Verify the SEED rows are present in
    the DB so the API can serve them once the API agent ships their
    endpoint — this test guards the shared contract.
    """
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT key, is_core FROM internal_roles WHERE is_core = true ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
        close_system_db()

    keys = [r[0] for r in rows]
    assert "core.viewer" in keys
    assert "core.analyst" in keys
    assert "core.km_admin" in keys
    assert "core.admin" in keys
    # All four core roles flagged is_core=true
    assert all(r[1] is True for r in rows)


# ── Group mapping form + list ────────────────────────────────────────────


def test_role_mapping_page_renders_form_action_endpoints(fresh_db):
    """Verify the page contains the JS endpoints the create/delete flow
    targets — guards against accidental URL drift between UI and API."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Both the GET (load) and the create POST hit the same base URL.
    assert "API_GROUP_MAPPINGS = \"/api/admin/group-mappings\"" in body
    assert "deleteMapping" in body
    assert "createMapping" in body


def test_role_mapping_page_form_submits_create_mapping_payload(fresh_db, monkeypatch):
    """Smoke test: the form-submit JS calls fetch with the right shape.

    We don't have the API endpoint to drive end-to-end, so we verify the
    JS body shape that the form will POST: a JSON object with
    external_group_id + role_key. This guards the parent agent's contract.
    """
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # The createMapping POST body shape — these strings keep the contract
    # honest if someone refactors the JS.
    assert "external_group_id" in body
    assert "role_key" in body
    # Delete uses DELETE method against the per-id URL.
    assert 'method: "DELETE"' in body


# ── Internal roles table renders the right columns ────────────────────────


def test_role_mapping_page_internal_roles_table_columns(fresh_db):
    """The roles table header lists the columns the brief specified:
    role, description, owner module, type, mappings count, grants count."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Header columns rendered in the static HTML (sortable by JS later).
    for column in ("Role", "Description", "Owner module", "Type", "Mappings", "Direct grants"):
        assert column in body, f"missing column header: {column}"


def test_role_mapping_page_navigation_link_visible_to_admin(fresh_db):
    """Admin sees the 'Role mapping' link in the global header nav."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    # Render any admin page so the header is included in the response.
    resp = client.get(
        "/admin/users",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The nav link appears in admin pages (header partial).
    assert 'href="/admin/role-mapping"' in body


# ── Known-groups picker (group ID discovery UX) ──────────────────────────


def test_known_groups_panel_renders_empty_state_with_helpful_copy(fresh_db):
    """Fresh DB + JWT-auth admin (no session google_groups, no mappings):
    the picker still renders, with copy that tells the admin where the
    chips will come from once they sign in via Google or set
    LOCAL_DEV_GROUPS. Avoids a confusing blank panel."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Panel marker present.
    assert 'data-testid="known-groups"' in body
    assert "Known groups" in body
    # Empty-state copy guides the operator.
    assert "session.google_groups" in body or "LOCAL_DEV_GROUPS" in body


def test_known_groups_panel_renders_chip_for_existing_mapping(fresh_db):
    """A previously-mapped external group ID surfaces as a chip with the
    'already mapped' tag — so the admin can pick a known-good ID without
    typing it. ID-only because we don't re-fetch the directory per render."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from src.repositories.group_mappings import GroupMappingsRepository
    from src.repositories.internal_roles import InternalRolesRepository
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        # Seed a mapping so its external_group_id surfaces in the picker.
        analyst_role = InternalRolesRepository(conn).get_by_key("core.analyst")
        GroupMappingsRepository(conn).create(
            id=str(uuid.uuid4()),
            external_group_id="groups/known-dev-team",
            internal_role_id=analyst_role["id"],
            assigned_by="admin@t",
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/role-mapping",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    assert 'data-group-id="groups/known-dev-team"' in body
    assert "already mapped" in body


def test_role_mapping_nav_link_hidden_for_non_admin(fresh_db):
    """Non-admin (analyst) does not see the Role mapping nav link."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, "user@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    # /dashboard renders the same header partial.
    resp = client.get(
        "/dashboard",
        cookies={"access_token": sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'href="/admin/role-mapping"' not in body
