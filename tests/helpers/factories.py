"""Test factories for generating test data."""

import hashlib
import hmac
import json


class WebhookEventFactory:
    """Factory for creating Jira webhook event payloads."""

    @staticmethod
    def issue_updated(issue_key: str = "PROJ-123", webhook_secret: str = "") -> tuple[dict, bytes, str]:
        """Create an issue_updated webhook event.

        Returns:
            (event_data, payload_bytes, signature) tuple.
        """
        event_data = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": issue_key,
                "id": "10001",
                "fields": {
                    "summary": "Test issue",
                    "status": {"name": "Open"},
                    "issuetype": {"name": "Bug"},
                },
            },
            "user": {"displayName": "Test User"},
        }
        payload = json.dumps(event_data).encode()
        sig = WebhookEventFactory._sign(payload, webhook_secret) if webhook_secret else ""
        return event_data, payload, sig

    @staticmethod
    def issue_deleted(issue_key: str = "PROJ-456", webhook_secret: str = "") -> tuple[dict, bytes, str]:
        """Create an issue_deleted webhook event."""
        event_data = {
            "webhookEvent": "jira:issue_deleted",
            "issue": {
                "key": issue_key,
                "id": "10002",
                "fields": {"summary": "Deleted issue"},
            },
        }
        payload = json.dumps(event_data).encode()
        sig = WebhookEventFactory._sign(payload, webhook_secret) if webhook_secret else ""
        return event_data, payload, sig

    @staticmethod
    def issue_created(issue_key: str = "PROJ-789", webhook_secret: str = "") -> tuple[dict, bytes, str]:
        """Create an issue_created webhook event."""
        event_data = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": issue_key,
                "id": "10003",
                "fields": {
                    "summary": "New issue",
                    "status": {"name": "Open"},
                    "issuetype": {"name": "Story"},
                },
            },
        }
        payload = json.dumps(event_data).encode()
        sig = WebhookEventFactory._sign(payload, webhook_secret) if webhook_secret else ""
        return event_data, payload, sig

    @staticmethod
    def _sign(payload: bytes, secret: str) -> str:
        """Compute sha256=<HMAC hex> signature."""
        mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return f"sha256={mac}"
