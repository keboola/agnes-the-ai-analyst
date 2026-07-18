"""FastAPI chat REST + WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.auth.access import require_resource_access
from app.auth.dependencies import _get_db
from app.chat.frame_seq import stamp_frame
from app.chat.manager import ChatManager, ConcurrencyCapHit, SessionNotFound
from app.chat.persistence import ChatRepository
from app.chat.profiles import get_profile
from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR, list_recognized_commands, merged_skills
from app.chat.types import Surface
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from app.resource_types import ResourceType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Cloud chat is an RBAC resource: denied to everyone by default, granted to a
# group on /admin/access. Every chat endpoint depends on this gate (the WS
# stream is covered transitively — its ticket is only mintable through the
# gated create/reissue endpoints). Admins short-circuit via god-mode. The
# resource is a singleton, so the path template is the fixed id "chat".
require_chat_access = require_resource_access(ResourceType.CHAT, "chat")


# WS auth tickets ride the coordination backend (single-use KV with TTL) —
# not a module-level dict. In single-process ``memory`` mode that's still
# just an in-process dict under the hood (see app.coordination.memory), so
# behavior is unchanged from the original in-memory store; configuring the
# ``redis`` backend makes tickets visible across replicas, which is what HA
# deployments need (see app/startup_guards.py for the multi-process gate).
_TICKET_TTL_SEC = 60
_TICKET_KEY_PREFIX = "ws-ticket:"


def _issue_ticket(chat_id: str, user_email: str) -> str:
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"chat_id": chat_id, "user_email": user_email})
    coordination().kv_set(f"{_TICKET_KEY_PREFIX}{ticket}", payload, ttl_s=_TICKET_TTL_SEC)
    return ticket


def _consume_ticket(ticket: str) -> Optional[tuple[str, str]]:
    raw = coordination().kv_delete(f"{_TICKET_KEY_PREFIX}{ticket}")
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
        return rec["chat_id"], rec["user_email"]
    except (ValueError, KeyError, TypeError):
        return None


class CreateSessionBody(BaseModel):
    surface: str = "web"
    title: Optional[str] = None
    # Optional authoring-agent profile (see app/chat/profiles.py). Spawn-time
    # only — shapes the session persona + knowledge skill; not persisted.
    profile: Optional[str] = None


def _get_manager(request: Request) -> ChatManager:
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail={"kind": "chat_disabled", "hint": "Operator must enable chat.enabled in instance.yaml"},
        )
    return mgr


def _get_repo(request: Request) -> ChatRepository:
    return request.app.state.chat_repo


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    mgr = _get_manager(request)
    if body.profile is not None and get_profile(body.profile) is None:
        raise HTTPException(
            status_code=400,
            detail={"kind": "unknown_profile", "hint": body.profile},
        )
    try:
        s = await mgr.create_session(
            user_email=user["email"],
            surface=Surface(body.surface),
            title=body.title,
            profile=body.profile,
        )
    except ConcurrencyCapHit as exc:
        raise HTTPException(status_code=429, detail={"kind": "concurrency_cap", "hint": str(exc)})
    ticket = _issue_ticket(s.id, user["email"])
    return {
        "id": s.id,
        "surface": s.surface.value,
        "title": s.title,
        "ws_ticket": ticket,
        "ws_url": f"/api/chat/sessions/{s.id}/stream?ticket={ticket}",
    }


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: dict = Depends(require_chat_access),
):
    repo = _get_repo(request)
    rows = repo.list_sessions(user["email"])
    return [
        {
            "id": s.id,
            "surface": s.surface.value,
            "title": s.title,
            "started_at": s.started_at.isoformat(),
            "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None,
            "message_count": s.message_count,
            "paused": s.sandbox_paused_at is not None,
        }
        for s in rows
    ]


@router.get("/skills")
async def list_skills(
    user: dict = Depends(require_chat_access),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-normalized skills + commands catalog for the composer's slash menu.

    ``{"skills": [{name, description, source}], "commands": [{name, description}]}``.

    Two sources are merged server-side (see ``app.chat.skills_catalog`` for the
    full rationale): skills shipped in the bundled chat workspace template
    (``source="bundled"``) and the caller's RBAC-filtered marketplace/store
    plugin skills (``source="marketplace"``) — the same set
    ``app/chat/runner.py``'s ``_bootstrap_marketplace`` installs into the live
    sandbox. **Shadowing**: when a skill name is present in both sources, the
    marketplace entry wins (it is the more user-specific grant). Either source
    failing to list degrades non-fatally — a warning is logged and the other
    source's skills still come back.

    ``commands`` is currently always empty: neither ``app/chat/runner.py`` nor
    the bundled workspace template recognize any slash command today (checked,
    not assumed — see ``list_recognized_commands``'s docstring). Nothing is
    invented ahead of an actual implementation.
    """
    skills = merged_skills(BUNDLED_TEMPLATE_DIR, conn, user)
    return {"skills": skills, "commands": list_recognized_commands()}


@router.post("/sessions/{chat_id}/ticket", status_code=201)
async def reissue_ticket(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Mint a fresh WS ticket for an EXISTING session.

    POST /api/chat/sessions creates a new session every time. When the user
    clicks an old conversation in the sidebar after their WS dropped, the
    frontend needs a way to re-attach to the SAME chat_id (so history
    context continues, message threading is preserved) rather than start
    a new one. This endpoint is that path: 404 if the session doesn't
    exist or belongs to someone else, otherwise the same ticket+url shape
    that ``create_session`` returns.
    """
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    ticket = _issue_ticket(chat_id, user["email"])
    return {
        "id": chat_id,
        "ws_ticket": ticket,
        "ws_url": f"/api/chat/sessions/{chat_id}/stream?ticket={ticket}",
    }


@router.get("/sessions/{chat_id}/messages")
async def list_messages(
    chat_id: str,
    request: Request,
    after_id: Optional[str] = None,
    user: dict = Depends(require_chat_access),
):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    msgs = repo.list_messages(chat_id, after_id=after_id)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "tool_calls": m.tool_calls,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.delete("/sessions/{chat_id}", status_code=204)
async def archive_session(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    mgr = _get_manager(request)
    try:
        await mgr.kill(chat_id, reason="user_archive")
    except Exception:
        logger.exception("kill on archive failed for %s", chat_id)
    repo.archive_session(chat_id)


@router.websocket("/sessions/{chat_id}/stream")
async def ws_stream(ws: WebSocket, chat_id: str, ticket: str):
    try:
        consumed = _consume_ticket(ticket)
    except CoordinationUnavailable:
        await ws.close(code=4503, reason="coordination_unavailable")
        return
    if consumed is None or consumed[0] != chat_id:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    chat_id_v, user_email = consumed

    await ws.accept()
    mgr: ChatManager = ws.app.state.chat_manager

    async def reader_loop() -> None:
        try:
            while True:
                frame = await ws.receive_json()
                kind = frame.get("type")
                if kind == "user_msg":
                    # The client may send ``user_msg`` as soon as the WS is
                    # TCP-open, but ``attach()`` hasn't necessarily finished
                    # ``_spawn_runner`` (E2B sandbox creation can take ~5 s),
                    # so ``live[chat_id]`` may not exist yet. Wait briefly
                    # for ``attach`` to populate it before raising — without
                    # this, an early ``user_msg`` triggers SessionNotFound,
                    # ws_stream closes the WS with 4404, and the user sees
                    # "Disconnected" before the runner has a chance to boot.
                    text = frame.get("text", "")
                    for _ in range(60):  # up to 30 s total at 0.5 s ticks
                        try:
                            # Thread sender_email so per-sender budgets (SR-10)
                            # and departed-participant replay-skip (SR-11) work.
                            await mgr.send_user_message(chat_id_v, text, sender_email=user_email)
                            break
                        except SessionNotFound:
                            await asyncio.sleep(0.5)
                    else:
                        # Sent directly on the WS before any LiveSession
                        # exists (so it can't go through
                        # ChatManager._broadcast) — stamp it here (wave-2F
                        # task 2).
                        await ws.send_json(
                            stamp_frame(
                                chat_id_v,
                                {
                                    "type": "error",
                                    "kind": "runner_not_ready",
                                    "message": "Runner did not become ready within 30 s.",
                                },
                            )
                        )
                elif kind == "cancel":
                    await mgr.cancel(chat_id_v)
        except WebSocketDisconnect:
            return

    try:
        await mgr.attach(chat_id_v, ws)
        await reader_loop()
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
    finally:
        await mgr.detach_sink(chat_id_v, ws)


@router.websocket("/sessions/{session_id}/join")
async def ws_join(ws: WebSocket, session_id: str, ticket: str):
    """WebSocket join route for co-drive participants.

    A participant who obtained a ticket via POST /api/chat/{id}/join-ticket
    connects here to join a live co-session.  The route:

      1. Consumes the short-lived opaque ticket (same coordination-backed
         ticket mechanism as ws_stream) to recover (session_id, participant_email).
      2. Re-verifies that the email is a live (left_at IS NULL) participant
         of the session (SR-9: membership re-verified at WS connect time,
         not just at ticket issuance).
      3. Calls mgr.add_sink(session_id, ws, participant_email), which
         replays persisted history to the joiner and then fans out new
         frames to them alongside the primary sink.

    This is the ONLY path that calls add_sink for web co-drive joiners.
    The primary owner always connects via ws_stream (which calls attach).
    """
    try:
        consumed = _consume_ticket(ticket)
    except CoordinationUnavailable:
        await ws.close(code=4503, reason="coordination_unavailable")
        return
    if consumed is None or consumed[0] != session_id:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    _session_id_v, participant_email = consumed

    mgr: ChatManager = ws.app.state.chat_manager
    repo = ws.app.state.chat_repo

    # SR-9: re-verify live participant membership at WS connect time.
    # The ticket was issued at join-ticket time (SR-9 verified there too),
    # but the participant may have left between ticket issuance and WS connect.
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == participant_email and p.left_at is None for p in parts):
        await ws.close(code=4403, reason="not_a_live_participant")
        return

    await ws.accept()

    async def joiner_reader_loop() -> None:
        try:
            while True:
                frame = await ws.receive_json()
                kind = frame.get("type")
                if kind == "user_msg":
                    text = frame.get("text", "")
                    for _ in range(60):
                        try:
                            # Thread sender_email so per-sender budgets (SR-10)
                            # and departed-participant replay-skip (SR-11) work.
                            await mgr.send_user_message(session_id, text, sender_email=participant_email)
                            break
                        except SessionNotFound:
                            await asyncio.sleep(0.5)
                    else:
                        # See ws_stream's identical branch above — stamp for
                        # the same reason (wave-2F task 2).
                        await ws.send_json(
                            stamp_frame(
                                session_id,
                                {
                                    "type": "error",
                                    "kind": "runner_not_ready",
                                    "message": "Runner did not become ready within 30 s.",
                                },
                            )
                        )
                elif kind == "cancel":
                    await mgr.cancel(session_id)
        except WebSocketDisconnect:
            return

    try:
        # add_sink replays history and appends the joiner to live.sinks.
        # SR-9: raises PermissionError if participant left between accept()
        # and add_sink(); close with 4403 in that case.
        await mgr.add_sink(session_id, ws, participant_email)
        await joiner_reader_loop()
    except PermissionError:
        await ws.close(code=4403, reason="not_a_live_participant")
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
    finally:
        # Mirror ws_stream: a departed joiner must not leave a dead sink in
        # live.sinks — it would block the last-sink detach (linger→pause)
        # policy until the idle reaper. No-op if add_sink never seated it.
        await mgr.detach_sink(session_id, ws)
