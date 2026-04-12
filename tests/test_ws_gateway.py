"""Tests for WebSocket Gateway JWT authentication."""

import time

import jwt
import pytest

SECRET = "test-secret-ws-gateway"


@pytest.fixture(autouse=True)
def patch_gateway_secret(monkeypatch):
    """Patch the DESKTOP_JWT_SECRET so the module can be imported."""
    monkeypatch.setenv("DESKTOP_JWT_SECRET", SECRET)
    # Force reload of config module so the env var is picked up
    import importlib
    import services.ws_gateway.config as cfg
    importlib.reload(cfg)
    import services.ws_gateway.auth as auth_mod
    importlib.reload(auth_mod)


def _make_token(payload: dict, secret: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def _import_validate():
    """Return the validate_token function (after env is patched)."""
    from services.ws_gateway.auth import validate_token
    return validate_token


class TestValidateToken:
    def test_valid_token_returns_payload(self):
        """A token with 'sub' and a future 'exp' returns the decoded payload."""
        validate_token = _import_validate()
        payload = {"sub": "alice", "exp": int(time.time()) + 3600}
        token = _make_token(payload)
        result = validate_token(token)
        assert result is not None
        assert result["sub"] == "alice"

    def test_expired_token_returns_none(self):
        """An expired token returns None."""
        validate_token = _import_validate()
        payload = {"sub": "bob", "exp": int(time.time()) - 10}
        token = _make_token(payload)
        result = validate_token(token)
        assert result is None

    def test_invalid_signature_returns_none(self):
        """A token signed with a different secret returns None."""
        validate_token = _import_validate()
        payload = {"sub": "charlie", "exp": int(time.time()) + 3600}
        token = _make_token(payload, secret="wrong-secret")
        result = validate_token(token)
        assert result is None

    def test_token_missing_sub_returns_none(self):
        """A token that has no 'sub' claim returns None."""
        validate_token = _import_validate()
        payload = {"exp": int(time.time()) + 3600, "role": "admin"}
        token = _make_token(payload)
        result = validate_token(token)
        assert result is None

    def test_garbage_string_returns_none(self):
        """A completely invalid token string returns None."""
        validate_token = _import_validate()
        result = validate_token("not.a.token")
        assert result is None

    def test_valid_token_includes_all_claims(self):
        """All custom claims are present in the returned payload."""
        validate_token = _import_validate()
        payload = {"sub": "dave", "exp": int(time.time()) + 3600, "role": "analyst"}
        token = _make_token(payload)
        result = validate_token(token)
        assert result is not None
        assert result["role"] == "analyst"
