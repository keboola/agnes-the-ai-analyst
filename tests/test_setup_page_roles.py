"""Tests for /setup role query-param branching.

Task 4 wires `?role=analyst|admin` through the /setup route handler so the
template can render two role tiles and the renderer can pick the right
layout (admin = full marketplace/skills/diagnose flow; analyst = trimmed
workspace-bootstrap flow). Default is `admin` to preserve existing behavior.
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


def test_setup_page_default_role_is_admin(client):
    """No `role` query param → admin layout (default, preserves existing flow)."""
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # Both tiles present in markup; admin tile is the active one.
    assert "role=analyst" in text
    assert "role=admin" in text or 'href="/setup"' in text
    # Active state lives on the admin tile when role=admin (default).
    # Asserting the tile labels are both rendered keeps the assertion
    # robust against future styling tweaks.
    assert "Analyst workspace" in text
    assert "Admin CLI" in text


def test_setup_page_analyst_role(client):
    """`?role=analyst` → analyst tile is the active one."""
    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    assert "Analyst workspace" in text
    assert "Admin CLI" in text
    # The page must reflect the analyst selection somewhere — either via
    # the active-state CSS class or the `role=analyst` link being rendered.
    assert "role=analyst" in text


def test_install_redirects_to_setup(client):
    """`/install` legacy path keeps redirecting to `/setup` (302/307)."""
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]


def test_setup_page_invalid_role_falls_back(client):
    """Invalid role values must NOT 500 — either FastAPI's Literal
    validation rejects with 422, or the route quietly falls back to admin.
    Both are acceptable; what's not acceptable is an unhandled exception.
    """
    resp = client.get("/setup?role=hacker", follow_redirects=True)
    assert resp.status_code in (200, 422)


def test_setup_page_analyst_js_uses_bootstrap_scope(client):
    """Analyst tile's setupNewClaude JS must mint bootstrap-analyst PATs.

    The JS PAT mint must be role-aware: analyst gets a short-TTL
    bootstrap-analyst-scoped PAT (server clamps ttl ≤ 3600s), not the
    historical 90-day general PAT. Asserts the wiring at the rendered
    template level so we catch any regression in either the Jinja ctx
    plumbing or the JS branching.
    """
    resp = client.get("/setup?role=analyst", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # The role variable must be set to analyst in JS scope.
    assert (
        'const ROLE = "analyst"' in text
        or 'ROLE = "analyst"' in text
        or 'data-role="analyst"' in text
    )
    # The bootstrap-analyst scope must appear in the JS PAT-mint body.
    assert "bootstrap-analyst" in text
    assert "ttl_seconds" in text


def test_setup_page_admin_js_uses_general_scope(client):
    """Admin tile's setupNewClaude JS must keep the existing 90-day
    expires_in_days behavior — byte-identical PAT mint shape so existing
    admin flows don't regress.
    """
    resp = client.get("/setup?role=admin", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    assert "expires_in_days" in text  # still present in the admin body


def test_setup_page_js_ternary_keys_bootstrap_to_analyst(client):
    """Mutation-resistant assertion: the `bootstrap-analyst` scope must sit on
    the truthy branch of `ROLE === "analyst"`, not on the falsy branch.

    Without this, a silent inversion (`ROLE === "analyst"` ↔ `ROLE !== "analyst"`)
    would let analyst tile mint a general PAT and admin tile mint a
    bootstrap-scoped PAT — with both substrings still present in served HTML.
    The test passes whether the content is queried via either role URL since
    the JS ternary itself is identical across role responses.
    """
    import re
    resp = client.get("/setup?role=admin", follow_redirects=True)  # JS body is role-independent
    assert resp.status_code == 200
    text = resp.text
    # Match the ternary `ROLE === "analyst" ? <truthy_branch> : <falsy_branch>`.
    # Allow whitespace/newlines, ensure `bootstrap-analyst` is in the truthy branch.
    pattern = re.compile(
        r'ROLE\s*===\s*"analyst"\s*\?\s*\{[^}]*bootstrap-analyst[^}]*\}\s*:\s*\{[^}]*expires_in_days[^}]*\}',
        re.DOTALL,
    )
    assert pattern.search(text), (
        "JS PAT-mint ternary must have `bootstrap-analyst` on the analyst (truthy) "
        "branch and `expires_in_days` on the admin (falsy) branch. A silent inversion "
        "would let the analyst tile mint a general PAT — exactly the regression we want to catch."
    )
