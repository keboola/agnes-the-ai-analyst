"""Tests for Cloudflare Access auth provider and middleware."""

import time
import uuid
from base64 import urlsafe_b64encode
from typing import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt as pyjwt


def _b64url_uint(n: int) -> str:
    """Encode an integer as base64url per RFC 7518 §6.3.1."""
    byte_length = (n.bit_length() + 7) // 8
    return urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()


@pytest.fixture
def cf_keypair():
    """Generate an RSA keypair for signing test CF Access JWTs."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    pub_numbers = pub.public_numbers()
    kid = "test-kid-1"
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(pub_numbers.n),
                "e": _b64url_uint(pub_numbers.e),
            }
        ]
    }
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {"kid": kid, "jwks": jwks, "private_pem": priv_pem}


@pytest.fixture
def make_cf_jwt(cf_keypair) -> Callable[..., str]:
    """Factory: build a signed CF Access JWT with overridable claims."""
    def _make(
        email: str = "user@example.com",
        aud: str = "test-aud-123",
        iss: str = "https://testteam.cloudflareaccess.com",
        exp_offset: int = 3600,
        name: str = "Test User",
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "email": email,
            "name": name,
            "aud": aud,
            "iss": iss,
            "iat": now,
            "exp": now + exp_offset,
            "sub": str(uuid.uuid4()),
        }
        if extra_claims:
            claims.update(extra_claims)
        return pyjwt.encode(
            claims,
            cf_keypair["private_pem"],
            algorithm="RS256",
            headers={"kid": cf_keypair["kid"]},
        )
    return _make


@pytest.fixture(autouse=True)
def _reset_cf_jwks_cache(monkeypatch):
    """Reset the module-level JWKS client so each test starts fresh.

    Without this, a client built from a previous test's team/URL would persist.
    """
    import sys
    mod = sys.modules.get("app.auth.providers.cloudflare")
    if mod is not None:
        monkeypatch.setattr(mod, "_JWKS_CLIENT", None, raising=False)
        monkeypatch.setattr(mod, "_JWKS_TEAM", None, raising=False)


@pytest.fixture
def patch_jwks(monkeypatch, cf_keypair):
    """Patch PyJWKClient so verify_cf_jwt reads our test key instead of hitting the network."""
    from cryptography.hazmat.primitives import serialization as _ser
    # Build a PyJWK-compatible signing key object from the public key
    pub_pem = _ser.load_pem_private_key(cf_keypair["private_pem"], password=None).public_key().public_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PublicFormat.SubjectPublicKeyInfo,
    )

    class _FakeSigningKey:
        def __init__(self, key_bytes: bytes):
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            self.key = load_pem_public_key(key_bytes)

    def _fake_get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(pub_pem)

    monkeypatch.setattr(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        _fake_get_signing_key_from_jwt,
    )


@pytest.fixture
def cf_client(tmp_path, monkeypatch, patch_jwks):
    """TestClient with CF_ACCESS_* env vars set so the provider is available."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
    monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")

    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app()
    return TestClient(app)


@pytest.fixture
def no_cf_client(tmp_path, monkeypatch):
    """TestClient WITHOUT CF_ACCESS_* env — provider should be unavailable."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.delenv("CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)

    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app()
    return TestClient(app)


class TestCloudflareProviderAvailability:
    def test_unavailable_without_env(self, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_TEAM", raising=False)
        monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
        # Force re-import so module-level env reads are fresh
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)
        assert cf_mod.is_available() is False

    def test_unavailable_with_only_team(self, monkeypatch):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)
        assert cf_mod.is_available() is False

    def test_unavailable_with_only_aud(self, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_TEAM", raising=False)
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)
        assert cf_mod.is_available() is False

    def test_available_with_both_env(self, monkeypatch):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)
        assert cf_mod.is_available() is True


class TestVerifyCfJwt:
    def test_valid_token_returns_claims(self, monkeypatch, patch_jwks, make_cf_jwt):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        token = make_cf_jwt(email="alice@example.com")
        claims = cf_mod.verify_cf_jwt(token)
        assert claims is not None
        assert claims["email"] == "alice@example.com"

    def test_wrong_audience_rejected(self, monkeypatch, patch_jwks, make_cf_jwt):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        token = make_cf_jwt(aud="wrong-aud")
        assert cf_mod.verify_cf_jwt(token) is None

    def test_wrong_issuer_rejected(self, monkeypatch, patch_jwks, make_cf_jwt):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        token = make_cf_jwt(iss="https://evil.example.com")
        assert cf_mod.verify_cf_jwt(token) is None

    def test_expired_token_rejected(self, monkeypatch, patch_jwks, make_cf_jwt):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        token = make_cf_jwt(exp_offset=-60)  # expired 60s ago
        assert cf_mod.verify_cf_jwt(token) is None

    def test_malformed_token_rejected(self, monkeypatch, patch_jwks):
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        assert cf_mod.verify_cf_jwt("not-a-jwt") is None
        assert cf_mod.verify_cf_jwt("") is None

    def test_verify_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_TEAM", raising=False)
        monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        assert cf_mod.verify_cf_jwt("anything") is None


class TestGetOrCreateUserFromCf:
    def test_creates_new_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        conn = get_system_db()
        try:
            user = cf_mod.get_or_create_user_from_cf(
                email="new@example.com", name="New User", conn=conn,
            )
            assert user is not None
            assert user["email"] == "new@example.com"
            assert user["role"] == "analyst"
        finally:
            conn.close()

    def test_returns_existing_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        conn = get_system_db()
        try:
            UserRepository(conn).create(
                id="existing-id", email="existing@example.com",
                name="Existing", role="admin",
            )
            user = cf_mod.get_or_create_user_from_cf(
                email="existing@example.com", name="Existing", conn=conn,
            )
            assert user["id"] == "existing-id"
            assert user["role"] == "admin"  # role preserved
        finally:
            conn.close()

    def test_deactivated_user_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        conn = get_system_db()
        try:
            UserRepository(conn).create(
                id="deact-id", email="deact@example.com",
                name="Deact", role="analyst",
            )
            UserRepository(conn).update(id="deact-id", active=False)
            user = cf_mod.get_or_create_user_from_cf(
                email="deact@example.com", name="Deact", conn=conn,
            )
            assert user is None
        finally:
            conn.close()

    def test_domain_allowlist_rejects_outsider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        monkeypatch.setenv("CF_ACCESS_DOMAIN_ALLOW", "example.com")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        conn = get_system_db()
        try:
            user = cf_mod.get_or_create_user_from_cf(
                email="outsider@evil.com", name="Outsider", conn=conn,
            )
            assert user is None
        finally:
            conn.close()

    def test_domain_allowlist_accepts_insider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        monkeypatch.setenv("CF_ACCESS_DOMAIN_ALLOW", "example.com,partner.com")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        conn = get_system_db()
        try:
            user = cf_mod.get_or_create_user_from_cf(
                email="ok@partner.com", name="Partner", conn=conn,
            )
            assert user is not None
            assert user["email"] == "ok@partner.com"
        finally:
            conn.close()

    def test_empty_or_none_email_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("CF_ACCESS_TEAM", "testteam")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-123")
        import importlib
        from app.auth.providers import cloudflare as cf_mod
        importlib.reload(cf_mod)

        from src.db import get_system_db
        conn = get_system_db()
        try:
            assert cf_mod.get_or_create_user_from_cf(email="", name="x", conn=conn) is None
            assert cf_mod.get_or_create_user_from_cf(email=None, name="x", conn=conn) is None
            assert cf_mod.get_or_create_user_from_cf(email=123, name="x", conn=conn) is None
        finally:
            conn.close()


class TestMiddlewarePassthrough:
    def test_no_header_no_cookie_redirects_to_login(self, cf_client):
        """Dashboard without any auth → normal 302 to /login (middleware must not interfere)."""
        resp = cf_client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")

    def test_invalid_cf_header_passes_through(self, cf_client):
        """Garbage CF header → middleware ignores it → normal 302 to login."""
        resp = cf_client.get(
            "/dashboard",
            headers={"Cf-Access-Jwt-Assertion": "not-a-valid-jwt"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")

    def test_middleware_unavailable_when_env_missing(self, no_cf_client, make_cf_jwt):
        """Without CF_ACCESS_* env, middleware must be inert even if header is present."""
        # Note: make_cf_jwt still produces a token but middleware should ignore it.
        token = make_cf_jwt()
        resp = no_cf_client.get(
            "/dashboard",
            headers={"Cf-Access-Jwt-Assertion": token},
            follow_redirects=False,
        )
        # No cookie set, normal redirect to login
        assert resp.status_code == 302
        assert "access_token" not in resp.cookies
