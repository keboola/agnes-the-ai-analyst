"""X-StorageApi-Token header auth: mapping, precedence, classification,
cache, flood guard — and the require_session_token laundering block.

No ``/auth/me`` (or other bare whoami) route exists on this instance — see
``app/auth/router.py`` / the ``/api/me*`` routers, none of which expose a
plain "who am I" JSON payload reachable with nothing but ``get_current_user``.
Per the task brief's own fallback, endpoint-level assertions below exercise
``GET /api/catalog/tables`` (an ordinary ``Depends(get_current_user)`` route,
see ``app/api/catalog.py``) and assert on status codes only; the
mapping-to-identity assertion (``credential_surface`` / ``token_type``) is
covered at the unit level via ``resolve_header_user`` directly in
``TestResolveUnit``.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204",
        project_id="5947",
        project_name="Acme DWH",
        email=email,
        name="Jane",
        role="admin",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "1")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    from app.auth import keboola_header

    keboola_header.reset_state_for_tests()
    from app.main import create_app
    from src.repositories import users_repo

    app = create_app()
    c = TestClient(app)
    uid = str(uuid.uuid4())
    users_repo().create(id=uid, email="jane@example.com", name="Jane")
    return c


class TestHeaderAuth:
    def test_maps_to_existing_user(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-1"})
        assert resp.status_code == 200

    def test_unknown_user_gets_onboarding_hint(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity("nobody@example.com"))
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-2"})
        assert resp.status_code == 401
        assert "sign in" in resp.json()["detail"].lower()

    def test_switch_off_ignores_header(self, client, monkeypatch):
        monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "0")
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-3"})
        assert resp.status_code == 401

    def test_bearer_takes_precedence(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: called.append(tok) or _identity())
        resp = client.get(
            "/api/catalog/tables",
            headers={"Authorization": "Bearer not-a-jwt", "X-StorageApi-Token": "tok-4"},
        )
        # The bogus bearer fails auth; the storage header must NOT rescue it.
        assert resp.status_code == 401
        assert called == []

    def test_verify_cache_hits_within_ttl(self, client, monkeypatch):
        calls = []

        def counting(tok):
            calls.append(tok)
            return _identity()

        monkeypatch.setattr(kv, "verify_storage_token", counting)
        for _ in range(3):
            assert client.get("/api/catalog/tables", headers={"X-StorageApi-Token": "tok-5"}).status_code == 200
        assert len(calls) == 1

    def test_cannot_mint_pat(self, client, monkeypatch):
        # The laundering block: a Storage token must never create a persistent PAT.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.post(
            "/auth/tokens",
            json={"name": "laundered"},
            headers={"X-StorageApi-Token": "tok-6"},
        )
        assert resp.status_code == 403

    def test_flood_guard_trips_on_distinct_invalid_tokens(self, client, monkeypatch):
        def failing(tok):
            raise kv.KeboolaVerifyError("invalid_token", "no")

        monkeypatch.setattr(kv, "verify_storage_token", failing)
        last = None
        for i in range(30):
            last = client.get("/api/catalog/tables", headers={"X-StorageApi-Token": f"junk-{i}"})
        assert last.status_code == 429


class TestResolveUnit:
    def test_credential_surface_is_stack(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        from app.auth.keboola_header import resolve_header_user

        user, reason = resolve_header_user("tok-7", None)
        assert reason == ""
        assert user["credential_surface"] == "stack"
        assert user["token_type"] == "keboola_token"
