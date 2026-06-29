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
      - Marketplace block is always emitted (Fix B in 2026-05-10
        init-report response): anonymous visitors with no plugin grants
        still get the marketplace registration step so the SessionStart
        hook is pre-wired. Confirm = step 8.
    """
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # Unified flow markers.
    assert "agnes init" in text
    # Legacy admin-only login verbs are gone from the rendered prompt.
    assert "agnes auth import-token" not in text
    # Always-on layout (preflight + marketplace + MCP + connectors block all
    # unconditional; skills step deleted in #242): Confirm = step 9.
    assert "10) Confirm:" in text


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
    """When the caller has a non-empty served stack, the marketplace block
    renders the "install your current stack" copy variant. Confirm stays
    at step 8 in the post-skills-removal layout (preflight + marketplace
    + MCP all always-on regardless of stack contents).

    Stub `marketplace_filter.resolve_user_marketplace` to return a
    plugin so we don't have to seed the full marketplace plumbing in
    this test — we're verifying the layout, not the RBAC resolver
    itself (covered by `test_marketplace_filter`).

    Post-Model B (v28+): the setup page reads from
    `resolve_user_marketplace` (which gates on explicit subscriptions)
    rather than `resolve_allowed_plugins` (RBAC-only)."""
    from app.web.router import get_optional_user
    from fastapi import Request
    from src import marketplace_filter

    async def _admin_user(request: Request):  # type: ignore[no-redef]
        return {"id": "admin-1", "email": "admin@example.com",
                "is_admin": True, "name": "Admin", "groups": ["Admin"]}

    monkeypatch.setattr(
        marketplace_filter,
        "resolve_user_marketplace",
        lambda conn, user: [{"manifest_name": "demo-plugin"}],
    )

    client.app.dependency_overrides[get_optional_user] = _admin_user
    try:
        resp = client.get("/setup", follow_redirects=True)
    finally:
        client.app.dependency_overrides.pop(get_optional_user, None)

    assert resp.status_code == 200
    text = resp.text
    # Marketplace block marker. The per-plugin install lines moved inside
    # `agnes refresh-marketplace --bootstrap`, so we check the section
    # header + the one-liner instead of `claude plugin install <name>@agnes`.
    # Non-empty stack → "install plugins" header variant.
    assert "Register the Agnes Claude Code marketplace and install plugins" in text
    assert "agnes refresh-marketplace --bootstrap" in text
    # Layout shift: Confirm is now step 9 (preflight + marketplace + MCP +
    # connectors all always-on; skills step deleted in #242).
    assert "10) Confirm:" in text
    # Pre-flight is in the rendered prompt at step 4.
    assert "Make sure git and claude are installed" in text
    # Atlassian MCP registration is at step 6.
    assert "claude mcp add --transport sse atlassian" in text


def test_install_legacy_path_redirects_to_setup(client):
    """`/install` legacy path keeps redirecting to `/setup` (302/307)."""
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]


def test_first_time_setup_renders_all_wizard_fields(client):
    """The first-time-setup wizard (`setup.html`, served at /first-time-setup)
    still renders every field end-to-end through the real Jinja/route stack
    after the base_login.html → base_ds.html migration (#586).

    The `client` fixture builds a fresh app rooted at tmp_path with no seeded
    users, so /first-time-setup serves the wizard rather than redirecting to
    /login. We assert all four steps, the four progress dots, the key inputs,
    and that the page rides the design-system narrow shell — proving no field
    was lost when the login-card wrapper divs were removed and that the page
    is genuinely on base_ds (footer copyright + nav header), not base_login.
    """
    resp = client.get("/first-time-setup", follow_redirects=False)
    assert resp.status_code == 200
    text = resp.text
    # Every wizard field marker survives the wrapper-div removal.
    for marker in (
        'id="admin-email"',
        'id="admin-password"',
        'id="data-source"',
        'id="step-1"',
        'id="step-2"',
        'id="step-3"',
        'id="step-4"',
        'id="step-dot-1"',
        'id="step-dot-2"',
        'id="step-dot-3"',
        'id="step-dot-4"',
    ):
        assert marker in text, f"wizard field marker missing: {marker}"
    # Design-system shell marker — the page now opts into the narrow container.
    assert "container--narrow" in text
    # base_ds-only chrome the old base_login lacked: footer copyright marker
    # (`&copy;` from base_ds's <footer>) confirms the page is genuinely on the
    # design-system base, not just textually edited.
    assert "&copy;" in text or "<footer>" in text
    # The bespoke login-card chrome is gone.
    assert "max-width: 520px" not in text
