"""Keboola OAuth web login: availability, redirect, callback outcomes."""

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    monkeypatch.setattr(kv, "client_id", lambda: "cid")
    monkeypatch.setattr(kv, "client_secret", lambda: "csecret")
    from app.main import create_app

    return TestClient(create_app())


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204",
        project_id="5947",
        project_name="Acme DWH",
        email=email,
        name="Jane",
        role="admin",
    )


class TestLoginRoute:
    def test_redirects_to_oauth_host(self, client):
        resp = client.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"].startswith("https://connection.example.com/oauth/authorize")

    def test_unconfigured_redirects_to_login_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setattr(kv, "client_id", lambda: "")
        from app.main import create_app

        c = TestClient(create_app())
        resp = c.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code == 307 or resp.status_code == 302
        assert "error=keboola_not_configured" in resp.headers["location"]


class TestCallback:
    def _patch_flow(self, monkeypatch, identity=None, verify_error=None):
        from app.auth.providers import keboola as kb

        async def fake_authorize_access_token(request):
            return {"access_token": "at-123"}

        class FakeApp:
            authorize_access_token = staticmethod(fake_authorize_access_token)

        monkeypatch.setattr(kb, "_oauth_client", lambda: FakeApp())
        if verify_error is not None:

            def boom(tok):
                raise verify_error

            monkeypatch.setattr(kv, "verify_oauth_access_token", boom)
        else:
            monkeypatch.setattr(kv, "verify_oauth_access_token", lambda tok: identity or _identity())

    def test_happy_path_provisions_and_sets_cookie(self, client, monkeypatch):
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        from src.repositories import users_repo

        assert users_repo().get_by_email("jane@example.com") is not None

    def test_project_mismatch_redirects_with_error(self, client, monkeypatch):
        self._patch_flow(monkeypatch, verify_error=kv.KeboolaVerifyError("project_mismatch", "wrong project"))
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_project_mismatch" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_deactivated_user_rejected(self, client, monkeypatch):
        from src.repositories import users_repo
        import uuid

        uid = str(uuid.uuid4())
        users_repo().create(id=uid, email="jane@example.com", name="Jane")
        users_repo().update(id=uid, active=False)
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=deactivated" in resp.headers["location"]
