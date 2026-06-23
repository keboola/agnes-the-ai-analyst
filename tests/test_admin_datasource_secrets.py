"""Tests for /api/admin/datasource-secrets — admin-gated, write-only, vault-backed.

Mirrors tests/test_admin_slack_secrets.py.
"""

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


def _admin(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _analyst(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['analyst_token']}"}


def test_set_requires_admin(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=_analyst(seeded_app),
        json={"value": "123-x.apps.googleusercontent.com"},
    )
    assert r.status_code == 403


def test_set_rejects_unknown_name(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/DATABASE_URL",
        headers=_admin(seeded_app),
        json={"value": "postgres://x"},
    )
    assert r.status_code == 400


def test_set_rejects_empty_value(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=_admin(seeded_app),
        json={"value": ""},
    )
    assert r.status_code == 400


def test_set_then_status_reports_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    r = client.put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=headers,
        json={"value": "123456789-abc.apps.googleusercontent.com"},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["AGNES_GWS_CLIENT_ID"]["source"] == "vault"
    assert by_name["AGNES_GWS_CLIENT_ID"]["has_value"] is True
    assert "value" not in by_name["AGNES_GWS_CLIENT_ID"]


def test_env_shadows_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_SECRET", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)
    client.put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_SECRET",
        headers=headers,
        json={"value": "vault-secret"},
    )
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "env-secret")

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["AGNES_GWS_CLIENT_SECRET"]["source"] == "env"


def test_delete_clears(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)
    client.put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=headers,
        json={"value": "123-x.apps.googleusercontent.com"},
    )
    r = client.delete("/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID", headers=headers)
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["AGNES_GWS_CLIENT_ID"]["source"] == "unset"
    assert by_name["AGNES_GWS_CLIENT_ID"]["has_value"] is False


def test_validate_gws_credentials(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_GWS_CLIENT_ID", "123456789-abc.apps.googleusercontent.com")
    monkeypatch.setenv("AGNES_GWS_CLIENT_SECRET", "GOCSPX-x")
    r = seeded_app["client"].post(
        "/api/admin/validate-gws-credentials",
        headers=_admin(seeded_app),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["configured"] is True
    assert data["issues"] == []
