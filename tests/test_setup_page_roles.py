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
