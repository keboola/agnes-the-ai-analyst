"""Tests for the seed-ownership gate on the workspace-prompt + welcome-
template admin editors.

Covers (A1.3 of the connector-skills refactor):
  * GET returns seed file content + `source: "seed"` when seed owns
  * PUT returns 409 `iwt_seed_owns_template` when seed owns
  * DELETE returns 409 when seed owns
  * All endpoints behave normally when seed does NOT own the file (local
    DB override path stays alive)
  * Per-file detection: an instance can have seed own one editor and not
    the other (asymmetric)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


def _stub_seed_owns(*paths: str):
    """Make `seed_owns(p)` return True only for paths in `paths`."""
    paths_set = set(paths)

    def _fake(p: str) -> bool:
        return p in paths_set

    return _fake


def _stub_resolve_seed_file(file_map: dict[str, str]):
    """Make `resolve_seed_file(p)` return (content, "iwt") for any path in
    the map; None otherwise."""

    def _fake(p: str):
        if p in file_map:
            return (file_map[p], "iwt")
        return None

    return _fake


# ---------------------------------------------------------------------------
# /api/admin/welcome-template (install-prompt template gate)
# ---------------------------------------------------------------------------


def test_welcome_get_returns_seed_content_when_seed_owns(app_client):
    client, token = app_client
    seed_content = "# I am the seed install prompt template\n"
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("install-prompt/template.md.tmpl"),
    ), patch(
        "src.initial_workspace.resolve_seed_file",
        _stub_resolve_seed_file({"install-prompt/template.md.tmpl": seed_content}),
    ):
        resp = client.get(
            "/api/admin/welcome-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "seed"
    assert body["seed_path"] == "install-prompt/template.md.tmpl"
    assert body["content"] == seed_content
    assert body["updated_at"] is None


def test_welcome_get_normal_when_seed_does_not_own(app_client):
    client, token = app_client
    with patch("src.initial_workspace.seed_owns", _stub_seed_owns()):
        resp = client.get(
            "/api/admin/welcome-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "local"
    assert body.get("seed_path") is None


def test_welcome_put_rejected_when_seed_owns(app_client):
    client, token = app_client
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("install-prompt/template.md.tmpl"),
    ):
        resp = client.put(
            "/api/admin/welcome-template",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": "anything"},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["kind"] == "iwt_seed_owns_template"
    assert detail["seed_path"] == "install-prompt/template.md.tmpl"
    assert "Sync now" in detail["hint"]


def test_welcome_delete_rejected_when_seed_owns(app_client):
    client, token = app_client
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("install-prompt/template.md.tmpl"),
    ):
        resp = client.delete(
            "/api/admin/welcome-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["kind"] == "iwt_seed_owns_template"


# ---------------------------------------------------------------------------
# /api/admin/workspace-prompt-template (analyst CLAUDE.md gate)
# ---------------------------------------------------------------------------


def test_workspace_get_returns_seed_content_when_seed_owns(app_client):
    client, token = app_client
    seed_content = "# I am the seed CLAUDE.md\n"
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("workspace/CLAUDE.md"),
    ), patch(
        "src.initial_workspace.resolve_seed_file",
        _stub_resolve_seed_file({"workspace/CLAUDE.md": seed_content}),
    ):
        resp = client.get(
            "/api/admin/workspace-prompt-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "seed"
    assert body["seed_path"] == "workspace/CLAUDE.md"
    assert body["content"] == seed_content


def test_workspace_put_rejected_when_seed_owns(app_client):
    client, token = app_client
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("workspace/CLAUDE.md"),
    ):
        resp = client.put(
            "/api/admin/workspace-prompt-template",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": "anything"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["kind"] == "iwt_seed_owns_template"


def test_workspace_delete_rejected_when_seed_owns(app_client):
    client, token = app_client
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("workspace/CLAUDE.md"),
    ):
        resp = client.delete(
            "/api/admin/workspace-prompt-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Asymmetric ownership — operator can have seed own one and not the other
# ---------------------------------------------------------------------------


def test_per_file_detection_is_independent(app_client):
    """Seed owns workspace/CLAUDE.md but NOT install-prompt/template.md.tmpl.
    workspace-prompt editor disables; agent-prompt editor stays editable.
    """
    client, token = app_client
    with patch(
        "src.initial_workspace.seed_owns",
        _stub_seed_owns("workspace/CLAUDE.md"),
    ), patch(
        "src.initial_workspace.resolve_seed_file",
        _stub_resolve_seed_file({"workspace/CLAUDE.md": "from seed\n"}),
    ):
        ws_resp = client.get(
            "/api/admin/workspace-prompt-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        ap_resp = client.get(
            "/api/admin/welcome-template",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert ws_resp.status_code == 200
    assert ws_resp.json()["source"] == "seed"
    assert ap_resp.status_code == 200
    assert ap_resp.json()["source"] == "local"
