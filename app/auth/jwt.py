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


SECRET_KEY = _get_secret_key()

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24  # 24 hours


def create_access_token(
    user_id: str,
    email: str,
    role: str = "analyst",
    expires_delta: Optional[timedelta] = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
