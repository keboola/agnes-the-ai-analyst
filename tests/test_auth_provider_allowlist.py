"""auth.providers allowlist: unset = today's behavior; set = allowlist ∩ availability;
excluded providers' endpoints 404 — including the shared-router POST /auth/token."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(providers_env: str | None):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        # email.is_available() requires SMTP/SendGrid config (or local-dev
        # mode, which has its own side effect of auto-seeding + redirecting
        # /login away entirely). The login-page assertions below need the
        # email provider to actually be available so the allowlist — not
        # availability — is what's under test; no email is ever sent.
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        if providers_env is None:
            monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        else:
            monkeypatch.setenv("AGNES_AUTH_PROVIDERS", providers_env)
        from app.main import create_app

        return TestClient(create_app())

    return _make


class TestRegistry:
    def test_unset_allows_everything(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth.provider_registry import provider_allowed

        assert all(provider_allowed(p) for p in ("google", "email", "password", "keboola"))

    def test_set_narrows(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google")
        from app.auth.provider_registry import provider_allowed

        assert provider_allowed("google") is True
        assert provider_allowed("password") is False

    def test_unknown_names_ignored_empty_result_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "definitely-not-a-provider")
        from app.auth.provider_registry import configured_allowlist

        assert configured_allowlist() is None  # fail-open, loudly logged


class TestEndpointGating:
    def test_password_endpoints_404_when_excluded(self, make_client):
        client = make_client("google")
        # Router-level dependency: any matched route under /auth/password 404s.
        # (POST — the login form route; a GET would 405 before dependencies run.)
        assert client.post("/auth/password/login/web", data={}).status_code == 404
        # The easy-to-miss one: the shared-router password grant.
        resp = client.post("/auth/token", data={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 404
        # Login sub-page is gated too.
        assert client.get("/login/password").status_code == 404

    def test_email_endpoints_404_when_excluded(self, make_client):
        # Symmetric to the password case: excluding `email` must 404 both the
        # magic-link sub-page and its form/JSON send-link endpoints, so an
        # instance can't be narrowed to a provider whose door still answers.
        client = make_client("password")
        assert client.get("/login/email").status_code == 404
        assert client.post("/auth/email/send-link/web", data={"email": "a@b.c"}).status_code == 404
        assert client.post("/auth/email/send-link", json={"email": "a@b.c"}).status_code == 404

    def test_password_endpoints_live_when_unset(self, make_client):
        client = make_client(None)
        resp = client.post("/auth/token", data={"email": "nobody@example.com", "password": "x"})
        assert resp.status_code != 404  # 401/422 is fine — the endpoint exists

    def test_login_page_hides_excluded_buttons(self, make_client):
        client = make_client("email")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html
        assert "Sign in with Email &amp; Password" not in html and "Sign in with Email & Password" not in html

    def test_login_page_unset_is_todays_behavior(self, make_client):
        client = make_client(None)
        html = client.get("/login").text
        # No Google credentials in the test env → password + email link, exactly as before.
        assert "Sign in with Email & Password" in html or "Sign in with Email &amp; Password" in html
        assert "Sign in with Email Link" in html
