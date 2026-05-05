"""Tests for src/github_app.py GitHub App helpers."""
import base64
from unittest.mock import MagicMock, patch

import pytest

from src.github_app import GitHubAppConfig, _get_file_sha, push_file, push_skill


@pytest.fixture
def config():
    return GitHubAppConfig(
        app_id="123",
        private_key_pem="fake-pem",
        installation_id="456",
        repo="org/repo",
    )


def test_get_file_sha_returns_none_on_404(config):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("src.github_app.requests.get", return_value=mock_resp):
        sha = _get_file_sha("tok", "org/repo", "plugins/x/SKILL.md")
    assert sha is None


def test_get_file_sha_returns_sha_on_200(config):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"sha": "abc123"}
    with patch("src.github_app.requests.get", return_value=mock_resp):
        sha = _get_file_sha("tok", "org/repo", "plugins/x/SKILL.md")
    assert sha == "abc123"


def test_push_file_creates_new_file():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("src.github_app._get_file_sha", return_value=None),
        patch("src.github_app.requests.put", return_value=mock_resp) as mock_put,
    ):
        push_file("tok", "org/repo", "plugins/x/SKILL.md", "content", "add skill")

    call_body = mock_put.call_args[1]["json"]
    assert call_body["message"] == "add skill"
    assert call_body["content"] == base64.b64encode(b"content").decode()
    assert "sha" not in call_body


def test_push_file_updates_existing_file():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("src.github_app._get_file_sha", return_value="existing-sha"),
        patch("src.github_app.requests.put", return_value=mock_resp) as mock_put,
    ):
        push_file("tok", "org/repo", "plugins/x/SKILL.md", "content", "update skill")

    call_body = mock_put.call_args[1]["json"]
    assert call_body["sha"] == "existing-sha"


def test_push_skill_pushes_only_skill_md(config):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("src.github_app._get_installation_token", return_value="tok"),
        patch("src.github_app._get_file_sha", return_value=None),
        patch("src.github_app.requests.put", return_value=mock_resp) as mock_put,
    ):
        push_skill(config, "flea-market", "my-skill", "# skill content")

    assert mock_put.call_count == 1
    url = mock_put.call_args[0][0]
    assert "plugins/flea-market/skills/my-skill/SKILL.md" in url
