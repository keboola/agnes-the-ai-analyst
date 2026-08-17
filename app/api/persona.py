"""Persona-compatible SSE bridge for Agnes.

Persona (`@runtypelabs/persona`) is a vanilla-JS agent UI widget that talks to
any SSE backend. Agnes already streams AG-UI events from its chat manager.
This module adapts those internal frames into Persona's wire event vocabulary
and exposes a single dispatch endpoint the widget can POST to.

Wire format (one SSE record per event):

    event: execution_start
    data: {"executionId":"...","agentId":"...","agentName":"...","seq":0,"ts":"..."}

    event: turn_start
    data: {"executionId":"...","turnId":"...","seq":1}

    event: text_start
    data: {"executionId":"...","turnId":"...","textId":"...","seq":2,"kind":"agent","ts":"..."}

    event: text_delta
    data: {"executionId":"...","turnId":"...","textId":"...","seq":3,"delta":"...","kind":"agent"}

    ... text_complete, turn_complete, execution_complete on a normal turn,
    or execution_error on failure.

This is a proof-of-concept integration: one-shot (stateless) turns. Persona
sends the full message list; we create a fresh Agnes chat session, route the
last user message through it, stream the response, and then discard the
session id. Multi-turn over the same Agnes session can be added later by
surfacing a session id in Persona metadata.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.agent_sse import frame_to_agui
from app.auth.access import can_access
from app.auth.dependencies import _get_db, get_current_user
from app.chat.manager import get_current_chat_manager
from app.chat.streaming_sink import StreamingSink
from app.chat.types import Surface
from src.repositories import agents_repo

router = APIRouter(
    prefix="/api/v1/persona",
    tags=["persona"],
)

#: Idle timeout copied from ``app/api/agent_sessions.py`` — a turn that emits
#: no frame for this long is force-terminated with an ``execution_error``.
_IDLE_TIMEOUT_S = 300.0


class PersonaMessage(BaseModel):
    """One chat message in Persona's request body."""

    model_config = ConfigDict(extra="ignore")

    role: str
    content: str


class PersonaDispatchBody(BaseModel):
    """Persona widget request body.

    The Persona client sends ``messages``, ``context`` and ``metadata``.
    We also accept ``agent_slug`` (top-level or inside ``metadata``) so the
    widget can pick which Agnes agent to run.
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[PersonaMessage] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent_slug: Optional[str] = None


_PERSONA_TERMINAL_EVENTS = {"text_complete", "execution_error", "execution_complete"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persona_event(
    event: str,
    *,
    execution_id: str,
    seq: int,
    turn_id: Optional[str] = None,
    text_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> bytes:
    """Serialize a single Persona wire event as an SSE record."""
    payload: dict[str, Any] = {
        "executionId": execution_id,
        "seq": seq,
    }
    if turn_id is not None:
        payload["turnId"] = turn_id
    if text_id is not None:
        payload["textId"] = text_id
    if extra:
        payload.update(extra)
    if "ts" not in payload:
        payload["ts"] = _now_iso()
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _resolve_agent(user_id: str, body: PersonaDispatchBody, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Resolve the agent to run.

    Priority:
    1. ``body.agent_slug``
    2. ``body.metadata["agent_slug"]``
    3. The user's default agent (``is_default``)

    Raises ``404`` when an explicit slug is missing and ``409`` when no default
    agent exists.
    """
    slug = body.agent_slug or body.metadata.get("agent_slug")
    repo = agents_repo()
    if slug:
        agent = repo.get_by_slug(user_id, slug, include_deleted=False)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent_not_found")
        return agent

    agents = repo.list_for_user(user_id)
    defaults = [a for a in agents if a.get("is_default")]
    if defaults:
        return defaults[0]
    if agents:
        return agents[0]
    raise HTTPException(status_code=409, detail="no_agent_available")


async def _persona_event_stream(
    execution_id: str,
    turn_id: str,
    text_id: str,
    manager: Any,
    session_id: str,
    sink: StreamingSink,
) -> AsyncIterator[bytes]:
    """Drain an Agnes ``StreamingSink`` and emit Persona wire events."""
    seq = 0

    # Lifecycle: start execution, turn, text.
    yield _persona_event(
        "execution_start",
        execution_id=execution_id,
        seq=seq,
    )
    seq += 1
    yield _persona_event(
        "turn_start",
        execution_id=execution_id,
        seq=seq,
        turn_id=turn_id,
    )
    seq += 1
    yield _persona_event(
        "text_start",
        execution_id=execution_id,
        seq=seq,
        turn_id=turn_id,
        text_id=text_id,
        extra={"kind": "agent", "ts": _now_iso()},
    )
    seq += 1

    tool_call_ids: list[str] = []
    try:
        aiter = sink.__aiter__()
        while True:
            try:
                frame = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT_S)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield _persona_event(
                    "execution_error",
                    execution_id=execution_id,
                    seq=seq,
                    extra={"message": "Turn timed out waiting for the next event.", "code": "idle_timeout"},
                )
                return

            agui = frame_to_agui(frame)
            if agui is None:
                continue

            atype = agui["type"]

            if atype == "TEXT_MESSAGE_CONTENT":
                yield _persona_event(
                    "text_delta",
                    execution_id=execution_id,
                    seq=seq,
                    turn_id=turn_id,
                    text_id=text_id,
                    extra={"delta": agui.get("delta") or "", "kind": "agent"},
                )
                seq += 1

            elif atype == "TOOL_CALL_START":
                tool_call_id = f"{execution_id}-tool-{len(tool_call_ids)}"
                tool_call_ids.append(tool_call_id)
                yield _persona_event(
                    "tool_start",
                    execution_id=execution_id,
                    seq=seq,
                    turn_id=turn_id,
                    extra={
                        "toolCallId": tool_call_id,
                        "toolName": agui.get("name"),
                        "toolType": "function",
                        "parameters": agui.get("args") or {},
                    },
                )
                seq += 1

            elif atype == "TOOL_CALL_END" and tool_call_ids:
                tool_call_id = tool_call_ids.pop()
                yield _persona_event(
                    "tool_complete",
                    execution_id=execution_id,
                    seq=seq,
                    turn_id=turn_id,
                    extra={
                        "toolCallId": tool_call_id,
                        "success": True,
                        "result": agui.get("result"),
                    },
                )
                seq += 1

            elif atype == "RUN_ERROR":
                yield _persona_event(
                    "execution_error",
                    execution_id=execution_id,
                    seq=seq,
                    extra={
                        "message": agui.get("message", "Runner reported an error."),
                        "code": agui.get("code", "run_error"),
                    },
                )
                return

            elif atype == "RUN_FINISHED":
                yield _persona_event(
                    "text_complete",
                    execution_id=execution_id,
                    seq=seq,
                    turn_id=turn_id,
                    text_id=text_id,
                )
                seq += 1
                yield _persona_event(
                    "turn_complete",
                    execution_id=execution_id,
                    seq=seq,
                    turn_id=turn_id,
                )
                seq += 1
                yield _persona_event(
                    "execution_complete",
                    execution_id=execution_id,
                    seq=seq,
                )
                return

    finally:
        try:
            await manager.detach_sink(session_id, sink)
        except Exception:
            pass


@router.post("/dispatch")
async def persona_dispatch(
    request: Request,
    body: PersonaDispatchBody,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> StreamingResponse:
    """Accept a Persona request and stream the agent answer as Persona SSE."""
    if not request.app.state.chat_config.enabled:
        raise HTTPException(status_code=503, detail="chat_disabled")

    if not can_access(user["id"], "chat", "chat", conn):
        raise HTTPException(status_code=403, detail="chat_access_denied")

    if not body.messages or not isinstance(body.messages[-1].content, str):
        raise HTTPException(status_code=422, detail="messages_required")
    last_user = body.messages[-1].content.strip()
    if not last_user:
        raise HTTPException(status_code=422, detail="empty_message")

    agent = _resolve_agent(user["id"], body, conn)
    manager = get_current_chat_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="chat_manager_unavailable")

    # ``attach`` may spawn the runner; we must be seated before sending.
    sink = StreamingSink()
    session = None
    try:
        session = await manager.create_session(
            user_email=user.get("email") or user["id"],
            surface=Surface.API,
            agent_id=agent["id"],
            title="Persona chat",
        )
        await manager.attach(session.id, sink, is_primary=False)
        await manager.send_user_message(
            session.id,
            last_user,
            sender_email=user.get("email"),
        )
    except Exception as exc:
        if session is not None:
            try:
                await manager.detach_sink(session.id, sink)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"session_start_failed: {exc}") from exc

    execution_id = uuid.uuid4().hex[:16]
    turn_id = uuid.uuid4().hex[:12]
    text_id = uuid.uuid4().hex[:12]

    return StreamingResponse(
        _persona_event_stream(
            execution_id=execution_id,
            turn_id=turn_id,
            text_id=text_id,
            manager=manager,
            session_id=session.id,
            sink=sink,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
