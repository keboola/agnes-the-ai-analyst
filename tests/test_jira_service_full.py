"""Full tests for the Jira service (JiraService.process_webhook_event and friends)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers.factories import WebhookEventFactory


@pytest.fixture
def jira_env(tmp_path, monkeypatch):
    """Set up a Jira environment with required dirs and env vars."""
    data_dir = tmp_path / "jira_data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()

    monkeypatch.setenv("JIRA_DOMAIN", "mycompany.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@mycompany.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token-xyz")
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "webhook-secret-123")

    return data_dir


def _make_jira_service(jira_env):
    """Create a fresh JiraService with test configuration."""
    from connectors.jira import service as svc
    svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
    svc.Config.JIRA_EMAIL = "bot@mycompany.com"
    svc.Config.JIRA_API_TOKEN = "test-token-xyz"
    svc.Config.JIRA_DATA_DIR = jira_env
    svc.Config.JIRA_WEBHOOK_SECRET = "webhook-secret-123"
    svc._jira_service = None
    return svc.JiraService()


def _fake_issue_data(issue_key: str = "TEST-1") -> dict:
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "summary": "Test issue summary",
            "status": {"name": "Open"},
            "issuetype": {"name": "Bug"},
            "attachment": [],
            "comment": {"comments": []},
        },
    }


class TestJiraServiceWebhookProcessing:
    def test_process_issue_updated_calls_fetch_and_save(self, jira_env):
        """process_webhook_event for issue_updated fetches fresh data from API."""
        service = _make_jira_service(jira_env)
        event_data, _, _ = WebhookEventFactory.issue_updated("PROJ-100")
        issue_data = _fake_issue_data("PROJ-100")

        with patch.object(service, "fetch_issue", return_value=issue_data), \
             patch.object(service, "fetch_remote_links", return_value=[]), \
             patch.object(service, "fetch_sla_fields", return_value=None), \
             patch.object(service, "download_all_attachments", return_value=[]), \
             patch("connectors.jira.service.trigger_incremental_transform", return_value=True):
            result = service.process_webhook_event(event_data)

        assert result is True
        saved_file = jira_env / "issues" / "PROJ-100.json"
        assert saved_file.exists()
        with open(saved_file) as f:
            saved = json.load(f)
        assert saved["key"] == "PROJ-100"

    def test_process_issue_deleted_marks_file(self, jira_env):
        """process_webhook_event for issue_deleted marks existing JSON with _deleted_at."""
        service = _make_jira_service(jira_env)

        # Pre-create the issue JSON
        issue_file = jira_env / "issues" / "PROJ-200.json"
        issue_file.write_text(json.dumps({"key": "PROJ-200", "fields": {}}))

        event_data, _, _ = WebhookEventFactory.issue_deleted("PROJ-200")

        with patch("connectors.jira.service.trigger_incremental_transform", return_value=True):
            result = service.process_webhook_event(event_data)

        assert result is True
        with open(issue_file) as f:
            saved = json.load(f)
        assert "_deleted_at" in saved

    def test_process_missing_issue_key_returns_false(self, jira_env):
        """Webhook event without issue key returns False."""
        service = _make_jira_service(jira_env)
        result = service.process_webhook_event({"webhookEvent": "jira:issue_updated"})
        assert result is False

    def test_process_uses_embedded_data_when_fetch_fails(self, jira_env):
        """Falls back to embedded issue data in webhook payload when API fetch fails."""
        service = _make_jira_service(jira_env)
        event_data = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "PROJ-300",
                "id": "10003",
                "fields": {
                    "summary": "Embedded issue",
                    "attachment": [],
                    "comment": {"comments": []},
                },
            },
        }

        with patch.object(service, "fetch_issue", return_value=None), \
             patch.object(service, "fetch_remote_links", return_value=[]), \
             patch.object(service, "fetch_sla_fields", return_value=None), \
             patch.object(service, "download_all_attachments", return_value=[]), \
             patch("connectors.jira.service.trigger_incremental_transform", return_value=True):
            result = service.process_webhook_event(event_data)

        assert result is True
        saved_file = jira_env / "issues" / "PROJ-300.json"
        assert saved_file.exists()

    def test_deletion_of_nonexistent_issue_returns_true(self, jira_env):
        """Deleting an issue that has no local file returns True (idempotent)."""
        service = _make_jira_service(jira_env)
        event_data, _, _ = WebhookEventFactory.issue_deleted("PROJ-99999")

        result = service.process_webhook_event(event_data)
        assert result is True

    def test_fetch_issue_returns_none_on_404(self, jira_env):
        """fetch_issue returns None when Jira returns 404."""
        import httpx

        service = _make_jira_service(jira_env)

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=mock_client):
            result = service.fetch_issue("PROJ-MISSING")

        assert result is None

    def test_fetch_issue_returns_data_on_200(self, jira_env):
        """fetch_issue returns parsed JSON on HTTP 200."""
        service = _make_jira_service(jira_env)
        issue_data = _fake_issue_data("PROJ-42")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = issue_data
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=mock_client):
            result = service.fetch_issue("PROJ-42")

        assert result is not None
        assert result["key"] == "PROJ-42"

    def test_webhook_event_factory_signature_verification(self):
        """WebhookEventFactory produces correct HMAC-SHA256 signatures."""
        import hashlib
        import hmac

        secret = "test-secret"
        event_data, payload, sig = WebhookEventFactory.issue_updated("TEST-1", secret)

        expected_mac = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        assert sig == f"sha256={expected_mac}"
