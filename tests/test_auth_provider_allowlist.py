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

        assert all(provider_allowed(p) for p in ("google", "email", "password", "keboola", "microsoft"))

    def test_microsoft_is_known(self):
        from app.auth.provider_registry import KNOWN_PROVIDERS

        assert "microsoft" in KNOWN_PROVIDERS

    def test_set_narrows(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google")
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        # Narrowing is what's under test, not availability — make the named
        # provider count as configured so the lockout rescue stays out of it.
        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: True)
        assert provider_allowed("google") is True
        assert provider_allowed("password") is False

    def test_unknown_names_ignored_empty_result_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "definitely-not-a-provider")
        from app.auth.provider_registry import configured_allowlist

        assert configured_allowlist() is None  # fail-open, loudly logged


class TestLockoutRescue:
    """An allowlist naming only *unconfigured* providers admits nobody; the
    reader treats it as unset (all providers), so the env/static-file path —
    which the admin API's write-time guard never sees — cannot lock the
    instance out (Devin Review on PR #1288)."""

    def test_all_named_providers_unconfigured_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist, provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: False)
        assert configured_allowlist() is None
        assert provider_allowed("password") is True

    def test_allowlist_stands_when_any_named_provider_is_configured(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola,google")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist, provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: name == "google")
        assert configured_allowlist() == ["keboola", "google"]
        assert provider_allowed("password") is False

    def test_web_lockout_config_still_offers_usable_logins(self, make_client):
        # End-to-end: keboola named alone with no stack configured — exactly
        # the lockout scenario. Login page must still offer usable methods and
        # the shared-router password grant must answer.
        client = make_client("keboola")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html
        resp = client.post("/auth/token", data={"email": "nobody@example.com", "password": "x"})
        assert resp.status_code != 404


class TestEndpointGating:
    def test_password_endpoints_404_when_excluded(self, make_client):
        # `email` is configured by the fixture (SMTP_HOST), so the allowlist
        # stands on its own and the lockout rescue never enters the picture.
        client = make_client("email")
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

    def test_microsoft_endpoints_404_when_excluded(self, make_client):
        # Symmetric to the keboola/password cases: excluding `microsoft` must
        # 404 its login door even though the router is always registered.
        client = make_client("email")
        assert client.get("/auth/microsoft/login", follow_redirects=False).status_code == 404

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
