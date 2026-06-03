"""Slack Events webhook + identity binding endpoint."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from services.slack_bot.events import _run_logged, _schedule, dispatch_event
from services.slack_bot.sigverify import verify_slack_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slack", tags=["slack"])


@router.post("/events")
async def slack_events(request: Request):
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret or not verify_slack_signature(secret, ts, sig, body):
        raise HTTPException(401, "bad_signature")
    payload = await request.json()
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    if payload.get("type") == "event_callback":
        # Ack-then-async: schedule the (slow, E2B-spawning) dispatch and
        # return the 200 immediately so Slack's 3s budget is never blown.
        # A failure inside the detached task is handled by _run_logged, not
        # by a Slack retry (we already acked). The DM handler emits its own
        # binding/error replies inline, so no top-level on_failure is needed
        # here (the recovery seam is used by later, context-bearing phases).
        _schedule(_run_logged(dispatch_event(request.app, payload["event"])))
        return {"ok": True}
    return {"ok": True}


class BindBody(BaseModel):
    code: str


@router.post("/bind")
async def bind_slack(body: BindBody, request: Request, user: dict = Depends(get_current_user)):
    from services.slack_bot.binding import redeem_verification_code
    repo = request.app.state.chat_repo
    ok = redeem_verification_code(repo._conn, user_email=user["email"], code=body.code)
    if not ok:
        raise HTTPException(400, "invalid_or_expired_code")
    return {"ok": True}
