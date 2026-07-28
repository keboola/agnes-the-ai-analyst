"""Tests for /api/admin/slack-secrets — admin-gated, write-only, vault-backed."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def test_set_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"value": "xoxb-x"},
    )
    assert r.status_code == 403


def test_set_rejects_unknown_name(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/DATABASE_URL",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": "x"},
    )
    assert r.status_code == 400


def test_set_rejects_empty(seeded_app):
    client = seeded_app["client"]
    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"value": ""},
    )
    assert r.status_code == 400


def test_set_then_status_reports_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.put(
        "/api/admin/slack-secrets/SLACK_BOT_TOKEN", headers=admin,
        json={"value": "xoxb-secret"},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/slack-secrets", headers=admin)
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_BOT_TOKEN"]["source"] == "vault"
    assert by_name["SLACK_BOT_TOKEN"]["has_value"] is True
    assert "value" not in by_name["SLACK_BOT_TOKEN"]


def test_env_shadows_vault_in_status(seeded_app, monkeypatch):
    # Populate the vault first, THEN set env — proves env wins over a
    # populated vault (the actual env > vault precedence), not just
    # "env-set reports env".
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    client.put(
        "/api/admin/slack-secrets/SLACK_APP_TOKEN", headers=admin,
        json={"value": "xapp-vault"},
    )
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-env")
    r = client.get("/api/admin/slack-secrets", headers=admin)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_APP_TOKEN"]["source"] == "env"


def test_delete_clears(seeded_app, monkeypatch):
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    client = seeded_app["client"]
    admin = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    client.put(
        "/api/admin/slack-secrets/SLACK_SIGNING_SECRET", headers=admin,
        json={"value": "shh"},
    )
    r = client.delete("/api/admin/slack-secrets/SLACK_SIGNING_SECRET", headers=admin)
    assert r.status_code == 204
    r = client.get("/api/admin/slack-secrets", headers=admin)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["SLACK_SIGNING_SECRET"]["source"] == "unset"
    assert by_name["SLACK_SIGNING_SECRET"]["has_value"] is False
