"""JWT authentication for WebSocket Gateway."""

import logging

import jwt

from .config import DESKTOP_JWT_SECRET

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"


def validate_token(token: str) -> dict | None:
    """Validate a JWT token and return the payload.

    Returns the decoded payload dict containing at least "sub" (username)
    and "exp" (expiration), or None if the token is invalid.
    """
    try:
        payload = jwt.decode(token, DESKTOP_JWT_SECRET, algorithms=[ALGORITHM])
        if "sub" not in payload:
            logger.warning("JWT missing 'sub' claim")
            return None
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid JWT token: %s", e)
        return None
