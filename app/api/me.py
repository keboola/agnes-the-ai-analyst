"""Self-scoped user endpoints for the /home onboarding flow.

POST /api/me/onboarded flips ``users.onboarded`` TRUE for the calling user
and writes an audit_log row distinguishing the trigger source:

- ``agnes_init``       — fired by the CLI's ``agnes init`` final step.
- ``self_acknowledged`` — fired by the on-page "I've already set this up"
  button shown to users who set up locally before /home shipped.

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
    source: Literal["agnes_init", "self_acknowledged"] = "agnes_init"


@router.post("/onboarded")
async def post_onboarded(
    body: OnboardedRequest = OnboardedRequest(),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    conn.execute(
        "UPDATE users SET onboarded = TRUE WHERE id = ?",
        [user["id"]],
    )
    AuditRepository(conn).log(
        user_id=user["id"],
        action="user_onboarded",
        params={"source": body.source},
        result="ok",
    )
    return {"status": "ok", "onboarded": True}
