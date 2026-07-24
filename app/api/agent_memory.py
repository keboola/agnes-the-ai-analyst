"""The "remember" tool — `POST /api/v1/sessions/{id}/memories` (agent-api
V1c Task 4). The in-sandbox-agent-facing write half of the memory notebook;
`app.chat.agent_profile.materialize_memories` (V1c Task 3) is the read half.

**C2 — bind the write to the CALLING session, never the path `{id}`.** The
in-sandbox agent reaches this route through the secret broker
(`app/api/broker.py`), which resolves the sandbox's ticket to the owner's
real identity and mints a fresh JWT carrying `scope=chat` +
`chat_session_id=<the session the ticket was minted for>` (`_mint_identity_
jwt`). `app.auth.dependencies._stash_chat_session_id_from_token` parks that
claim on `request.state.chat_session_id` for every request authenticated
under such a token — the same claim `app/api/query.py` reads for its
per-session BigQuery budget.

That claim is the ONLY trustworthy signal for "which session (and thus
which agent) is actually calling". The path `{id}` is attacker-controlled
content: the broker replays whatever `{method, path, body}` the sandboxed
agent describes (`app/api/broker.py::_replay`), so a prompt-injected agent
A (owner's `memory_write_mode='auto'`) could simply describe a POST to
`/api/v1/sessions/{sessionB_id}/memories` where session B belongs to a
DIFFERENT agent of the SAME owner (agent B, `memory_write_mode='off'`).
`require_session_principal` alone would happily authorize that — the owner
check passes, since both sessions belong to the same user — which is
exactly how agent A could poison agent B's notebook or bypass B's `off`
policy. So: whenever `request.state.chat_session_id` is present, it MUST
equal the path `{id}` or the request is `403 session_mismatch`, full stop,
before anything about the path session's agent is trusted. When no such
claim is present (an interactive owner session token, or an agent PAT —
neither goes through the broker), the path `{id}` — already ownership-
verified by `require_session_principal` — is exactly the calling session,
so no extra check is needed.

Because the mismatch check runs first, by the time the write mode / guards
below execute, `principal.agent` (resolved from the path `{id}` by
`require_session_principal`) IS the calling agent — no separate "look up
the claim's session" round-trip is needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.api.agent_sessions import SessionRuntimePrincipal, require_session_principal
from app.chat.config import ChatConfig
from src.repositories import agent_memories_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agent-memory"])

#: Fallback defaults when `request.app.state.chat_config` is unavailable
#: (mirrors the `cfg is not None else _DEFAULT_...` pattern in
#: `app/api/query.py`) — kept in sync with `ChatConfig`'s own field
#: defaults rather than duplicating literals.
_DEFAULTS = ChatConfig()


class RememberBody(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content must be non-empty")
        return v


def _err(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _chat_config(request: Request) -> ChatConfig:
    return getattr(request.app.state, "chat_config", None) or _DEFAULTS


@router.post("/sessions/{session_id}/memories", status_code=201)
async def remember(
    session_id: str,
    body: RememberBody,
    request: Request,
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> Dict[str, Any]:
    # C2: the calling session (from a broker-minted chat JWT, if present)
    # must match the path id — see the module docstring. Absent a claim
    # (interactive owner token / agent PAT — neither is broker-routed), the
    # path id IS the calling session, already ownership-checked above.
    calling_session_id = getattr(request.state, "chat_session_id", None)
    if calling_session_id is not None and calling_session_id != session_id:
        raise _err(403, "session_mismatch")

    agent = principal.agent
    mode = agent.get("memory_write_mode") or "propose"
    if mode == "off":
        raise _err(403, "memory_writes_disabled")

    cfg = _chat_config(request)
    if len(body.content) > cfg.agent_memory_max_chars:
        raise _err(413, "memory_too_large")

    repo = agent_memories_repo()
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    if repo.count_recent(agent["id"], since) >= cfg.agent_memory_writes_per_hour:
        raise _err(429, "memory_rate_limited")

    # C3: total-pending cap, independent of the rolling hourly rate limit —
    # a `propose`-mode agent writing just under the hourly rate forever
    # would otherwise grow an unbounded backlog of never-reviewed pending
    # rows. Reaping/ignoring stale pending rows past
    # `agent_memory_pending_ttl_days` when counting is DEFERRED (no reaper
    # exists yet, and `count_pending` — the V1c Task 2 repo method — takes
    # no age cutoff); the config knob is landed now so a future reaper has
    # somewhere to read its threshold from. Until then this cap counts
    # every pending row regardless of age, which is the safe (more
    # conservative, never under-counts) direction to be wrong in.
    if repo.count_pending(agent["id"]) >= cfg.agent_memory_max_pending:
        raise _err(429, "memory_pending_full")

    status = "active" if mode == "auto" else "pending"
    memory_id = str(uuid4())
    repo.create(
        id=memory_id,
        agent_id=agent["id"],
        owner_user_id=agent["owner_user_id"],
        content=body.content,
        source_session_id=session_id,
        status=status,
    )
    return {"id": memory_id, "status": status}
