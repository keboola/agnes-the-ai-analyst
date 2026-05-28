"""FastAPI chat REST + WebSocket endpoints."""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.chat.manager import ChatManager, ConcurrencyCapHit, SessionNotFound
from app.chat.persistence import ChatRepository
from app.chat.types import Surface

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


# In-memory ticket store. Per spec: single-worker constraint enforced at
# startup; HA needs ticket store in DuckDB or Redis (future spec).
_TICKETS: dict[str, tuple[str, str, float]] = {}  # ticket -> (chat_id, user_email, expires_at)
_TICKET_TTL_SEC = 60


def _issue_ticket(chat_id: str, user_email: str) -> str:
    ticket = secrets.token_urlsafe(32)
    _TICKETS[ticket] = (chat_id, user_email, time.time() + _TICKET_TTL_SEC)
    return ticket


def _consume_ticket(ticket: str) -> Optional[tuple[str, str]]:
    rec = _TICKETS.pop(ticket, None)
    if rec is None:
        return None
    chat_id, user_email, expires_at = rec
    if time.time() > expires_at:
        return None
    return chat_id, user_email


class CreateSessionBody(BaseModel):
    surface: str = "web"
    title: Optional[str] = None


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


@router.post("/sessions")
async def create_session(
    body: CreateSessionBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    mgr = _get_manager(request)
    try:
        s = await mgr.create_session(
            user_email=user["email"],
            surface=Surface(body.surface),
            title=body.title,
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
    user: dict = Depends(get_current_user),
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
        }
        for s in rows
    ]


@router.get("/sessions/{chat_id}/messages")
async def list_messages(
    chat_id: str,
    request: Request,
    after_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
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


@router.delete("/sessions/{chat_id}")
async def archive_session(
    chat_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
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
    return {"ok": True}


@router.websocket("/sessions/{chat_id}/stream")
async def ws_stream(ws: WebSocket, chat_id: str, ticket: str):
    consumed = _consume_ticket(ticket)
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
                    await mgr.send_user_message(chat_id_v, frame.get("text", ""))
                elif kind == "cancel":
                    await mgr.cancel(chat_id_v)
        except WebSocketDisconnect:
            return

    try:
        import asyncio
        attach_task = asyncio.create_task(mgr.attach(chat_id_v, ws))
        await reader_loop()
        attach_task.cancel()
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
