"""HTTP surface for the in-product chat agent (#459).

Endpoints
---------
POST   /api/chat                       Stream a turn (SSE).
GET    /api/chat/sessions              List the caller's sessions.
GET    /api/chat/sessions/{id}         Full transcript for one session.
DELETE /api/chat/sessions/{id}         Soft-archive a session.

Every endpoint is gated by ``get_current_user``. Sessions are scoped
to ``user_email`` — a caller never sees another user's chat. Tool
invocations are RBAC-checked inside each handler (see ``app/chat/tools.py``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user, _get_db
from app.chat.loop import ChatTurnConfig, run_turn
from app.chat.persistence import ChatRepository, ChatSession, ChatMessage
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


DEFAULT_CHAT_MODEL = "claude-haiku-4-5-20251001"


def _resolve_model() -> str:
    """Pick the model for the chat loop. ``AGNES_CHAT_MODEL`` overrides;
    otherwise the Haiku 4.5 default. Tests monkeypatch this."""
    return os.environ.get("AGNES_CHAT_MODEL", "").strip() or DEFAULT_CHAT_MODEL


def _build_anthropic_client():
    """Lazily build the Anthropic async client. Raises ``HTTPException(500)``
    if no API key is configured — handled at request time so the rest of
    the app doesn't fail to import when running without an LLM key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Fall back to the same secondary var the LLM factory accepts.
        api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "chat is unavailable: set ANTHROPIC_API_KEY (or LLM_API_KEY) "
                "in the environment, or configure the ai: block in instance.yaml"
            ),
        )
    import anthropic
    return anthropic.AsyncAnthropic(api_key=api_key)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: Optional[str] = None


class SessionSummary(BaseModel):
    id: str
    title: Optional[str]
    started_at: Optional[str]
    last_message_at: Optional[str]
    message_count: int
    archived: bool


class SessionsListResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    id: str
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = []
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    model: Optional[str] = None
    created_at: Optional[str] = None


class SessionDetailResponse(BaseModel):
    session: SessionSummary
    messages: list[SessionMessage]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/sessions", response_model=SessionsListResponse)
def list_sessions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="missing user email")
    repo = ChatRepository(conn)
    sessions = [_session_to_model(s) for s in repo.list_sessions(email)]
    return {"sessions": sessions}


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session(
    session_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ChatRepository(conn)
    session = _require_owned_session(repo, session_id, user)
    messages = [_message_to_model(m) for m in repo.list_messages(session_id)]
    return {"session": _session_to_model(session), "messages": messages}


@router.delete("/sessions/{session_id}", status_code=204)
def archive_session(
    session_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ChatRepository(conn)
    _require_owned_session(repo, session_id, user)
    repo.archive_session(session_id)
    return None


@router.post("")
async def chat_turn(
    request: ChatRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """SSE stream of one chat turn.

    Frame format::

        event: token          data: {"text": "..."}
        event: tool_call      data: {"tool": "...", "args": {...}}
        event: tool_result    data: {"tool": "...", "ok": true, "result": {...}}
        event: assistant_message  data: {"content": "...", "tool_calls": [...], ...}
        event: done           data: {"session_id": "...", "last_message_id": "..."}
        event: error          data: {"error": "..."}

    """
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="missing user email")

    repo = ChatRepository(conn)
    if request.session_id:
        session = _require_owned_session(repo, request.session_id, user)
    else:
        session = repo.create_session(email, title=_auto_title(request.message))

    repo.add_message(session.id, role="user", content=request.message)

    config = ChatTurnConfig(model=_resolve_model())
    client = _build_anthropic_client()
    history = _build_history_for_replay(repo, session.id, exclude_last_user=True)

    async def event_stream() -> AsyncIterator[bytes]:
        # The async generator runs in a request-scoped task; we capture
        # everything we need before the stream takes over (the DI-injected
        # ``conn`` stays alive for the request lifetime via FastAPI).
        last_message_id: Optional[str] = None
        try:
            async for event in run_turn(
                client=client,
                config=config,
                history=history,
                user_message=request.message,
                user=user,
                conn=conn,
            ):
                etype = event.get("type", "")
                if etype == "assistant_message":
                    persisted = repo.add_message(
                        session.id,
                        role="assistant",
                        content=event.get("content", ""),
                        tool_calls=event.get("tool_calls") or None,
                        tokens_in=(event.get("usage") or {}).get("input_tokens"),
                        tokens_out=(event.get("usage") or {}).get("output_tokens"),
                        model=config.model,
                    )
                    last_message_id = persisted.id
                elif etype == "tool_result":
                    repo.add_message(
                        session.id,
                        role="tool_result",
                        content=json.dumps(event.get("result", {}), default=str),
                        tool_calls=[{"tool": event.get("tool"), "ok": event.get("ok")}],
                    )
                yield _sse_frame(etype or "message", event)
            # Final done frame so the client knows the SSE is closing cleanly.
            yield _sse_frame(
                "done",
                {"session_id": session.id, "last_message_id": last_message_id},
            )
        except Exception as exc:
            logger.exception("chat: stream failed")
            yield _sse_frame("error", {"error": str(exc)})

        # Audit row — emitted once per turn, regardless of outcome.
        try:
            AuditRepository(conn).log(
                user_id=user.get("id"),
                action="chat.turn",
                resource=f"chat_session:{session.id}",
                params={
                    "session_id": session.id,
                    "message_chars": len(request.message),
                    "model": config.model,
                },
                result="success" if last_message_id else "no_terminal_message",
            )
        except Exception:
            logger.exception("chat: audit log write failed (non-fatal)")

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # nginx: do not buffer
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sse_frame(event_name: str, payload: dict[str, Any]) -> bytes:
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
    ).encode("utf-8")


def _auto_title(message: str) -> str:
    title = message.strip().splitlines()[0]
    return title[:80] if title else "(empty)"


def _require_owned_session(
    repo: ChatRepository, session_id: str, user: dict,
) -> ChatSession:
    session = repo.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if session.user_email != user.get("email"):
        # 404 — don't leak existence of another user's session.
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _build_history_for_replay(
    repo: ChatRepository, session_id: str, exclude_last_user: bool,
) -> list[dict[str, Any]]:
    """Return a messages array suitable as Anthropic ``messages=`` input.

    The DB stores three role values (``user``, ``assistant``, ``tool_result``)
    but only ``user`` and ``assistant`` belong in the Anthropic history —
    tool_result blocks live inside a user turn alongside the matching
    tool_use ids, and we don't have those ids persisted yet. For v1 we
    therefore drop ``tool_result`` rows from replay; the new turn always
    starts with a fresh tool-use loop, and any prior tool calls live as
    informational text in the preceding assistant message's ``content``.
    """
    out: list[dict[str, Any]] = []
    msgs = repo.list_messages(session_id)
    if exclude_last_user and msgs and msgs[-1].role == "user":
        msgs = msgs[:-1]
    for m in msgs:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            out.append({"role": "assistant", "content": m.content})
        # tool_result rows are skipped (see docstring).
    return out


def _session_to_model(session: ChatSession) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        title=session.title,
        started_at=session.started_at,
        last_message_at=session.last_message_at,
        message_count=session.message_count,
        archived=session.archived,
    )


def _message_to_model(msg: ChatMessage) -> SessionMessage:
    return SessionMessage(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        tool_calls=msg.tool_calls,
        tokens_in=msg.tokens_in,
        tokens_out=msg.tokens_out,
        model=msg.model,
        created_at=msg.created_at,
    )
