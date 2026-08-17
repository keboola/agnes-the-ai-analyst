"""Tests for Bot Framework Connector JWT verification
(services/teams_bot/sigverify.py).

Uses ``asyncio.run`` rather than ``@pytest.mark.asyncio`` — this repo does
not depend on pytest-asyncio (see tests/test_relay.py for the same idiom).

No ``respx`` in dev deps. Follows the ``_upstream_client()``-seam pattern
from ``tests/test_data_apps_proxy.py``: the module-level ``_http_client()``
seam (``services.teams_bot.sigverify._http_client``) is monkeypatched to
return an ``httpx.AsyncClient(transport=httpx.MockTransport(handler))``
that serves a fake openid-configuration + JWKS document.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from services.teams_bot import sigverify

APP_ID = "test-app-id"
ISSUER = sigverify.BOTFRAMEWORK_ISSUER
JWKS_URI = "https://login.botframework.com/v1/.well-known/keys"


def _rsa_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_for(private_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _token(
    private_key,
    kid: str,
    *,
    aud: str = APP_ID,
    iss: str = ISSUER,
    exp_delta: int = 3600,
) -> str:
    now = int(time.time())
    payload = {"aud": aud, "iss": iss, "iat": now, "nbf": now, "exp": now + exp_delta}
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _reset_jwks_cache(monkeypatch):
    monkeypatch.setattr(sigverify, "_JWKS_CACHE", {})
    monkeypatch.setattr(sigverify, "_LAST_REFETCH_AT", 0.0)


def _install_jwks_transport(monkeypatch, keys_provider):
    """``keys_provider`` is called fresh on every mocked JWKS fetch (a
    callable, not a static list) so a test can simulate key rotation
    between two verify calls. Returns a mutable ``calls`` dict tracking how
    many times each endpoint was hit."""
    calls = {"openid": 0, "jwks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == sigverify.BOTFRAMEWORK_OPENID_CONFIG_URL:
            calls["openid"] += 1
            return httpx.Response(200, json={"jwks_uri": JWKS_URI})
        assert str(request.url) == JWKS_URI
        calls["jwks"] += 1
        return httpx.Response(200, json={"keys": keys_provider()})

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)

    monkeypatch.setattr(sigverify, "_http_client", _client)
    return calls


def test_valid_token_verifies(monkeypatch):
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    token = _token(key, "kid-1")

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is True


def test_wrong_audience_rejected(monkeypatch):
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    token = _token(key, "kid-1", aud="someone-elses-app-id")

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is False


def test_wrong_issuer_rejected(monkeypatch):
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    token = _token(key, "kid-1", iss="https://evil.example.com")

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is False


def test_expired_token_rejected(monkeypatch):
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    token = _token(key, "kid-1", exp_delta=-3600)

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is False


def test_token_signed_with_non_matching_key_rejected(monkeypatch):
    signing_key = _rsa_keypair()
    other_key = _rsa_keypair()
    # JWKS only ever serves the *other* key under this kid — simulates a
    # forged/foreign signature that doesn't match what's on file.
    jwk = _jwk_for(other_key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    token = _token(signing_key, "kid-1")

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is False


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic dXNlcjpwYXNz", "Bearer", "Bearer "],
)
def test_missing_or_malformed_header_rejected(monkeypatch, header):
    result = asyncio.run(sigverify.verify_bot_framework_token(header, APP_ID))
    assert result is False


def test_unknown_kid_triggers_one_refetch_and_succeeds_on_rotation(monkeypatch):
    old_key = _rsa_keypair()
    new_key = _rsa_keypair()
    old_jwk = _jwk_for(old_key, "kid-old")
    new_jwk = _jwk_for(new_key, "kid-new")
    # First serve only the old key; a "rotation" flag flips the JWKS
    # response to also include the new key.
    rotated = {"done": False}

    def keys_provider():
        return [old_jwk, new_jwk] if rotated["done"] else [old_jwk]

    calls = _install_jwks_transport(monkeypatch, keys_provider)

    # Prime the cache with the old key (one fetch).
    old_token = _token(old_key, "kid-old")
    assert asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {old_token}", APP_ID)) is True
    assert calls["jwks"] == 1

    # Simulate Microsoft rotating in a new key server-side, and make sure
    # our throttle guard doesn't block the refetch in this test.
    rotated["done"] = True
    monkeypatch.setattr(sigverify, "_LAST_REFETCH_AT", 0.0)

    new_token = _token(new_key, "kid-new")
    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {new_token}", APP_ID))
    assert result is True
    # Exactly one additional JWKS fetch for the unknown kid.
    assert calls["jwks"] == 2


def test_token_missing_exp_rejected(monkeypatch):
    """PyJWT only checks exp/nbf when present; a token omitting it entirely
    would otherwise verify. `options={"require": [...]}` closes that gap."""
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    _install_jwks_transport(monkeypatch, lambda: [jwk])
    now = int(time.time())
    payload = {"aud": APP_ID, "iss": ISSUER, "iat": now, "nbf": now}  # no exp
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "kid-1"})

    result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert result is False


def test_failed_first_fetch_does_not_block_immediate_retry(monkeypatch):
    """A transient failure on the very first JWKS fetch must not stamp the
    throttle timestamp — otherwise every request for the next 60s fails
    closed with no retry, turning a one-off blip into a fixed outage."""
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    attempt = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == sigverify.BOTFRAMEWORK_OPENID_CONFIG_URL:
            return httpx.Response(200, json={"jwks_uri": JWKS_URI})
        attempt["n"] += 1
        if attempt["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": [jwk]})

    monkeypatch.setattr(
        sigverify, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )
    token = _token(key, "kid-1")

    first = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert first is False  # fetch failed, cache still empty

    # No sleep, no throttle-window manipulation — must retry immediately
    # because the failed attempt above never stamped _LAST_REFETCH_AT.
    second = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {token}", APP_ID))
    assert second is True


def test_unknown_kid_throttled_does_not_refetch_every_request(monkeypatch):
    key = _rsa_keypair()
    jwk = _jwk_for(key, "kid-1")
    calls = _install_jwks_transport(monkeypatch, lambda: [jwk])

    garbage_token = _token(key, "kid-does-not-exist")
    for _ in range(3):
        result = asyncio.run(sigverify.verify_bot_framework_token(f"Bearer {garbage_token}", APP_ID))
        assert result is False

    # First call always refetches (empty cache); the next two are within
    # the throttle window and must not trigger another network round trip.
    assert calls["jwks"] == 1
