"""Tests for the unified `/setup` route.

The previous `?role=analyst|admin` query parameter is gone. The route
renders a single layout for everyone — admin-vs-analyst is no longer a
branch. The marketplace + plugins block is gated by per-user
`resource_grants` resolved inside `compute_default_agent_prompt`.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient against a freshly-built FastAPI app rooted at tmp_path.

    Mirrors the `web_client` fixture in tests/test_web_ui.py — we re-create
    the app so the DuckDB singleton picks up the per-test DATA_DIR rather
    than leaking state across tests on the same xdist worker.
    """
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


def test_setup_page_renders_unified_layout(client):
    """Bare `/setup` (no query param) renders the unified flow:

      - `agnes init` is mandatory (subsumes the old admin-only
        `agnes auth import-token` + `agnes auth whoami` pair).
      - Anonymous visitors with no plugin grants get the no-marketplace
        layout (Confirm = step 6).
    """
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # Unified flow markers.
    assert "agnes init" in text
    # Legacy admin-only login verbs are gone from the rendered prompt.
    assert "agnes auth import-token" not in text
    # No-marketplace layout: Confirm = step 6.
    assert "6) Confirm:" in text


def test_setup_page_ignores_role_query_param(client):
    """`?role=...` is no longer accepted by the route signature. FastAPI
    ignores unknown query params silently — `/setup?role=admin` still
    serves the unified layout. No 422, no redirect, no behavior delta
    vs. bare `/setup`."""
    bare = client.get("/setup", follow_redirects=True)
    with_role = client.get("/setup?role=admin", follow_redirects=True)
    assert bare.status_code == 200
    assert with_role.status_code == 200
    # Both responses contain the unified-flow marker.
    assert "agnes init" in bare.text
    assert "agnes init" in with_role.text
    # Legacy admin-only login verbs are gone from both.
    assert "agnes auth import-token" not in bare.text
    assert "agnes auth import-token" not in with_role.text


def test_setup_page_renders_marketplace_for_user_with_grants(client, monkeypatch):
    """When the caller has plugin grants in `resource_grants`, the
    unified flow inserts the marketplace + plugins block (step 5) and
    Confirm shifts to step 8.

    Stub `marketplace_filter.resolve_allowed_plugins` to return a
    plugin so we don't have to seed the full marketplace plumbing in
    this test — we're verifying the layout switch, not the RBAC
    resolver itself (covered by `test_marketplace_filter`)."""
    from app.web.router import get_optional_user
    from fastapi import Request
    from src import marketplace_filter

    async def _admin_user(request: Request):  # type: ignore[no-redef]
        return {"id": "admin-1", "email": "admin@example.com",
                "is_admin": True, "name": "Admin", "groups": ["Admin"]}

    monkeypatch.setattr(
        marketplace_filter,
        "resolve_allowed_plugins",
        lambda conn, user: [{"manifest_name": "demo-plugin"}],
    )

    client.app.dependency_overrides[get_optional_user] = _admin_user
    try:
        resp = client.get("/setup", follow_redirects=True)
    finally:
        client.app.dependency_overrides.pop(get_optional_user, None)

    assert resp.status_code == 200
    text = resp.text
    # Marketplace block markers.
    assert "claude plugin install demo-plugin@agnes" in text
    # Layout shift: Confirm is now step 8 (was 6 without marketplace).
    assert "8) Confirm:" in text
    # Pre-flight is in the rendered prompt at step 4.
    assert "Make sure git and claude are installed" in text


def test_install_legacy_path_redirects_to_setup(client):
    """`/install` legacy path keeps redirecting to `/setup` (302/307)."""
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]
