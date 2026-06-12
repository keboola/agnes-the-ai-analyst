"""Tests for the SOURCE-MODE gate on the workspace-prompt + welcome-template
admin editors (#622).

The old implicit ``seed_owns()`` read-only lock is gone — these tests encode
its replacement: the explicit ``instance_templates.source_mode`` toggle.

  * Editor mode: the editor is writable EVEN when an IWT repo is registered
    (the production lock-out #622 fixes). GET returns source=local/editor.
  * Git mode: PUT/DELETE return 409 ``prompt_in_git_mode``; GET returns
    source=seed/git bound to the repo file.

Covers both legacy editors (``/api/admin/welcome-template`` and
``/api/admin/workspace-prompt-template``), since the JS may still call them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/auth/bootstrap",
        json={"email": "admin@example.com", "name": "A", "password": "TestPass123!"},
    )
    if resp.status_code == 403:
        pytest.skip("admin already bootstrapped")
    assert resp.status_code == 200, resp.text
    return client, resp.json()["access_token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _set_git_mode(kind: str):
    """Force a managed prompt into git source_mode via the repo factory."""
    from src.repositories import claude_md_template_repo, welcome_template_repo

    repo = claude_md_template_repo() if kind == "workspace" else welcome_template_repo()
    repo.set_source_mode("git", updated_by="admin@example.com")


# ---------------------------------------------------------------------------
# Editor mode: writable even with an IWT registered (the inversion of the old
# seed-owns 409). We don't even need a clone — editor mode never consults it.
# ---------------------------------------------------------------------------


def test_workspace_editor_put_allowed(app_client):
    client, token = app_client
    valid = "# Workspace prompt\nHello {{ user.email }}\n"
    resp = client.put(
        "/api/admin/workspace-prompt-template",
        headers=_hdr(token),
        json={"content": valid},
    )
    assert resp.status_code == 200, resp.text


def test_welcome_editor_put_allowed(app_client):
    client, token = app_client
    valid = "# Install prompt\nHello {% if user %}{{ user.email }}{% endif %}\n"
    resp = client.put(
        "/api/admin/welcome-template",
        headers=_hdr(token),
        json={"content": valid},
    )
    assert resp.status_code == 200, resp.text


def test_workspace_get_local_in_editor_mode(app_client):
    client, token = app_client
    resp = client.get("/api/admin/workspace-prompt-template", headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "local"
    assert body["source_mode"] == "editor"


# ---------------------------------------------------------------------------
# Git mode: PUT + DELETE refused with prompt_in_git_mode; GET surfaces seed/git.
# ---------------------------------------------------------------------------


def test_workspace_put_rejected_in_git_mode(app_client):
    client, token = app_client
    _set_git_mode("workspace")
    resp = client.put(
        "/api/admin/workspace-prompt-template",
        headers=_hdr(token),
        json={"content": "anything"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["kind"] == "prompt_in_git_mode"


def test_workspace_delete_rejected_in_git_mode(app_client):
    client, token = app_client
    _set_git_mode("workspace")
    resp = client.delete("/api/admin/workspace-prompt-template", headers=_hdr(token))
    assert resp.status_code == 409
    assert resp.json()["detail"]["kind"] == "prompt_in_git_mode"


def test_welcome_put_rejected_in_git_mode(app_client):
    client, token = app_client
    _set_git_mode("install")
    resp = client.put(
        "/api/admin/welcome-template",
        headers=_hdr(token),
        json={"content": "anything"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["kind"] == "prompt_in_git_mode"


def test_welcome_get_seed_in_git_mode(app_client):
    client, token = app_client
    _set_git_mode("install")
    resp = client.get("/api/admin/welcome-template", headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "seed"
    assert body["source_mode"] == "git"
