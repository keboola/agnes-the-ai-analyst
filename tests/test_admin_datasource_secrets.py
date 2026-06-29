"""Tests for /api/admin/datasource-secrets — admin-gated, write-only, vault-backed.

Mirrors tests/test_admin_slack_secrets.py.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests

_VALID_SA_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "my-project",
        "private_key_id": "key123",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "sa@my-project.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


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
        "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN",
        headers=_analyst(seeded_app),
        json={"value": "some-token"},
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
        "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN",
        headers=_admin(seeded_app),
        json={"value": ""},
    )
    assert r.status_code == 400


def test_set_then_status_reports_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    r = client.put(
        "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN",
        headers=headers,
        json={"value": "my-keboola-token"},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["KEBOOLA_STORAGE_TOKEN"]["source"] == "vault"
    assert by_name["KEBOOLA_STORAGE_TOKEN"]["has_value"] is True
    assert "value" not in by_name["KEBOOLA_STORAGE_TOKEN"]


def test_env_shadows_vault(seeded_app, monkeypatch):
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)
    client.put(
        "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN",
        headers=headers,
        json={"value": "vault-token"},
    )
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "env-token")

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["KEBOOLA_STORAGE_TOKEN"]["source"] == "env"


def test_delete_clears(seeded_app, monkeypatch):
    monkeypatch.delenv("KEBOOLA_STORAGE_TOKEN", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)
    client.put(
        "/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN",
        headers=headers,
        json={"value": "my-keboola-token"},
    )
    r = client.delete("/api/admin/datasource-secrets/KEBOOLA_STORAGE_TOKEN", headers=headers)
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["KEBOOLA_STORAGE_TOKEN"]["source"] == "unset"
    assert by_name["KEBOOLA_STORAGE_TOKEN"]["has_value"] is False


def test_bigquery_json_validation_rejects_invalid(seeded_app):
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    # Not JSON at all
    r = client.put(
        "/api/admin/datasource-secrets/BIGQUERY_SERVICE_ACCOUNT_JSON",
        headers=headers,
        json={"value": "not-json"},
    )
    assert r.status_code == 400
    assert "invalid_service_account_json" in r.json().get("detail", "")


def test_bigquery_json_validation_rejects_missing_fields(seeded_app):
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    # Valid JSON but wrong type / missing required fields
    bad = json.dumps({"type": "authorized_user", "client_id": "x"})
    r = client.put(
        "/api/admin/datasource-secrets/BIGQUERY_SERVICE_ACCOUNT_JSON",
        headers=headers,
        json={"value": bad},
    )
    assert r.status_code == 400
    assert "invalid_service_account_json" in r.json().get("detail", "")


def test_bigquery_json_validation_accepts_valid(seeded_app, monkeypatch):
    monkeypatch.delenv("BIGQUERY_SERVICE_ACCOUNT_JSON", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    r = client.put(
        "/api/admin/datasource-secrets/BIGQUERY_SERVICE_ACCOUNT_JSON",
        headers=headers,
        json={"value": _VALID_SA_JSON},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["BIGQUERY_SERVICE_ACCOUNT_JSON"]["source"] == "vault"


def test_gws_client_id_rejects_invalid(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=_admin(seeded_app),
        json={"value": "not-valid-client-id"},
    )
    assert r.status_code == 400
    assert r.json().get("detail") == "invalid_gws_client_id"


def test_gws_client_secret_rejects_empty(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_SECRET",
        headers=_admin(seeded_app),
        json={"value": "   "},
    )
    assert r.status_code == 400
    assert r.json().get("detail") == "secret value required"


def test_gws_client_secret_accepts_non_gocspx(seeded_app):
    r = seeded_app["client"].put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_SECRET",
        headers=_admin(seeded_app),
        json={"value": "legacy-client-secret-value"},
    )
    assert r.status_code == 204


def test_gws_client_id_accepts_valid(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_GWS_CLIENT_ID", raising=False)
    client = seeded_app["client"]
    headers = _admin(seeded_app)

    r = client.put(
        "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_ID",
        headers=headers,
        json={"value": "123456789012-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com"},
    )
    assert r.status_code == 204

    r = client.get("/api/admin/datasource-secrets", headers=headers)
    by_name = {s["name"]: s for s in r.json()["secrets"]}
    assert by_name["AGNES_GWS_CLIENT_ID"]["source"] == "vault"


def test_validate_gws_requires_admin(seeded_app):
    r = seeded_app["client"].post(
        "/api/admin/validate-gws-credentials",
        headers=_analyst(seeded_app),
        json={"client_id": "123-abc.apps.googleusercontent.com"},
    )
    assert r.status_code == 403


def test_validate_gws_accepts_valid_format(seeded_app):
    r = seeded_app["client"].post(
        "/api/admin/validate-gws-credentials",
        headers=_admin(seeded_app),
        json={"client_id": "123456789012-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"valid": True}


def test_validate_gws_rejects_invalid_format(seeded_app):
    r = seeded_app["client"].post(
        "/api/admin/validate-gws-credentials",
        headers=_admin(seeded_app),
        json={"client_id": "not-a-client-id"},
    )
    assert r.status_code == 200
    assert r.json() == {"valid": False}
