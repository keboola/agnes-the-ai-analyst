"""Self-scoped user endpoints for the /home onboarding flow.

POST /api/me/onboarded toggles ``users.onboarded`` for the calling user
and writes an audit_log row distinguishing the trigger source:

- ``agnes_init``       — fired by the CLI's ``agnes init`` final step.
- ``self_acknowledged`` — fired by the on-page "I've already set this up"
  button shown to users who set up locally before /home shipped.
- ``self_unmark``      — fired by the on-page "Mark me as offboarded"
  button (visible once the user is onboarded).

The body's optional ``onboarded`` field defaults to ``True`` for
backward compat with existing ``agnes init`` calls. Pass ``false`` to
flip back — useful when an analyst wipes their workspace and wants the
inline install steps back, or when an operator demos the not-onboarded
view without an SQL UPDATE.

Idempotent — a second call still returns 200 and writes a second audit
row, so duplicate fires are visible without breaking the client. See
origin: docs/brainstorms/home-page-requirements.md §2 + §6.
"""

from __future__ import annotations

from typing import Literal

import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/me", tags=["me"])


class OnboardedRequest(BaseModel):
    source: Literal["agnes_init", "self_acknowledged", "self_unmark"] = "agnes_init"
    onboarded: bool = True


@router.post("/onboarded")
async def post_onboarded(
    body: OnboardedRequest = OnboardedRequest(),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    target = bool(body.onboarded)
    conn.execute(
        "UPDATE users SET onboarded = ? WHERE id = ?",
        [target, user["id"]],
    )
    AuditRepository(conn).log(
        user_id=user["id"],
        action="user_onboarded" if target else "user_offboarded",
        params={"source": body.source},
        result="ok",
    )
    return {"status": "ok", "onboarded": target}
