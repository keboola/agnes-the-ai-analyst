"""Authoring Suggestions API (v77) — generic non-admin suggestion queue for
the authoring studio + admin moderation.

Two routers split by audience:

  - ``POST /api/studio/suggestions``                       — any auth user submits
  - ``GET  /api/studio/suggestions/mine``                  — caller sees their own
  - ``GET  /api/admin/authoring-suggestions``              — admin queue
  - ``POST /api/admin/authoring-suggestions/{id}/approve`` — admin resolves
  - ``POST /api/admin/authoring-suggestions/{id}/reject``  — admin resolves

A non-admin who lacks the admin mutation right submits a proposed create
``payload`` here; an admin reviews and approves or rejects it. Approve/reject
are guarded state transitions (only flip a ``pending`` row) and write an
``audit_log`` row so the Activity Center surfaces them.

NOTE (deferred, see the design spec §5): turning an *approved* suggestion into
the real resource automatically ("approval replay") is intentionally NOT done
here — it must be full re-validation through the domain endpoint (re-running
guardrails + re-checking the proposer could target the grant), never a trusted
replay of attacker-shaped payload. Until that lands, approve records the
disposition and the admin creates the resource from the (now-visible) payload.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.web.studio import get_domain
from src.repositories import audit_repo, authoring_suggestions_repo

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/api/studio", tags=["authoring-suggestions"])
admin_router = APIRouter(prefix="/api/admin", tags=["authoring-suggestions"])


class CreateSuggestionBody(BaseModel):
    domain: str
    payload: Dict[str, Any]


class ResolveBody(BaseModel):
    note: Optional[str] = None


@public_router.post("/suggestions", status_code=201)
async def submit_suggestion(
    body: CreateSuggestionBody,
    user: dict = Depends(get_current_user),
):
    if get_domain(body.domain) is None:
        raise HTTPException(status_code=400, detail={"kind": "unknown_domain", "hint": body.domain})
    if not body.payload:
        raise HTTPException(status_code=400, detail={"kind": "empty_payload"})
    sid = authoring_suggestions_repo().create(domain=body.domain, payload=body.payload, created_by=user["email"])
    audit_repo().log(
        user_id=user["email"],
        action="authoring_suggestion.submit",
        resource=sid,
        params={"domain": body.domain},
    )
    return {"id": sid, "status": "pending"}


@public_router.get("/suggestions/mine")
async def my_suggestions(
    user: dict = Depends(get_current_user),
):
    return authoring_suggestions_repo().list(created_by=user["email"])


@admin_router.get("/authoring-suggestions")
async def list_suggestions(
    status: Optional[str] = None,
    domain: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    return authoring_suggestions_repo().list(status=status, domain=domain)


@admin_router.post("/authoring-suggestions/{sid}/approve")
async def approve_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    return _resolve(sid, "approved", body.note, admin)


@admin_router.post("/authoring-suggestions/{sid}/reject")
async def reject_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    return _resolve(sid, "rejected", body.note, admin)


def _resolve(sid: str, status: str, note: Optional[str], admin: dict) -> dict:
    repo = authoring_suggestions_repo()
    if repo.get(sid) is None:
        raise HTTPException(status_code=404, detail={"kind": "not_found"})
    flipped = repo.resolve(sid, status=status, resolved_by=admin["email"], resolution_note=note)
    if not flipped:
        raise HTTPException(status_code=409, detail={"kind": "already_resolved"})
    audit_repo().log(
        user_id=admin["email"],
        action=f"authoring_suggestion.{status}",
        resource=sid,
        params={"note": note},
    )
    return {"id": sid, "status": status}
