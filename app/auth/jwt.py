"""JWT token creation and verification for API auth."""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-in-production")

import warnings as _warnings
if len(SECRET_KEY) < 32 and os.environ.get("TESTING", "").lower() not in ("1", "true"):
    _warnings.warn(
        f"JWT_SECRET_KEY is {len(SECRET_KEY)} chars — minimum 32 recommended for production",
        UserWarning, stacklevel=2,
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24 * 30  # 30 days


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
