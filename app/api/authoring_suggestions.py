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

Approval auto-creates the real resource for all four domains by REPLAYING the
payload through each domain's own validation + repo create path (the pydantic
request models are the re-validation, design spec §5 — the stored payload is
never trusted blindly). The confused-deputy risk for ``mcp``/``marketplace``
(a stdio ``command`` / git ``url`` in the payload) is mitigated because the
moderation UI renders the COMPLETE payload before the admin clicks approve:
approval is informed consent. See ``_SAFE_REPLAY``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.instance_config import get_studio_enabled
from app.web.studio import get_domain


def _require_studio_enabled() -> None:
    """403 when the instance-level Studio toggle is off.

    Applied to the WHOLE suggestion surface — public submit/read-own AND the
    admin moderation endpoints — mirroring the web routes (owner decision on
    PR #973: the toggle closes the entire Studio, moderation included).
    Pending rows are untouched; they reappear in the queue when the instance
    re-enables Studio. The store submission endpoints
    (``/api/store/entities/from-markdown``) are deliberately NOT gated here —
    that is the Flea store's own surface (also used by the CLI and the MCP
    foundation tool) with its own guardrail/review pipeline.
    """
    if not get_studio_enabled():
        raise HTTPException(status_code=403, detail={"kind": "studio_disabled"})


from src.repositories import (
    audit_repo,
    authoring_suggestions_repo,
    data_packages_repo,
    memory_domains_repo,
)

logger = logging.getLogger(__name__)


# Approval auto-creates the real resource by REPLAYING the payload through the
# same validation + repo create path the domain's own endpoint uses. The
# pydantic models below ARE the re-validation (design spec §5) — the stored
# payload is never trusted blindly. The confused-deputy risk (a proposer slipping
# a stdio ``command`` / git ``url`` past the admin) is mitigated because the
# moderation UI renders the COMPLETE payload before the admin clicks approve:
# approval is informed consent, not a silent replay.
def _replay_data_package(payload: dict, by: str) -> str:
    return data_packages_repo().create(
        name=payload["name"],
        slug=payload["slug"],
        description=payload.get("description"),
        icon=None,
        color=None,
        created_by=by,
    )


def _replay_corporate_memory(payload: dict, by: str) -> str:
    return memory_domains_repo().create(
        name=payload["name"],
        slug=payload["slug"],
        description=payload.get("description"),
        icon=None,
        color=None,
        created_by=by,
    )


def _replay_mcp(payload: dict, by: str) -> str:
    # Re-validate transport/shape via the endpoint's own request model.
    from app.api.admin_mcp import CreateMCPSourceRequest, _require_safe_source_name
    from src.repositories import mcp_sources_repo

    req = CreateMCPSourceRequest(**payload)
    name = (req.name or "").strip()
    _require_safe_source_name(name)
    repo = mcp_sources_repo()
    if repo.get_by_name(name) is not None:
        raise ValueError("name_exists")
    source_id = str(uuid.uuid4())
    repo.upsert(
        id=source_id,
        name=name,
        transport=req.transport,
        command=req.command,
        args=req.args,
        env=req.env,
        url=req.url,
        auth_method=req.auth_method,
        auth_secret_env=req.auth_secret_env,
        enabled=req.enabled,
        scope=req.scope or "shared",
    )
    return source_id


def _replay_marketplace(payload: dict, by: str) -> str:
    from app.api.marketplaces import CreateMarketplaceRequest
    from src.repositories import marketplace_registry_repo

    req = CreateMarketplaceRequest(**payload)
    repo = marketplace_registry_repo()
    if repo.get(req.slug) is not None:
        raise ValueError("slug_exists")
    repo.register(
        id=req.slug,
        name=req.name,
        url=req.url,
        branch=req.branch,
        description=req.description,
        registered_by=by,
        curator_name=req.curator_name,
        curator_email=req.curator_email,
    )
    return req.slug


_SAFE_REPLAY = {
    "data-package": _replay_data_package,
    "corporate-memory": _replay_corporate_memory,
    "mcp": _replay_mcp,
    "marketplace": _replay_marketplace,
}

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
    _require_studio_enabled()
    spec = get_domain(body.domain)
    if spec is None:
        raise HTTPException(status_code=400, detail={"kind": "unknown_domain", "hint": body.domain})
    if spec.submit_directly:
        raise HTTPException(
            status_code=400,
            detail={"kind": "domain_submits_directly", "hint": spec.endpoint},
        )
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
    _require_studio_enabled()
    return authoring_suggestions_repo().list(created_by=user["email"])


@admin_router.get("/authoring-suggestions")
async def list_suggestions(
    status: Optional[str] = None,
    domain: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    return authoring_suggestions_repo().list(status=status, domain=domain)


@admin_router.post("/authoring-suggestions/{sid}/approve")
async def approve_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    repo = authoring_suggestions_repo()
    sug = repo.get(sid)
    if sug is None:
        raise HTTPException(status_code=404, detail={"kind": "not_found"})
    # Atomically CLAIM the suggestion (pending -> approved) BEFORE the
    # side-effecting replay. On a concurrent approve (e.g. PG multi-worker) only
    # the admin who wins the flip runs replay(); the loser gets 409 and never
    # creates a duplicate/orphan resource. If replay then fails, reopen() rolls
    # the claim back to pending so the admin can retry.
    if not repo.resolve(sid, status="approved", resolved_by=admin["email"], resolution_note=body.note):
        raise HTTPException(status_code=409, detail={"kind": "already_resolved"})
    created_resource_id = None
    replay = _SAFE_REPLAY.get(sug["domain"])
    if replay is not None:
        try:
            created_resource_id = replay(sug.get("payload") or {}, admin["email"])
        except KeyError as exc:
            repo.reopen(sid)
            raise HTTPException(status_code=400, detail={"kind": "invalid_payload", "hint": str(exc)})
        except Exception as exc:  # validation / UNIQUE collision — roll the claim back
            repo.reopen(sid)
            raise HTTPException(status_code=409, detail={"kind": "create_failed", "hint": str(exc)})
        repo.set_created_resource_id(sid, created_resource_id)
    audit_repo().log(
        user_id=admin["email"],
        action="authoring_suggestion.approved",
        resource=sid,
        params={"note": body.note, "created_resource_id": created_resource_id},
    )
    return {"id": sid, "status": "approved", "created_resource_id": created_resource_id}


@admin_router.post("/authoring-suggestions/{sid}/reject")
async def reject_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    return _resolve(sid, "rejected", body.note, admin)


def _resolve(
    sid: str,
    status: str,
    note: Optional[str],
    admin: dict,
    created_resource_id: Optional[str] = None,
) -> dict:
    repo = authoring_suggestions_repo()
    if repo.get(sid) is None:
        raise HTTPException(status_code=404, detail={"kind": "not_found"})
    flipped = repo.resolve(
        sid,
        status=status,
        resolved_by=admin["email"],
        resolution_note=note,
        created_resource_id=created_resource_id,
    )
    if not flipped:
        raise HTTPException(status_code=409, detail={"kind": "already_resolved"})
    audit_repo().log(
        user_id=admin["email"],
        action=f"authoring_suggestion.{status}",
        resource=sid,
        params={"note": note, "created_resource_id": created_resource_id},
    )
    return {"id": sid, "status": status, "created_resource_id": created_resource_id}
