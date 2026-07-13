"""Admin REST API for maintained knowledge digests (K4, #799).

Mirrors ``app/api/memory_domains.py`` for the ``knowledge_digests`` table
(Tasks 1-2). Endpoints:

  - ``GET    /api/admin/knowledge-digests``          — list (output_md
    truncated to a 280-char preview + ``output_chars`` count)
  - ``POST   /api/admin/knowledge-digests``          — create
  - ``GET    /api/admin/knowledge-digests/{id}``     — detail (full output_md)
  - ``PUT    /api/admin/knowledge-digests/{id}``     — update
    title/instructions/source_corpus_ids (slug is immutable — it is a
    filename on every analyst laptop, see the K4 plan's Global Constraints)
  - ``DELETE /api/admin/knowledge-digests/{id}``     — delete + clean up any
    dangling ``resource_grants`` rows

All admin-only (``Depends(require_admin)``) — read/distribution access to a
digest's content rides the separate ``ResourceType.KNOWLEDGE_DIGEST`` grant
(``app/resource_types.py``), enforced at the manifest/content-serving layer
(Task 6), not here.

Audit actions are ``knowledge_digest.create/update/delete``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.auth.access import require_admin
from src.repositories import (
    audit_repo,
    file_corpora_repo,
    knowledge_digests_repo,
    resource_grants_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/knowledge-digests", tags=["knowledge-digests"])

# Must satisfy the client's _SAFE_ID_RE (cli/lib/pull.py:83) — the slug
# becomes a filename (`ka_<slug>.md`) on every analyst laptop.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PREVIEW_CHARS = 280


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateKnowledgeDigestRequest(BaseModel):
    slug: str
    title: str
    instructions: str
    source_corpus_ids: List[str] = []

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v or ""):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return v

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title is required")
        if len(v) > 200:
            raise ValueError("title must be <= 200 chars")
        return v

    @field_validator("instructions")
    @classmethod
    def _check_instructions(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("instructions is required")
        if len(v) > 20000:
            raise ValueError("instructions must be <= 20000 chars")
        return v

    @field_validator("source_corpus_ids")
    @classmethod
    def _check_source_corpus_ids(cls, v: List[str]) -> List[str]:
        if len(v) > 50:
            raise ValueError("source_corpus_ids must have <= 50 entries")
        return v


class UpdateKnowledgeDigestRequest(BaseModel):
    title: Optional[str] = None
    instructions: Optional[str] = None
    source_corpus_ids: Optional[List[str]] = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        if len(v) > 200:
            raise ValueError("title must be <= 200 chars")
        return v

    @field_validator("instructions")
    @classmethod
    def _check_instructions(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("instructions must not be empty")
        if len(v) > 20000:
            raise ValueError("instructions must be <= 20000 chars")
        return v

    @field_validator("source_corpus_ids")
    @classmethod
    def _check_source_corpus_ids(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None and len(v) > 50:
            raise ValueError("source_corpus_ids must have <= 50 entries")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
    params_before: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _validate_source_corpus_ids(source_corpus_ids: List[str]) -> None:
    """400 if any id does not resolve to a live ``file_corpora`` row."""
    repo = file_corpora_repo()
    for cid in source_corpus_ids:
        if repo.get(cid) is None:
            raise HTTPException(status_code=400, detail=f"unknown_source_corpus_id:{cid}")


def _serialize(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": d["id"],
        "slug": d["slug"],
        "title": d["title"],
        "instructions": d["instructions"],
        "source_corpus_ids": d.get("source_corpus_ids") or [],
        "output_md": d.get("output_md"),
        "source_fingerprint": d.get("source_fingerprint"),
        "generated_at": d["generated_at"].isoformat() if d.get("generated_at") else None,
        "model": d.get("model"),
        "status": d.get("status") or "pending",
        "status_reason": d.get("status_reason"),
        "created_by": d.get("created_by"),
        "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
        "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
    }


def _serialize_preview(d: Dict[str, Any]) -> Dict[str, Any]:
    """List-view serialization: output_md truncated to a preview + char count."""
    out = _serialize(d)
    full_md = out.get("output_md") or ""
    out["output_chars"] = len(full_md)
    out["output_md"] = full_md[:_PREVIEW_CHARS]
    return out


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_knowledge_digests(
    user: dict = Depends(require_admin),
):
    """List all digests with a truncated ``output_md`` preview."""
    rows = knowledge_digests_repo().list()
    return {"items": [_serialize_preview(r) for r in rows]}


@router.post("", status_code=201)
async def create_knowledge_digest(
    payload: CreateKnowledgeDigestRequest,
    user: dict = Depends(require_admin),
):
    _validate_source_corpus_ids(payload.source_corpus_ids)
    repo = knowledge_digests_repo()
    if repo.get_by_slug(payload.slug):
        raise HTTPException(status_code=409, detail="slug_exists")
    try:
        digest_id = repo.create(
            slug=payload.slug,
            title=payload.title,
            instructions=payload.instructions,
            source_corpus_ids=payload.source_corpus_ids,
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    _audit(
        user["id"],
        "knowledge_digest.create",
        f"knowledge_digest:{digest_id}",
        {"slug": payload.slug, "title": payload.title},
    )
    return _serialize(repo.get(digest_id))


@router.get("/{digest_id}")
async def get_knowledge_digest(
    digest_id: str,
    user: dict = Depends(require_admin),
):
    """Detail view — full ``output_md`` (list only ships a preview)."""
    d = knowledge_digests_repo().get(digest_id)
    if not d:
        raise HTTPException(status_code=404, detail="knowledge_digest_not_found")
    return _serialize(d)


@router.put("/{digest_id}")
async def update_knowledge_digest(
    digest_id: str,
    payload: UpdateKnowledgeDigestRequest,
    user: dict = Depends(require_admin),
):
    repo = knowledge_digests_repo()
    existing = repo.get(digest_id)
    if not existing:
        raise HTTPException(status_code=404, detail="knowledge_digest_not_found")
    if payload.source_corpus_ids is not None:
        _validate_source_corpus_ids(payload.source_corpus_ids)
    before = {
        "title": existing.get("title"),
        "instructions": existing.get("instructions"),
        "source_corpus_ids": existing.get("source_corpus_ids"),
    }
    repo.update(
        digest_id,
        title=payload.title,
        instructions=payload.instructions,
        source_corpus_ids=payload.source_corpus_ids,
    )
    fresh = repo.get(digest_id)
    after = {
        "title": fresh.get("title") if fresh else None,
        "instructions": fresh.get("instructions") if fresh else None,
        "source_corpus_ids": fresh.get("source_corpus_ids") if fresh else None,
    }
    _audit(
        user["id"],
        "knowledge_digest.update",
        f"knowledge_digest:{digest_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize(fresh) if fresh else {"id": digest_id}


@router.delete("/{digest_id}", status_code=204)
async def delete_knowledge_digest(
    digest_id: str,
    user: dict = Depends(require_admin),
):
    repo = knowledge_digests_repo()
    existing = repo.get(digest_id)
    if not existing:
        raise HTTPException(status_code=404, detail="knowledge_digest_not_found")
    repo.delete(digest_id)
    removed_grants = resource_grants_repo().delete_by_resource("knowledge_digest", digest_id)
    _audit(
        user["id"],
        "knowledge_digest.delete",
        f"knowledge_digest:{digest_id}",
        {"slug": existing.get("slug"), "grants_removed": removed_grants},
    )
