# Cloudflare Access Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Cloudflare Access as a third authentication method that coexists with existing password and Google OAuth providers — users behind a Cloudflare Zero Trust tunnel are auto-logged-in via signed edge JWT; direct-access users keep password/Google/PAT flows.

**Architecture:** New provider module `app/auth/providers/cloudflare.py` exposes `is_available()` + `verify_cf_jwt()`. A starlette middleware (`app/auth/middleware.py`, wired in `app/main.py`) runs before route handlers, detects the `Cf-Access-Jwt-Assertion` header, verifies it against the Cloudflare team JWKS with audience check, and — on success — provisions the user and sets the standard `access_token` cookie. Middleware is a pure pass-through when the header is missing or verification fails, preserving all existing flows.

**Tech Stack:** PyJWT 2.12 (already in deps) with `PyJWKClient` for JWKS fetch + 5-min cache; FastAPI middleware decorator; existing `UserRepository` + `create_access_token()` helpers.

---

## Context — What's Already in the Codebase

Existing auth providers follow a common pattern:

- `app/auth/providers/password.py:32` — `is_available()` always True
- `app/auth/providers/google.py:24` — `is_available()` returns True when `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` set
- `app/auth/providers/email.py:31` — `is_available()` returns True when `SMTP_HOST` or `SENDGRID_API_KEY` set

All three self-register via `is_available()` in `app/main.py:138-146`. Login page (`app/web/router.py:194-232`) iterates providers and builds `login_buttons` dynamically.

Cookie flow: `create_access_token()` → `response.set_cookie(key="access_token", ...)` (see `app/auth/providers/google.py:97-103` for the reference pattern). Downstream routes read via `app/auth/dependencies.py:33` → header first, cookie fallback.

Cloudflare Access differs from those providers — it's **not** a clickable login button. It's an edge gate that injects a signed JWT in the `Cf-Access-Jwt-Assertion` header on every request. The provider therefore lives as a **middleware** that runs before handlers and transparently exchanges that edge JWT for our session cookie.

## Env Vars (New)

| Var | Required | Purpose |
|-----|----------|---------|
| `CF_ACCESS_TEAM` | yes | Your Cloudflare team domain prefix (e.g. `keboola` → `https://keboola.cloudflareaccess.com/cdn-cgi/access/certs`) |
| `CF_ACCESS_AUD` | yes | Application AUD tag (from CF dashboard → Access → Applications → your app → Overview) |
| `CF_ACCESS_DOMAIN_ALLOW` | no | Comma-separated email domain allowlist; if unset, falls back to `instance.yaml` `allowed_domains` (same as Google) |

## Security Model

1. **Trust gate:** middleware only inspects the header when **both** `CF_ACCESS_TEAM` and `CF_ACCESS_AUD` are set. If either is unset, the header is ignored — this prevents header spoofing on deployments that don't sit behind CF.
2. **Audience check:** `jwt.decode(..., audience=CF_ACCESS_AUD)` — PyJWT raises on mismatch.
3. **Issuer check:** explicit `options={"require": ["iss"]}` and `issuer=f"https://{CF_ACCESS_TEAM}.cloudflareaccess.com"`.
4. **Signature:** JWKS public keys fetched from `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cached by `PyJWKClient` with 5-min TTL).
5. **Failure is silent pass-through:** invalid/expired/missing JWT → middleware does nothing, request proceeds to route where normal auth (cookie/Bearer/login redirect) kicks in. Never returns 401 from middleware — that would break password/Google login paths.
6. **Domain allowlist:** same logic as `google.py:68-72` — reject email domains outside the configured allowlist.

## File Structure

**Create:**
- `app/auth/providers/cloudflare.py` — provider module (`is_available`, `verify_cf_jwt`, `get_or_create_user_from_cf`)
- `app/auth/middleware.py` — `CloudflareAccessMiddleware` starlette middleware class
- `tests/test_cloudflare_auth.py` — unit + integration tests
- `docs/auth-cloudflare.md` — ops doc (how to configure the CF tunnel + Access app)

**Modify:**
- `app/main.py:137-146` — register middleware between `SessionMiddleware` and route handlers
- `app/web/router.py:194-232` — add optional "Protected by Cloudflare Access" hint on login page when provider is available

---

## Task 1: Test scaffolding — fixtures for JWKS + signed tokens

Before writing any provider code, build the test plumbing. The rest of the plan depends on it.

**Files:**
- Create: `tests/test_cloudflare_auth.py`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write a fixture that generates an RSA keypair and a mock JWKS**

Add to `tests/test_cloudflare_auth.py`:

```python
"""Tests for Cloudflare Access auth provider and middleware."""

import json
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
```

- [ ] **Step 2: Add fixtures — reset cached JWKS client + patch JWKS fetch**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 3: Add a client fixture with CF env configured**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 4: Run pytest to confirm fixtures import cleanly (no tests yet — the file should collect with 0 tests)**

Run: `pytest tests/test_cloudflare_auth.py -v`
Expected: `collected 0 items` (no tests defined yet, but no import/collection errors).

- [ ] **Step 5: Commit**

```bash
git add tests/test_cloudflare_auth.py
git commit -m "test(auth): add Cloudflare Access test scaffolding fixtures"
```

---

## Task 2: Cloudflare provider module — `is_available()`

**Files:**
- Create: `app/auth/providers/cloudflare.py`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing `is_available()` tests**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestCloudflareProviderAvailability -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth.providers.cloudflare'`

- [ ] **Step 3: Create `app/auth/providers/cloudflare.py` with minimal `is_available()`**

Create `app/auth/providers/cloudflare.py`:

```python
"""Cloudflare Access auth provider — verifies edge JWT from Cloudflare Zero Trust.

Unlike password/google/email providers, Cloudflare Access is NOT a clickable
login button. Cloudflare's edge gate injects a signed JWT in the
`Cf-Access-Jwt-Assertion` header on every request. The app trusts that JWT
(after verifying signature + audience) and auto-provisions the user, issuing
our standard `access_token` cookie so downstream route handlers work unchanged.

This module exposes pure functions; the request-interception logic lives in
`app/auth/middleware.py`.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _team() -> str:
    return os.environ.get("CF_ACCESS_TEAM", "")


def _aud() -> str:
    return os.environ.get("CF_ACCESS_AUD", "")


def is_available() -> bool:
    """Provider is active only when BOTH team and aud are configured.

    The two-env-var gate prevents header spoofing on deployments that don't
    sit behind Cloudflare — an attacker could otherwise forge
    `Cf-Access-Jwt-Assertion` and bypass auth.

    Env vars are read at call time (not cached at import) so tests and
    runtime env changes behave predictably.
    """
    return bool(_team() and _aud())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloudflare_auth.py::TestCloudflareProviderAvailability -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auth/providers/cloudflare.py tests/test_cloudflare_auth.py
git commit -m "feat(auth): Cloudflare Access provider skeleton with is_available()"
```

---

## Task 3: JWT verification with JWKS

**Files:**
- Modify: `app/auth/providers/cloudflare.py`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing `verify_cf_jwt` tests**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestVerifyCfJwt -v`
Expected: FAIL with `AttributeError: module 'app.auth.providers.cloudflare' has no attribute 'verify_cf_jwt'`

- [ ] **Step 3: Implement `verify_cf_jwt` in `app/auth/providers/cloudflare.py`**

Replace the contents of `app/auth/providers/cloudflare.py` with:

```python
"""Cloudflare Access auth provider — verifies edge JWT from Cloudflare Zero Trust.

Unlike password/google/email providers, Cloudflare Access is NOT a clickable
login button. Cloudflare's edge gate injects a signed JWT in the
`Cf-Access-Jwt-Assertion` header on every request. The app trusts that JWT
(after verifying signature + audience) and auto-provisions the user, issuing
our standard `access_token` cookie so downstream route handlers work unchanged.

This module exposes pure functions; the request-interception logic lives in
`app/auth/middleware.py`.
"""

import logging
import os
from typing import Optional

import jwt as pyjwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_JWKS_CLIENT: Optional[PyJWKClient] = None
_JWKS_TEAM: Optional[str] = None  # team string the cached client was built for


def _team() -> str:
    return os.environ.get("CF_ACCESS_TEAM", "")


def _aud() -> str:
    return os.environ.get("CF_ACCESS_AUD", "")


def is_available() -> bool:
    """Provider is active only when BOTH team and aud are configured."""
    return bool(_team() and _aud())


def _jwks_url() -> str:
    return f"https://{_team()}.cloudflareaccess.com/cdn-cgi/access/certs"


def _issuer() -> str:
    return f"https://{_team()}.cloudflareaccess.com"


def _get_jwks_client() -> PyJWKClient:
    """Lazy-init JWKS client. PyJWKClient caches keys with 5-min TTL by default.

    If `CF_ACCESS_TEAM` changes (e.g. between tests), rebuild the client.
    """
    global _JWKS_CLIENT, _JWKS_TEAM
    current_team = _team()
    if _JWKS_CLIENT is None or _JWKS_TEAM != current_team:
        _JWKS_CLIENT = PyJWKClient(_jwks_url(), cache_jwk_set=True, lifespan=300)
        _JWKS_TEAM = current_team
    return _JWKS_CLIENT


def verify_cf_jwt(token: str) -> Optional[dict]:
    """Verify a Cloudflare Access JWT. Returns claims dict on success, None on any failure.

    Never raises — all exceptions are logged at debug and mapped to None so the
    middleware can treat them as "pass through to normal auth."
    """
    if not is_available():
        return None
    if not token:
        return None
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_aud(),
            issuer=_issuer(),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        return claims
    except pyjwt.InvalidTokenError as e:
        logger.debug("CF Access JWT invalid: %s", e)
        return None
    except Exception as e:
        # JWKS fetch failure, network error, etc. — never propagate
        logger.warning("CF Access JWT verification error: %s", e)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloudflare_auth.py::TestVerifyCfJwt -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auth/providers/cloudflare.py tests/test_cloudflare_auth.py
git commit -m "feat(auth): verify Cloudflare Access JWT with JWKS + audience + issuer"
```

---

## Task 4: User provisioning from Cloudflare identity

**Files:**
- Modify: `app/auth/providers/cloudflare.py`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing user-provisioning tests**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestGetOrCreateUserFromCf -v`
Expected: FAIL with `AttributeError: ... has no attribute 'get_or_create_user_from_cf'`

- [ ] **Step 3: Add `get_or_create_user_from_cf` to `app/auth/providers/cloudflare.py`**

Append to `app/auth/providers/cloudflare.py`:

```python
import uuid
from typing import Any

import duckdb

from src.repositories.users import UserRepository


def _allowed_domains() -> list[str]:
    """Domain allowlist — CF_ACCESS_DOMAIN_ALLOW env wins, else instance.yaml."""
    env = os.environ.get("CF_ACCESS_DOMAIN_ALLOW", "").strip()
    if env:
        return [d.strip().lower() for d in env.split(",") if d.strip()]
    try:
        from app.instance_config import get_allowed_domains
        return [d.lower() for d in (get_allowed_domains() or [])]
    except Exception:
        return []


def get_or_create_user_from_cf(
    email: str,
    name: str,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[dict[str, Any]]:
    """Look up or provision a user from a verified CF Access identity.

    Returns the user dict on success; returns None when:
    - email domain is outside the allowlist
    - user exists but is deactivated

    New users default to `analyst` role (same default as Google OAuth).
    """
    if not email or not isinstance(email, str):
        return None

    allow = _allowed_domains()
    if allow:
        domain = email.split("@")[-1].lower()
        if domain not in allow:
            logger.info("CF Access: rejecting email outside allowlist: %s", email)
            return None

    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if user is None:
        user_id = str(uuid.uuid4())
        repo.create(
            id=user_id,
            email=email,
            name=name or email.split("@")[0],
            role="analyst",
        )
        user = repo.get_by_email(email)
        logger.info("CF Access: provisioned new user %s", email)

    if not bool(user.get("active", True)):
        logger.info("CF Access: rejecting deactivated user %s", email)
        return None

    return user
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloudflare_auth.py::TestGetOrCreateUserFromCf -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auth/providers/cloudflare.py tests/test_cloudflare_auth.py
git commit -m "feat(auth): provision users from verified Cloudflare Access identity"
```

---

## Task 5: Middleware skeleton — pass-through

**Files:**
- Create: `app/auth/middleware.py`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing pass-through tests**

Append to `tests/test_cloudflare_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestMiddlewarePassthrough -v`
Expected: FAIL — most likely `ModuleNotFoundError: app.auth.middleware` once `create_app()` tries to import it (after Task 7 wires it in). At this stage they'll actually pass because middleware isn't registered yet. If they pass, that's fine — they are assertions of baseline behavior we must preserve.

(The tests explicitly verify the **absence** of CF-induced behavior, so they're a safety net against breaking existing flows once middleware is wired up.)

- [ ] **Step 3: Create `app/auth/middleware.py` with a pass-through middleware**

Create `app/auth/middleware.py`:

```python
"""Starlette middleware that transparently exchanges a verified Cloudflare Access
JWT for our standard `access_token` session cookie.

Runs before route handlers. On every request:

1. If the CF provider is not configured, pass through untouched.
2. If the request carries an `Authorization: Bearer` header (API/CLI/PAT
   client), pass through — those clients don't need a cookie, and setting
   one could leak into subsequent requests from shared clients.
3. If the request already has an `access_token` cookie, pass through
   (don't overwrite an active session — user may have logged in manually).
4. If a `Cf-Access-Jwt-Assertion` header is present and verifies, provision
   the user, mint our JWT, set the cookie, continue.
5. On any verification failure, pass through — the route handler will
   apply its normal auth logic (cookie/Bearer/redirect).

Never returns 401 from the middleware itself — that would break password/Google
login flows on deployments that enable CF as *one of several* auth methods.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth.providers import cloudflare as cf

logger = logging.getLogger(__name__)

CF_HEADER = "Cf-Access-Jwt-Assertion"
COOKIE_NAME = "access_token"
COOKIE_MAX_AGE = 86400  # 24h — matches ACCESS_TOKEN_EXPIRE_HOURS in app/auth/jwt.py


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not cf.is_available():
            return await call_next(request)
        # Bearer clients (PATs, API scripts) manage their own auth — don't set a cookie on them.
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            return await call_next(request)
        if request.cookies.get(COOKIE_NAME):
            return await call_next(request)
        token = request.headers.get(CF_HEADER)
        if not token:
            return await call_next(request)

        claims = cf.verify_cf_jwt(token)
        if claims is None:
            return await call_next(request)

        # Import inside dispatch to avoid circular imports at module load time
        from src.db import get_system_db
        from app.auth.jwt import create_access_token

        email = claims.get("email", "")
        name = claims.get("name", "")
        conn = get_system_db()
        try:
            user = cf.get_or_create_user_from_cf(email=email, name=name, conn=conn)
        finally:
            conn.close()

        if user is None:
            # Email outside allowlist or deactivated — pass through so the
            # normal 401 → /login redirect tells the user why.
            return await call_next(request)

        app_jwt = create_access_token(
            user_id=user["id"],
            email=user["email"],
            role=user["role"],
        )

        response = await call_next(request)
        import os
        use_secure = os.environ.get("TESTING", "").lower() not in ("1", "true")
        response.set_cookie(
            key=COOKIE_NAME,
            value=app_jwt,
            httponly=True,
            max_age=COOKIE_MAX_AGE,
            samesite="lax",
            secure=use_secure,
        )
        # Stash on request.state so this-request handlers can see the identity.
        # (Not strictly needed — the next request will use the cookie — but it
        # makes the first CF-authenticated request behave identically to a
        # cookie-authenticated one.)
        request.state.cf_user = user
        return response
```

- [ ] **Step 4: Run tests to verify they still pass (middleware not yet wired up — baseline behavior unchanged)**

Run: `pytest tests/test_cloudflare_auth.py::TestMiddlewarePassthrough -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/auth/middleware.py tests/test_cloudflare_auth.py
git commit -m "feat(auth): Cloudflare Access middleware skeleton (not yet wired)"
```

---

## Task 6: Wire the middleware into `create_app()`

**Files:**
- Modify: `app/main.py:137`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing integration test (CF header → authenticated)**

Append to `tests/test_cloudflare_auth.py`:

```python
class TestMiddlewareAutoLogin:
    def test_valid_cf_header_auto_logs_in(self, cf_client, make_cf_jwt):
        """Valid CF JWT on /dashboard request → middleware sets cookie → 200 (not 302)."""
        token = make_cf_jwt(email="alice@example.com", name="Alice")
        resp = cf_client.get(
            "/dashboard",
            headers={"Cf-Access-Jwt-Assertion": token},
            follow_redirects=False,
        )
        # Middleware provisioned Alice + set cookie → dashboard renders
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
        )
        # Cookie was set on the response
        assert "access_token" in resp.cookies
        # User now exists
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("alice@example.com")
            assert user is not None
            assert user["role"] == "analyst"
        finally:
            conn.close()

    def test_bearer_pat_passes_through_without_cookie(self, cf_client, make_cf_jwt):
        """A Bearer-authenticated client (PAT/API) must NOT get a cookie set,
        even if a CF header is also present."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        import uuid as _uuid

        conn = get_system_db()
        try:
            uid = str(_uuid.uuid4())
            UserRepository(conn).create(
                id=uid, email="pat@example.com", name="PAT User", role="analyst",
            )
            bearer = create_access_token(uid, "pat@example.com", "analyst")
        finally:
            conn.close()

        cf_token = make_cf_jwt(email="spoofed@example.com")
        resp = cf_client.get(
            "/dashboard",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Cf-Access-Jwt-Assertion": cf_token,
            },
            follow_redirects=False,
        )
        # Bearer auth succeeds, middleware skipped → no cookie leaked
        assert resp.status_code == 200
        assert "access_token" not in resp.cookies
        # Spoofed email must not have been provisioned
        from src.db import get_system_db as _gdb
        conn2 = _gdb()
        try:
            spoofed = UserRepository(conn2).get_by_email("spoofed@example.com")
            assert spoofed is None
        finally:
            conn2.close()

    def test_existing_cookie_wins_over_cf_header(self, cf_client, make_cf_jwt):
        """If the user already has an access_token cookie, middleware must not overwrite it."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        import uuid as _uuid

        conn = get_system_db()
        try:
            uid = str(_uuid.uuid4())
            UserRepository(conn).create(
                id=uid, email="bob@example.com", name="Bob", role="admin",
            )
            existing_token = create_access_token(uid, "bob@example.com", "admin")
        finally:
            conn.close()

        cf_client.cookies.set("access_token", existing_token)
        cf_token = make_cf_jwt(email="carol@example.com", name="Carol")
        resp = cf_client.get(
            "/dashboard",
            headers={"Cf-Access-Jwt-Assertion": cf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Carol must NOT have been provisioned — existing cookie session wins
        from src.db import get_system_db as _gdb
        conn2 = _gdb()
        try:
            carol = UserRepository(conn2).get_by_email("carol@example.com")
            assert carol is None
        finally:
            conn2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestMiddlewareAutoLogin -v`
Expected: FAIL — `test_valid_cf_header_auto_logs_in` gets 302 (login redirect) because middleware isn't registered yet.

- [ ] **Step 3: Register the middleware in `app/main.py`**

In `app/main.py`, locate lines 58-61 (SessionMiddleware setup) and insert the CF middleware registration immediately after the CORS block (after line 71). The new block goes between line 71 and line 73.

Edit `app/main.py` — add this block right before the `# Load .env_overlay` comment on line 73:

```python
    # Cloudflare Access middleware — runs before route handlers to exchange
    # a verified CF edge JWT for our session cookie. Inert unless
    # CF_ACCESS_TEAM + CF_ACCESS_AUD are both set.
    from app.auth.middleware import CloudflareAccessMiddleware
    app.add_middleware(CloudflareAccessMiddleware)
```

(Starlette middleware runs in LIFO order; adding CF last means it runs *first* on inbound requests — exactly what we want.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cloudflare_auth.py::TestMiddlewareAutoLogin tests/test_cloudflare_auth.py::TestMiddlewarePassthrough -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full auth test suite to verify no regressions**

Run: `pytest tests/test_auth_providers.py tests/test_journey_bootstrap_auth.py tests/test_cloudflare_auth.py -v`
Expected: all pass (no existing auth test should break — CF is inert without env vars, and the non-CF client fixtures don't set them).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_cloudflare_auth.py
git commit -m "feat(auth): wire Cloudflare Access middleware into FastAPI app"
```

---

## Task 7: Login page hint when CF is available

**Files:**
- Modify: `app/web/router.py:194-232`
- Test: `tests/test_cloudflare_auth.py`

- [ ] **Step 1: Write the failing login-page test**

Append to `tests/test_cloudflare_auth.py`:

```python
class TestLoginPageCfHint:
    def test_login_page_shows_cf_hint_when_available(self, cf_client):
        """When CF provider is available, login page shows an informational hint."""
        resp = cf_client.get("/login")
        assert resp.status_code == 200
        assert "Cloudflare Access" in resp.text

    def test_login_page_no_cf_hint_when_unavailable(self, no_cf_client):
        """Without CF env, no hint on login page."""
        resp = no_cf_client.get("/login")
        assert resp.status_code == 200
        assert "Cloudflare Access" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloudflare_auth.py::TestLoginPageCfHint -v`
Expected: `test_login_page_shows_cf_hint_when_available` FAILs (hint not yet rendered).

- [ ] **Step 3: Add CF hint to the login page context**

In `app/web/router.py`, locate the `login_page` handler (starts around line 194). Replace the body of the function with:

```python
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    next_path = request.query_params.get("next", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    providers = []
    try:
        from app.auth.providers.google import is_available as google_available
        if google_available():
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    try:
        from app.auth.providers.email import is_available as email_available
        if email_available():
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
    except Exception:
        pass

    # Convert to login_buttons format expected by template
    login_buttons = []
    for p in providers:
        if p["name"] == "google":
            login_buttons.append({"url": "/auth/google/login", "text": "Sign in with Google", "css_class": "btn-primary", "icon_html": ""})
        elif p["name"] == "password":
            _url = "/login/password"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append({"url": _url, "text": "Sign in with Email & Password", "css_class": "btn-secondary", "icon_html": ""})
        elif p["name"] == "email":
            _url = "/login/email"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append({"url": _url, "text": "Sign in with Email Link", "css_class": "btn-secondary", "icon_html": ""})

    cf_available = False
    try:
        from app.auth.providers.cloudflare import is_available as cf_is_available
        cf_available = cf_is_available()
    except Exception:
        pass

    ctx = _build_context(
        request, providers=providers, login_buttons=login_buttons,
        next_path=next_path, cf_available=cf_available,
    )
    return templates.TemplateResponse(request, "login.html", ctx)
```

- [ ] **Step 4: Add the hint to the login template**

In `app/web/templates/login.html`, locate the `{% if not login_buttons %}` block (around line 117) and insert the CF hint just before it:

```html
                {% if cf_available %}
                <p class="login-note" style="margin-top: 16px; font-size: 12px; opacity: 0.8;">
                    This deployment is protected by Cloudflare Access. If you expected to be
                    signed in automatically, please access via your configured Cloudflare URL.
                </p>
                {% endif %}

                {% if not login_buttons %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cloudflare_auth.py::TestLoginPageCfHint -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add app/web/router.py app/web/templates/login.html tests/test_cloudflare_auth.py
git commit -m "feat(auth): show Cloudflare Access hint on login page when enabled"
```

---

## Task 8: Documentation

**Files:**
- Create: `docs/auth-cloudflare.md`
- Modify: `README.md` (add one-line pointer)

- [ ] **Step 1: Write the ops documentation**

Create `docs/auth-cloudflare.md`:

````markdown
# Cloudflare Access Authentication

Agnes can be deployed behind a Cloudflare Zero Trust tunnel with Access
protecting it as an SSO gate. When configured, users who pass CF's
identity check are automatically signed into Agnes — no second login.

This works **alongside** the built-in password and Google OAuth flows:
direct connections (e.g. local dev, CLI with PAT) still use those. Only
the CF-gated path auto-logs-in.

## Prerequisites

- A Cloudflare Zero Trust team (Free tier works for up to 50 users)
- A domain routed to Agnes via Cloudflare Tunnel (`cloudflared`) or CF proxy
- An Access Application configured in front of that domain

## Configure the Access Application

1. In the Cloudflare Zero Trust dashboard → **Access** → **Applications**
   → **Add an application** → **Self-hosted**
2. Application domain: the hostname routed to your Agnes instance
   (e.g. `agnes.yourco.com`)
3. Identity providers: enable your IdP (Google Workspace, Okta, etc.)
4. Policies: add at least one Allow policy (e.g. email ending in `@yourco.com`)
5. After creation, open the app → **Overview** tab and copy the **Application
   Audience (AUD) Tag**

## Configure Agnes

Set two environment variables in your deployment (`.env` or Secret Manager):

```bash
CF_ACCESS_TEAM=yourteam          # from https://yourteam.cloudflareaccess.com
CF_ACCESS_AUD=abc123...          # AUD Tag from the Application → Overview page
```

Optionally restrict which email domains can auto-provision:

```bash
CF_ACCESS_DOMAIN_ALLOW=yourco.com,partner.com
```

If unset, falls back to `allowed_domains` in `config/instance.yaml` (same
allowlist used by the Google OAuth provider).

Restart Agnes. That's it — requests arriving with a valid
`Cf-Access-Jwt-Assertion` header will auto-provision a new `analyst` user
and issue a session cookie.

## Security Model

- **Both env vars required**: if either `CF_ACCESS_TEAM` or `CF_ACCESS_AUD`
  is unset, the middleware is completely inert and the header is ignored.
  This prevents header spoofing on deployments that don't actually sit
  behind Cloudflare.
- **JWT verification**: signature checked against the team's JWKS
  (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, cached 5 min);
  `aud` and `iss` both validated; expired tokens rejected.
- **Never overwrites an existing session**: if the user already has an
  `access_token` cookie, the middleware passes through — you can always
  sign in explicitly with password/Google on a CF-protected deployment.
- **Never 401s from middleware**: if verification fails for any reason, the
  request continues to the normal auth layer — users see the normal login
  page rather than a confusing middleware error.
- **PAT/API (Bearer) clients are skipped**: requests carrying an
  `Authorization: Bearer <token>` header bypass the middleware entirely —
  no cookie is set. This preserves the clean stateless contract for
  CLI tools, CI, and scripts.

## Logout Semantics

Clicking "log out" in Agnes clears the local `access_token` cookie.
**However, if the user is still behind Cloudflare Access**, the next
request will carry a fresh `Cf-Access-Jwt-Assertion` header and the
middleware will immediately re-issue a session cookie — logout appears
to have no effect.

To fully sign out on a CF-gated deployment, the user must also sign out
of their Cloudflare Access session by visiting:

```
https://<your-agnes-domain>/cdn-cgi/access/logout
```

Consider linking to this URL from Agnes's logout UI on CF-gated
deployments, or document it in your internal user guide.

## Troubleshooting

**Auto-login doesn't happen:**
- Check `CF_ACCESS_TEAM` matches the exact subdomain (no protocol, no path):
  `keboola`, not `https://keboola.cloudflareaccess.com`
- Check `CF_ACCESS_AUD` is the **Application AUD Tag**, not the Access
  Team ID
- Verify the request actually has the header:
  `curl -I https://agnes.yourco.com/dashboard` behind CF should show
  `Cf-Access-Jwt-Assertion` in the request (use `cloudflared access curl`
  or browser dev tools)
- Check Agnes logs for `CF Access JWT invalid: ...` or
  `CF Access JWT verification error: ...`

**"User deactivated" redirect:**
- Someone deactivated this user in Agnes's admin panel. CF Access passes
  identity, but Agnes enforces the `active` flag.

**New users arrive with `analyst` role — how do I get admin access?**
- Same as Google OAuth: bootstrap the first admin manually
  (`POST /auth/bootstrap`) or have an existing admin promote via the web UI.
````

- [ ] **Step 2: Add a pointer to `README.md`**

In `README.md`, locate the "Documentation" section (around line 134) and add one line to the list:

```markdown
- [Cloudflare Access Auth](docs/auth-cloudflare.md) — SSO via Cloudflare Zero Trust tunnel
```

Place it between the Onboarding Guide and Deployment Guide entries.

- [ ] **Step 3: Commit**

```bash
git add docs/auth-cloudflare.md README.md
git commit -m "docs(auth): Cloudflare Access setup + troubleshooting guide"
```

---

## Task 9: Final integration — full test sweep + manual smoke

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: all tests pass (633+ existing + ~20 new CF tests).

- [ ] **Step 2: Start the app locally without CF env and verify unchanged behavior**

Run:
```bash
unset CF_ACCESS_TEAM CF_ACCESS_AUD
DATA_DIR=./tmp-data SEED_ADMIN_EMAIL=admin@local.test JWT_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') uvicorn app.main:app --port 8001 &
sleep 2
curl -s -I http://localhost:8001/login | head -1
curl -s -I http://localhost:8001/dashboard | head -3  # should 302 → /login
kill %1
```

Expected: `HTTP/1.1 200 OK` for `/login`, `HTTP/1.1 302` for `/dashboard` with `location: /login?...`.

- [ ] **Step 3: Start the app with CF env and verify CF hint on login page**

Run:
```bash
DATA_DIR=./tmp-data2 SEED_ADMIN_EMAIL=admin@local.test JWT_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') CF_ACCESS_TEAM=example CF_ACCESS_AUD=test-aud uvicorn app.main:app --port 8002 &
sleep 2
curl -s http://localhost:8002/login | grep -c "Cloudflare Access"
kill %1
```

Expected: `1` (hint present).

- [ ] **Step 4: Verify middleware is inert without env even when header is spoofed**

Run (re-use the no-CF server from step 2):
```bash
unset CF_ACCESS_TEAM CF_ACCESS_AUD
DATA_DIR=./tmp-data3 SEED_ADMIN_EMAIL=admin@local.test JWT_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') uvicorn app.main:app --port 8003 &
sleep 2
# Forge a header — should be ignored
curl -s -o /dev/null -w "%{http_code}\n" -H "Cf-Access-Jwt-Assertion: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdHRhY2tlciJ9.fake" http://localhost:8003/dashboard
kill %1
```

Expected: `302` (redirect to login — header ignored because `CF_ACCESS_*` env is unset).

- [ ] **Step 5: Clean up tmp dirs**

```bash
rm -rf tmp-data tmp-data2 tmp-data3
```

- [ ] **Step 6: Final commit and branch status**

```bash
git log --oneline -10
git status  # should be clean
```

---

## Self-Review Notes

**Spec coverage:**
- ✅ Three auth methods coexist (Task 6 verifies existing providers unchanged; Task 7 verifies login page works with/without CF)
- ✅ CF header verification with JWKS + aud + iss + exp (Task 3)
- ✅ User provisioning with domain allowlist (Task 4)
- ✅ Middleware is pass-through on failure (Task 5)
- ✅ Existing cookie wins over CF header (Task 6, `test_existing_cookie_wins_over_cf_header`)
- ✅ Header spoofing prevented when env unset (Task 5 `test_middleware_unavailable_when_env_missing` + Task 9 Step 4)
- ✅ Docs cover setup + security model + troubleshooting (Task 8)

**Non-goals (explicit):**
- No group/role mapping from CF claims — new users always get `analyst`, admins promote manually (same as Google)
- No CLI/PAT integration with CF — PATs remain for programmatic access
- No UI to configure CF from the admin panel — env-only

**Risks / edge cases:**
- JWKS network fetch fails → `verify_cf_jwt` returns None → pass-through. Users see login page. Acceptable.
- Clock skew > 5min → tokens reject as expired. PyJWT has no leeway by default; acceptable (CF tokens are short-lived, typically 24h).
- `TESTING=1` disables cookie `secure=True` (see middleware `use_secure` logic) — matches existing pattern in `google.py:96`.

**Review-driven refinements applied (pre-execution):**
- **Env read at call time, not import time.** `CF_ACCESS_TEAM` / `CF_ACCESS_AUD` are read via helper functions on each call (Task 3), making tests and runtime env changes predictable. `_JWKS_CLIENT` cache keyed by team string so it rebuilds when the team env changes.
- **Autouse fixture** `_reset_cf_jwks_cache` resets the module-level JWKS client + team cache between tests, eliminating cross-test leakage (Task 1 Step 2).
- **PAT Bearer pass-through.** Middleware skips requests carrying `Authorization: Bearer` so CLI / PAT clients don't have cookies silently set on them (Task 5 Step 3). Test `test_bearer_pat_passes_through_without_cookie` verifies (Task 6 Step 1).
- **Email type safety.** `get_or_create_user_from_cf` guards `not isinstance(email, str)` (Task 4 Step 3).
- **Logout semantics documented.** `docs/auth-cloudflare.md` has a dedicated "Logout Semantics" section explaining the CF-session logout URL required for full sign-out on CF-gated deployments (Task 8 Step 1).
