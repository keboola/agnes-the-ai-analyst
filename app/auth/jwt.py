"""JWT token creation and verification for API auth."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

def _get_secret_key() -> str:
    """Load JWT secret - from env, file, or auto-generated."""
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return os.environ.get("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.secrets import get_jwt_secret
    key = get_jwt_secret()
    if len(key) < 32:
        import warnings as _warnings
        _warnings.warn(
            f"JWT_SECRET_KEY is {len(key)} chars — minimum 32 recommended",
            UserWarning, stacklevel=2,
        )
    return key


_SECRET_KEY_CACHE: Optional[str] = None

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24  # 24 hours


def _get_cached_secret_key() -> str:
    """Return the JWT secret, caching after first call.

    The cache is reset when TESTING env var is set so that each test
    module picks up the correct JWT_SECRET_KEY from monkeypatch/env.
    """
    global _SECRET_KEY_CACHE
    # In test mode, always re-read from env to respect monkeypatch
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return os.environ.get("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    if _SECRET_KEY_CACHE is None:
        _SECRET_KEY_CACHE = _get_secret_key()
    return _SECRET_KEY_CACHE


def create_access_token(
    user_id: str,
    email: str,
    role: str = "analyst",
    expires_delta: Optional[timedelta] = None,
    token_id: Optional[str] = None,
    typ: str = "session",
    omit_exp: bool = False,
) -> str:
    """Create a JWT. `typ` is "session" (interactive login) or "pat" (long-lived).

    If `omit_exp=True`, no `exp` claim is embedded. This is used by PATs with
    "no expiry" — the authoritative expiry check is the DB row in
    `personal_access_tokens.expires_at`, and a claim-less JWT avoids the
    misleading ~100y horizon that previously pretended to be "never".
    """
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "typ": typ,
        "iat": datetime.now(timezone.utc),
        "jti": token_id or uuid.uuid4().hex,
    }
    if not omit_exp:
        expire = datetime.now(timezone.utc) + (
            expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
        )
        payload["exp"] = expire
    return jwt.encode(payload, _get_cached_secret_key(), algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, _get_cached_secret_key(), algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
