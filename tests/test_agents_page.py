"""The `/agents` minimal builder page — Task 10.

Server-rendered list of the caller's own agents (name, slug, four scope
modes, model, budget, `is_default` badge) plus a create form and per-agent
action buttons whose JS drives the management API (`/api/v1/agents`,
covered by `tests/test_agents_management_api.py`). This file only exercises
the page route itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def agents_page_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    client = TestClient(create_app())
    return {
        "client": client,
        "owner_id": "owner1",
        "owner_token": create_access_token("owner1", "owner@test.com"),
    }


def _get(env):
    return env["client"].get(
        "/agents",
        headers={"Authorization": f"Bearer {env['owner_token']}"},
    )


def test_agents_page_lists_seeded_agents(agents_page_env):
    """200, chrome-wired (stylesheet present), lists the default agent
    (with its badge) and a second owner-created agent by name+slug."""
    from src.repositories import agents_repo

    agents_repo().get_or_create_default(agents_page_env["owner_id"])
    agents_repo().create(
        id="agent-sales-1",
        owner_user_id=agents_page_env["owner_id"],
        name="Sales Analyst",
        slug="sales-analyst",
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )

    r = _get(agents_page_env)
    assert r.status_code == 200, r.text
    html = r.text
    assert "/static/" in html  # _chrome_ctx wired -> stylesheet href non-empty
    assert "Sales Analyst" in html
    assert "sales-analyst" in html
    assert "Default" in html  # is_default badge marker


def test_agents_page_extends_page_shell(agents_page_env):
    """Renders through the base_page.html -> base_ds.html chain, not a
    bespoke standalone document (nav + hero present)."""
    from src.repositories import agents_repo

    agents_repo().get_or_create_default(agents_page_env["owner_id"])
    html = _get(agents_page_env).text
    assert 'class="app-header"' in html  # base_ds.html production nav
    assert "page-header--hero" in html  # base_page.html hero block


def test_agents_page_anonymous_rejected(agents_page_env):
    r = agents_page_env["client"].get("/agents", follow_redirects=False)
    assert r.status_code in (401, 302), r.text


def test_agents_page_issue_token_disabled_for_all_mode(agents_page_env):
    """The seeded default agent is all-mode -> its Issue-token control must
    render disabled (with a tooltip), never a clickable POST trigger."""
    from src.repositories import agents_repo

    agents_repo().get_or_create_default(agents_page_env["owner_id"])
    html = _get(agents_page_env).text
    assert "data-issue-token" in html
    assert "disabled" in html
