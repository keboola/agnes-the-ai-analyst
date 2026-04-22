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
