"""Agent-as-API runtime — `POST /api/v1/agents/{slug}/responses` +
`GET /api/v1/jobs/{id}` (Task 9, `docs/superpowers/specs/
2026-07-21-agent-profiles-and-agent-api-design.md` §3).

One-shot request/response over an owner's agent: creates a fresh headless
chat session (`app/chat/headless.py`), sends the caller's `input`, and
either returns the answer synchronously (bounded by `timeout_s`) or
degrades to a background job (`background: true`, or a sync call whose
wait outran `timeout_s` — the RUN itself is never killed, only the wait).

Auth chain (`require_agent_runtime_principal`): `get_current_user` (never
`get_optional_user` — this surface must 401, not silently downgrade to
anonymous, on a missing/invalid credential) -> resolve the agent by slug
for that owner (404 if none) -> if the credential is an agent PAT, 403
unless its `agent_id` claim matches this agent -> the same `ResourceType.CHAT`
grant check the chat WS route uses (`app/api/chat.py::require_chat_access`).

Idempotency (`Idempotency-Key` header): scoped to `(key, owner, agent)` —
see `src/repositories/idempotency.py`. A replay with an identical raw-body
hash returns the stored response verbatim (same status, same body, `answer`
NOT re-generated); a replay with a different body under the same key is
`409 idempotency_key_reuse`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from app.auth.access import can_access
from app.auth.dependencies import _get_db, get_current_user
from app.auth.pat_resolver import agent_id_from_request
from app.auth.session_principal import SessionPrincipal
from app.chat.agent_usage import agent_config_hash, usage_for_session
from app.chat.headless import run_one_shot
from app.chat.manager import get_current_chat_manager
from app.logging_config import request_id_var
from app.resource_types import ResourceType
from src.repositories import agents_repo, idempotency_repo, jobs_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agent-runtime"])

#: `jobs.kind` for both the background-from-the-start path and the
#: sync-timeout-degrades-to-background path — see `app/worker/kinds.py`'s
#: `_run_agent_response` for how it branches on `payload["mode"]`.
JOB_KIND = "agent_response"

_DEFAULT_TIMEOUT_S = 120
_MIN_TIMEOUT_S = 1
_MAX_TIMEOUT_S = 600

# Public status mapping for GET /api/v1/jobs/{id} — the internal `jobs.status`
# vocabulary (queued/running/done/failed) is DB-lifecycle language; the API
# surfaces request/response-shaped names instead.
_PUBLIC_STATUS_MAP = {
    "queued": "queued",
    "running": "in_progress",
    "done": "completed",
    "failed": "failed",
}


def _idempotency_ttl_s() -> int:
    raw = os.environ.get("AGNES_IDEMPOTENCY_TTL_S")
    if raw is None:
        return 86400  # 24h default, per the task spec
    try:
        return max(int(raw), 1)
    except ValueError:
        return 86400


class AgentResponseRequest(BaseModel):
    input: str
    background: bool = False
    timeout_s: int = _DEFAULT_TIMEOUT_S
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("input")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("input must be non-empty")
        return v


def _clamp_timeout(raw: int) -> int:
    return max(_MIN_TIMEOUT_S, min(_MAX_TIMEOUT_S, raw))


class AgentRuntimePrincipal:
    """Resolved (user, agent) pair for one `/api/v1/agents/{slug}/...` call."""

    def __init__(self, user: dict, agent: dict) -> None:
        self.user = user
        self.agent = agent


def require_agent_runtime_principal(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> AgentRuntimePrincipal:
    """Auth dependency for every `/api/v1/agents/{slug}/...` runtime route.

    A co-session (`SessionPrincipal`) credential is hard-denied — this
    surface is owner-scoped, and a co-session token carries no single
    owner identity to resolve an agent against.
    """
    if isinstance(user, SessionPrincipal):
        raise HTTPException(status_code=403, detail={"code": "agent_runtime_requires_owner_credential"})

    agent = agents_repo().get_by_slug(user["id"], slug)
    if agent is None or agent.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found"})

    pat_agent_id = agent_id_from_request(request)
    if pat_agent_id is not None and pat_agent_id != agent["id"]:
        raise HTTPException(status_code=403, detail={"code": "agent_pat_wrong_agent"})

    # Same check `app/api/chat.py::require_chat_access` gates the chat WS
    # route with — copying the call (not the mechanism), per the task
    # brief, since this dependency already needs its own composite
    # 404/403 ordering that `require_resource_access` doesn't expose.
    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(status_code=403, detail={"code": "chat_access_denied"})

    return AgentRuntimePrincipal(user=user, agent=agent)


def _job_payload_owner_id(job: dict) -> Optional[str]:
    return (job.get("payload_json") or {}).get("owner_user_id")


def _serialize_job(job: dict) -> dict:
    payload = job.get("payload_json") or {}
    result = payload.get("result")

    def _iso(v: Any) -> Optional[str]:
        return v.isoformat() if v is not None else None

    return {
        "id": job["id"],
        "status": _PUBLIC_STATUS_MAP.get(job["status"], job["status"]),
        "result": result,
        "error": job.get("error"),
        "created_at": _iso(job.get("created_at")),
        "finished_at": _iso(job.get("finished_at")),
    }


@router.post("/agents/{slug}/responses")
async def create_agent_response(
    slug: str,
    body: AgentResponseRequest,
    request: Request,
    response: Response,
    principal: AgentRuntimePrincipal = Depends(require_agent_runtime_principal),
) -> dict:
    user, agent = principal.user, principal.agent
    # `app.middleware.request_id.RequestIdMiddleware` (mounted globally in
    # `app/main.py`) already assigns/propagates the per-request id into this
    # contextvar AND appends it as the `x-request-id` RESPONSE header on
    # every request — this router echoes the SAME value into the body
    # rather than minting (and separately header-setting) its own, which
    # would otherwise land as a second, comma-joined `x-request-id` value.
    request_id = request_id_var.get() or uuid.uuid4().hex

    raw_body = await request.body()
    request_hash = hashlib.sha256(raw_body).hexdigest()
    idem_key = request.headers.get("Idempotency-Key")

    if idem_key:
        existing = idempotency_repo().get(idem_key, user["id"], agent["id"])
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail={"code": "idempotency_key_reuse"})
            response.status_code = existing["status_code"]
            # The replayed body deliberately keeps the ORIGINAL call's
            # `request_id` (byte-identical replay contract) — it will differ
            # from the current call's `x-request-id` header, which the
            # middleware always stamps fresh per request.
            return json.loads(existing["response_body"])

    timeout_s = _clamp_timeout(body.timeout_s)
    metadata = body.metadata or {}

    if body.background:
        job = jobs_repo().enqueue(
            JOB_KIND,
            {
                "mode": "fresh",
                "owner_user_id": user["id"],
                "owner_email": user["email"],
                "agent_id": agent["id"],
                "prompt": body.input,
                "metadata": metadata,
            },
        )
        status_code = 202
        result_body: dict = {"job_id": job["id"]}
    else:
        manager = get_current_chat_manager()
        if manager is None:
            raise HTTPException(status_code=503, detail={"code": "chat_disabled"})

        run_result = await run_one_shot(
            manager,
            user_email=user["email"],
            agent_id=agent["id"],
            prompt=body.input,
            timeout_s=timeout_s,
        )
        if run_result["timed_out"]:
            # The turn keeps running server-side — this only bounds the
            # WAIT. Degrade to a background job that resumes waiting on
            # the SAME chat_id (no re-send of the prompt).
            job = jobs_repo().enqueue(
                JOB_KIND,
                {
                    "mode": "continue",
                    "owner_user_id": user["id"],
                    "owner_email": user["email"],
                    "agent_id": agent["id"],
                    "chat_id": run_result["chat_id"],
                    "metadata": metadata,
                },
            )
            status_code = 202
            result_body = {"job_id": job["id"]}
        else:
            usage_accumulator_flush()
            result_body = {
                "answer": run_result["answer"],
                "session_id": run_result["chat_id"],
                "response_id": uuid.uuid4().hex,
                "usage": usage_for_session(agent["id"], run_result["chat_id"]),
                "agent_config_hash": agent_config_hash(agent),
                "request_id": request_id,
            }
            status_code = 200

    response.status_code = status_code

    if idem_key:
        idempotency_repo().put(
            idem_key,
            user["id"],
            agent["id"],
            request_hash,
            json.dumps(result_body),
            status_code,
            ttl_s=_idempotency_ttl_s(),
        )

    return result_body


def usage_accumulator_flush() -> None:
    """Flush the broker's batched `llm_usage` ledger before summing this
    session's usage — a just-finished turn's rows may still be sitting in
    the in-memory buffer otherwise. A thin, mockable indirection over
    `app.api.broker_agent_policy.usage_accumulator.flush()` (deferred
    import: this router must not carry an import-time dependency on the
    broker module)."""
    try:
        from app.api.broker_agent_policy import usage_accumulator

        usage_accumulator.flush()
    except Exception:
        logger.exception("usage_accumulator.flush() failed — usage totals may undercount this response")


@router.get("/jobs/{job_id}")
async def get_agent_job(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """Owner-scoped job read. 404 (not 403) when the job exists but belongs
    to someone else — existence is not leaked to a non-owner, matching the
    posture `app/api/agents_admin.py` documents for `{id}` routes."""
    if isinstance(user, SessionPrincipal):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})

    job = jobs_repo().get(job_id)
    if job is None or _job_payload_owner_id(job) != user.get("id"):
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})

    return _serialize_job(job)
