"""J5 — Jira webhook journey tests.

Tests the Jira webhook endpoint: valid HMAC signature accepted, invalid
signature rejected, missing signature handled, and basic health check.
"""

import hashlib
import hmac
import json
import pytest
from unittest.mock import patch, MagicMock


def _make_signature(payload: bytes, secret: str) -> str:
    """Generate a valid HMAC-SHA256 signature for a payload."""
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


SAMPLE_JIRA_EVENT = {
    "webhookEvent": "jira:issue_updated",
    "issue": {
        "key": "PROJ-123",
        "fields": {
            "summary": "Test issue",
            "status": {"name": "In Progress"},
        },
    },
}


@pytest.mark.journey
class TestJiraWebhookJourney:
    def test_webhook_health_check(self, seeded_app):
        """Jira webhook health endpoint is always accessible."""
        c = seeded_app["client"]
        resp = c.get("/webhooks/jira/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] == "ok"

    def test_webhook_with_no_secret_configured_accepted(self, seeded_app):
        """When JIRA_WEBHOOK_SECRET is not set, signature is skipped and webhook is processed."""
        c = seeded_app["client"]
        payload = json.dumps(SAMPLE_JIRA_EVENT).encode()

        with patch("connectors.jira.service._JiraConfig.JIRA_WEBHOOK_SECRET", ""), \
             patch("app.api.jira_webhooks.Config") as mock_cfg:
            mock_cfg.JIRA_WEBHOOK_SECRET = ""
            mock_cfg.JIRA_DATA_DIR = MagicMock()
            mock_cfg.JIRA_DATA_DIR.__truediv__ = lambda self, other: MagicMock(
                __truediv__=lambda s, o: MagicMock(mkdir=MagicMock(), __truediv__=lambda s2, o2: MagicMock())
            )

            mock_service = MagicMock()
            mock_service.is_configured.return_value = True
            mock_service.process_webhook_event.return_value = True

            with patch("app.api.jira_webhooks.get_jira_service", return_value=mock_service), \
                 patch("app.api.jira_webhooks._verify_signature", return_value=True), \
                 patch("app.api.jira_webhooks._log_webhook_event"):
                resp = c.post(
                    "/webhooks/jira",
                    content=payload,
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_webhook_with_valid_hmac_signature(self, seeded_app):
        """POST with valid HMAC-SHA256 signature is accepted."""
        c = seeded_app["client"]
        secret = "test-jira-secret-xyz"
        payload = json.dumps(SAMPLE_JIRA_EVENT).encode()
        signature = _make_signature(payload, secret)

        mock_service = MagicMock()
        mock_service.is_configured.return_value = True
        mock_service.process_webhook_event.return_value = True

        with patch("app.api.jira_webhooks.Config") as mock_cfg, \
             patch("app.api.jira_webhooks.get_jira_service", return_value=mock_service), \
             patch("app.api.jira_webhooks._log_webhook_event"):
            mock_cfg.JIRA_WEBHOOK_SECRET = secret
            mock_cfg.JIRA_DATA_DIR = MagicMock()

            resp = c.post(
                "/webhooks/jira",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["event"] == "jira:issue_updated"

    def test_webhook_with_invalid_signature_rejected(self, seeded_app):
        """POST with wrong signature returns 401."""
        c = seeded_app["client"]
        secret = "real-secret"
        payload = json.dumps(SAMPLE_JIRA_EVENT).encode()
        bad_signature = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        with patch("app.api.jira_webhooks.Config") as mock_cfg:
            mock_cfg.JIRA_WEBHOOK_SECRET = secret
            mock_cfg.JIRA_DATA_DIR = MagicMock()

            resp = c.post(
                "/webhooks/jira",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": bad_signature,
                },
            )
        assert resp.status_code == 401
        assert "Invalid signature" in resp.json()["detail"]

    def test_webhook_empty_payload_rejected(self, seeded_app):
        """Empty body returns 400."""
        c = seeded_app["client"]

        with patch("app.api.jira_webhooks.Config") as mock_cfg, \
             patch("app.api.jira_webhooks._verify_signature", return_value=True):
            mock_cfg.JIRA_WEBHOOK_SECRET = ""

            resp = c.post(
                "/webhooks/jira",
                content=b"",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400
