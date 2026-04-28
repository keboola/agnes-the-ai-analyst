"""Tests for the per-user detail page (/admin/users/{user_id}).

v12 status: the legacy v9 capabilities UI (core-role dropdown / additional
capabilities checkboxes / effective-roles debug section) is gone — the
new /admin/users/{id} page surfaces group memberships and an effective
resource-access readout instead. The whole module asserts v9 markers and
hits removed REST endpoints (/api/role-grants, /api/admin/users/.../core-role)
so it's skipped en bloc until rewritten against the v12 layout.
"""

import tempfile
import uuid

import pytest

pytest.skip(
    "v12: legacy capabilities UI replaced by group memberships + effective "
    "access. Rewrite the module against templates/admin_user_detail.html "
    "(v12) and /api/admin/users/{id}/memberships endpoints.",
    allow_module_level=True,
)


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email: str, role: str):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from tests.helpers.auth import grant_admin

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0], role=role)
    if role == "admin":
        grant_admin(conn, uid)
    token = create_access_token(user_id=uid, email=email, role=role)
    return uid, token


# ── Auth gate ────────────────────────────────────────────────────────────


def test_admin_can_render_user_detail_page(fresh_db):
    """Admin GET /admin/users/{user_id}: 200 with all section markers."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Page-shell markers
    assert "victim@t" in body
    assert target_uid in body

    # All three sections are present
    assert 'data-section="core-role"' in body
    assert 'data-section="capabilities"' in body
    assert 'data-section="effective-roles"' in body


def test_user_detail_page_renders_core_role_dropdown(fresh_db):
    """Section A: core role single-select dropdown is present."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Section A markers
    assert 'id="core-role-select"' in body
    assert "Core role" in body
    # JS endpoint references
    assert "/api/admin/internal-roles" in body
    assert "/api/admin/users/" in body
    assert "/role-grants" in body
    assert "/effective-roles" in body


def test_user_detail_page_renders_capabilities_list(fresh_db):
    """Section B: capabilities list element is in the rendered page."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Section B markers — caps list container + loading state
    assert 'id="caps-list"' in body
    assert "Additional capabilities" in body
    # Toggle handler presence
    assert "toggleCapability" in body


def test_user_detail_page_renders_effective_roles_section(fresh_db):
    """Section C: effective-roles debug view with the three lists."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # Section C — three list containers for direct/groups/expanded
    assert 'id="effective-direct"' in body
    assert 'id="effective-groups"' in body
    assert 'id="effective-expanded"' in body
    # Section labels
    assert "Direct grants" in body
    assert "Group-derived grants" in body
    assert "Expanded set" in body


def test_user_detail_page_unknown_user_returns_404(fresh_db):
    """GET /admin/users/{nonexistent}: 404 (UI surface check, not API)."""
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
        "/admin/users/00000000-0000-0000-0000-000000000000",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 404, resp.text


def test_non_admin_cannot_access_user_detail_page(fresh_db):
    """Non-admin GET /admin/users/{id}: 401/403."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        analyst_uid, sess = _make_user_and_session(conn, "user@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{analyst_uid}",
        cookies={"access_token": sess},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 401, 403), resp.text


# ── Toggle and core-role-change JS contract ──────────────────────────────


def test_user_detail_page_toggle_capability_uses_role_grants_api(fresh_db):
    """The capability toggle must POST/DELETE against the role-grants endpoint
    so the API agent's URL contract is honored."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        cookies={"access_token": admin_sess},
    )
    body = resp.text
    # POST to role-grants with role_key body for grant
    assert "role_key" in body
    # DELETE to role-grants/{grant_id} for revoke
    assert 'method: "POST"' in body
    assert 'method: "DELETE"' in body


def test_user_detail_page_core_role_change_deletes_then_creates(fresh_db):
    """Section A flow: changing core role deletes old grants then POSTs the new one,
    matching the brief's "delete+create dance from the client" requirement."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        target_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        f"/admin/users/{target_uid}",
        cookies={"access_token": admin_sess},
    )
    body = resp.text
    # The changeCoreRole function name
    assert "changeCoreRole" in body
    # The JS comment / logic to filter core.* grants is present.
    assert 'core.' in body or "core_role" in body


# ── Add-user form (Part 3 verification: writes to user_role_grants) ─────


def test_create_user_via_api_grants_core_role(fresh_db):
    """Verify the existing add-user flow auto-grants core.{role}.

    The brief asks us to verify (not modify) that the Add-user modal's
    POST hits an endpoint that routes through UserRepository.create(),
    which inserts a user_role_grants row. This pins the contract: a
    fresh user must end up with core.{role} in user_role_grants.
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
    resp = client.post(
        "/api/users",
        cookies={"access_token": admin_sess},
        json={"email": "newbie@t", "name": "Newbie", "role": "analyst", "send_invite": False},
    )
    assert resp.status_code == 201, resp.text
    new_user = resp.json()
    new_uid = new_user["id"]

    # Now check user_role_grants directly — confirm core.analyst is present.
    conn = get_system_db()
    try:
        rows = conn.execute(
            """SELECT r.key
               FROM user_role_grants g
               JOIN internal_roles r ON g.internal_role_id = r.id
               WHERE g.user_id = ?""",
            [new_uid],
        ).fetchall()
        keys = [r[0] for r in rows]
    finally:
        conn.close()
        close_system_db()
    assert "core.analyst" in keys, (
        "UserRepository.create should auto-grant core.{role}; "
        f"got {keys}"
    )


def test_admin_users_page_renders_detail_link(fresh_db):
    """The /admin/users list now links to /admin/users/{id} — verify the
    JS that builds row HTML emits the right href."""
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
        "/admin/users",
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200
    body = resp.text
    # The <a href> template literal in renderUsers includes /admin/users/${u.id}
    assert "/admin/users/${encodeURIComponent(u.id)}" in body
