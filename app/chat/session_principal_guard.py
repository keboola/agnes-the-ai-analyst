"""deny_principal — 403 a SessionPrincipal on human-only routes."""
from __future__ import annotations

from fastapi import HTTPException

from app.auth.session_principal import SessionPrincipal


def deny_principal(user) -> None:
    if isinstance(user, SessionPrincipal):
        raise HTTPException(status_code=403, detail="not available to co-session token")
