"""Tests for Jira webhook FastAPI router."""

import hashlib
import hmac
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


def _sign(payload: bytes, secret: str) -> str:
    """Compute sha256=<HMAC hex> for a given payload and secret."""
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


@pytest.fixture()
def webhook_client(tmp_path, monkeypatch):
    """Create a TestClient with required env vars and dirs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))

    # Re-read env into Config (class attrs read os.environ at import time)
    from connectors.jira import service as svc
    monkeypatch.setattr(svc.Config, "JIRA_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(svc.Config, "JIRA_DATA_DIR", data_dir)

    # Reset singleton so it picks up fresh Config values
    svc._jira_service = None

    # Reimport app to pick up router
    from app.main import create_app
    app = create_app()
    return TestClient(app)


def test_health(webhook_client):
    """GET /webhooks/jira/health returns 200."""
    resp = webhook_client.get("/webhooks/jira/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "webhook_secret_set" in body


def test_missing_signature_401(webhook_client):
    """POST without signature header returns 401."""
    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = webhook_client.post("/webhooks/jira", content=payload, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


def test_invalid_signature_401(webhook_client):
    """POST with wrong signature returns 401."""
    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = webhook_client.post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=badhex",
        },
    )
    assert resp.status_code == 401


def test_valid_signature_accepted(webhook_client):
    """POST with correct HMAC-SHA256 passes signature check (not 401)."""
    from unittest.mock import patch

    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    sig = _sign(payload, "test-webhook-secret")

    # Mock process_webhook_event so the test only checks HMAC validation,
    # not the full Jira API flow (which requires a real Jira connection).
    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client.post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200


def test_empty_payload_400(webhook_client):
    """POST with empty body and valid signature returns 400."""
    payload = b""
    sig = _sign(payload, "test-webhook-secret")
    resp = webhook_client.post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400


def test_unconfigured_secret_returns_503(tmp_path, monkeypatch):
    """Issue #83: missing JIRA_WEBHOOK_SECRET must fail-closed (no fall-through to 200)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))

    from connectors.jira import service as svc
    monkeypatch.setattr(svc.Config, "JIRA_WEBHOOK_SECRET", "")
    monkeypatch.setattr(svc.Config, "JIRA_DATA_DIR", data_dir)
    svc._jira_service = None

    from app.main import create_app
    client = TestClient(create_app())

    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = client.post(
        "/webhooks/jira",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "secret" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../etc/passwd",
        "../foo",
        "TEST-1/../../../bar",
        "TEST-1\x00.json",
        "test-1",          # lowercase project — Jira keys are uppercase
        "TEST",            # missing -<num>
        "TEST-",           # missing num
        "-1",              # missing project
        "",                # empty
        "A" * 100 + "-1",  # absurd length
    ],
)
def test_path_traversal_in_issue_key_rejected(webhook_client, bad_key):
    """Issue #83: malformed issue keys must be rejected with 400, not used in paths."""
    payload = json.dumps({
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": bad_key},
    }).encode()
    sig = _sign(payload, "test-webhook-secret")

    resp = webhook_client.post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400, f"key {bad_key!r} should have been rejected, got {resp.status_code}"


def test_valid_issue_key_accepted(webhook_client):
    """Sanity: a well-formed issue key still passes validation."""
    from unittest.mock import patch

    payload = json.dumps({
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "PROJ-42"},
    }).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client.post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200
