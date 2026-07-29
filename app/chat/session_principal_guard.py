"""deny_principal — 403 a restricted principal on human-only routes.

Covers every ``Principal`` kind (co-session runner token, agent-session
sandbox token). The routes behind this guard go on to read ``user["id"]`` /
``user["email"]``, which no principal dataclass can answer — and, more to the
point, they are human-driven co-presence actions (invite, join, leave) that a
machine credential must never perform on its owner's behalf.

The 403 detail string is deliberately unchanged (existing clients and tests
match on it); it under-describes the agent case but says the operative thing.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.auth.session_principal import PRINCIPAL_TYPES


def deny_principal(user) -> None:
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=403, detail="not available to co-session token")
