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

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from src.repositories import audit_repo, usage_repo, users_repo

router = APIRouter(prefix="/api/me", tags=["me"])


class OnboardedRequest(BaseModel):
    source: Literal["agnes_init", "self_acknowledged", "self_unmark"] = "agnes_init"
    onboarded: bool = True


@router.post("/onboarded")
async def post_onboarded(
    body: OnboardedRequest = OnboardedRequest(),
    user: dict = Depends(get_current_user),
):
    target = bool(body.onboarded)
    users_repo().update(user["id"], onboarded=target)
    audit_repo().log(
        user_id=user["id"],
        action="user_onboarded" if target else "user_offboarded",
        params={"source": body.source},
        result="ok",
    )
    return {"status": "ok", "onboarded": target}


# ---------------------------------------------------------------------------
# GET /api/me/home-stats — backing data for the /home status frame
# ---------------------------------------------------------------------------


_WINDOW_INTERVALS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


def _username_for_stats(user: dict) -> str:
    """Map a users row to the filesystem username used by the session
    collector and stored in ``usage_session_summary.username``.

    Mirrors ``app.api.admin_user_sessions._username_from_user``: the
    session collector writes JSONL under the OS username of the agent
    process which, for current deployments, equals the email local-part.
    Kept inline here so this endpoint has no cross-module dependency on
    an admin-only helper; if the mapping evolves both copies must update.
    """
    email: str = user.get("email", "") or ""
    return email.split("@")[0] if "@" in email else email


def compute_home_stats(user: dict, window: str = "24h") -> dict:
    """Pure helper that returns the home-stats payload for the given user.

    Shared by the HTTP endpoint and the /home Jinja handler (server-side
    initial render). Unknown windows clamp to ``24h`` so callers never
    need to pre-validate. Returns a dict with ISO-stringified
    ``last_pull_at`` (or None) so the same shape works for both JSON
    serialization and Jinja rendering.

    Routes through ``usage_repo()`` / ``users_repo()`` so the counters are
    correct on either the DuckDB or Postgres state backend.
    """
    delta = _WINDOW_INTERVALS.get(window)
    if delta is None:
        window = "24h"
        delta = _WINDOW_INTERVALS["24h"]

    username = _username_for_stats(user)
    uid = user.get("id") or ""
    since = datetime.now(timezone.utc) - delta

    stats = usage_repo().home_stats(uid, username, since)
    user_row = users_repo().get_by_id(uid) if uid else None
    last_pull_at = user_row.get("last_pull_at") if user_row else None

    input_t = stats["input_tokens"]
    output_t = stats["output_tokens"]
    cache_read = stats["cache_read"]
    cache_creation = stats["cache_creation"]
    return {
        "window": window,
        "last_pull_at": last_pull_at.isoformat() if last_pull_at else None,
        "sessions": stats["sessions"],
        "prompts": stats["prompts"],
        "tokens": {
            "input": input_t,
            "output": output_t,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total": input_t + output_t + cache_read + cache_creation,
        },
        "projects": stats["projects"],
    }


@router.get("/home-stats")
async def get_home_stats(
    window: str = "24h",
    user: dict = Depends(get_current_user),
):
    """Return the five counters rendered in the /home status frame for
    the calling user, over a 24-hour or 7-day window.

    Missing rows (new user, no telemetry yet) surface as zeros / null
    rather than 404 — the frame still renders cleanly for first-day
    analysts.
    """
    return compute_home_stats(user, window)
