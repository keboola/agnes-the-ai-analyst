"""Tests for app/api/flea_market.py endpoints."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.flea_market import router
from app.auth.dependencies import get_current_user


def _stub_user():
    return {"id": "test-user", "email": "test@example.com"}


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[get_current_user] = _stub_user
    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.marketplace_slug = "flea-market"
    cfg.plugin_name = "flea-market"
    cfg.github_repo = "org/repo"
    cfg.github_app_id = "1"
    cfg.github_app_private_key = "pem"
    cfg.github_app_installation_id = "2"
    return cfg


def test_get_skills_returns_empty_list(client, mock_config):
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.list_skills", return_value=[]),
    ):
        resp = client.get("/api/flea-market/skills")
    assert resp.status_code == 200
    assert resp.json() == {"skills": []}


def test_submit_skill_success(client, mock_config):
    review = MagicMock(is_duplicate=False, duplicate_of=None, duplicate_reason=None,
                        requires_setup=False, setup_description=None)
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value="md"),
        patch("app.api.flea_market.write_pending_marker"),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
        patch("app.api.flea_market._do_github_push"),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "my-skill",
            "description": "Does something useful",
            "body": "# Title\nThis is the skill body with enough content here.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert data["skill_name"] == "my-skill"


def test_submit_skill_name_conflict_returns_409(client, mock_config):
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=True),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "existing-skill",
            "description": "Already there",
            "body": "# Already exists with enough content here.",
        })
    assert resp.status_code == 409


def test_submit_skill_duplicate_returns_warning(client, mock_config):
    review = MagicMock(is_duplicate=True, duplicate_of="other-skill",
                        duplicate_reason="Same purpose", requires_setup=False, setup_description=None)
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[{"name": "other-skill", "description": "Same"}]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value="md"),
        patch("app.api.flea_market.write_pending_marker"),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
        patch("app.api.flea_market._do_github_push"),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "new-skill",
            "description": "Does something useful",
            "body": "# Title\nThis is the skill body with enough content here.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert data["warning"] is not None
    assert "other-skill" in data["warning"]


def test_submit_skill_with_setup_warning(client, mock_config):
    review = MagicMock(is_duplicate=False, duplicate_of=None, duplicate_reason=None,
                        requires_setup=True, setup_description="Needs MCP server")
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value="md"),
        patch("app.api.flea_market.write_pending_marker"),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
        patch("app.api.flea_market._do_github_push"),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "mcp-skill",
            "description": "Uses an MCP server for data access",
            "body": "# Title\nThis skill requires an MCP server to be running.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert data["warning"] is not None


def test_submit_writes_pending_marker_and_queues_push(client, mock_config):
    review = MagicMock(is_duplicate=False, duplicate_of=None, duplicate_reason=None,
                        requires_setup=False, setup_description=None)
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value="md"),
        patch("app.api.flea_market.write_pending_marker") as mock_marker,
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
        patch("app.api.flea_market._do_github_push") as mock_push,
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "my-skill",
            "description": "Does something useful",
            "body": "# Title\nThis is the skill body with enough content here.",
        })
    assert resp.status_code == 200
    mock_marker.assert_called_once_with(mock_config, "my-skill")
    mock_push.assert_called_once_with(mock_config, "my-skill")
