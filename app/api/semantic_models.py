"""Semantic-model / semantic-source admin API, plus the public export and
search surface (open semantic-layer contract, Task 10).

Two tiers:

- ``/api/admin/semantic-models`` and ``/api/admin/semantic-sources`` are
  ``require_admin`` — creating/editing/deleting the canonical Ossie
  documents and configuring where they sync from is an admin action.
- ``/api/semantic-models/{slug}.yaml`` (export) and
  ``/api/semantic-models/search`` are any-authenticated-user, gated instead
  on the linked Data Package's grant (``data_package_semantic_models``) —
  a model rides the same visibility as the package(s) it belongs to, the
  same way a table's visibility rides its package membership
  (``can_access_table``). A model with no linked package is reachable by
  admins only, since there is no package grant to check.

Ownership rule (the only enforcement point for it, since no UI ships in
this task): a model whose ``source`` is not ``'manual'`` was written by a
sync (``import_source``) and refuses edits with 409 ``source_owned`` —
the next sync would silently revert an edit made here, so editing at the
source is the only way to make a change stick.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from src.repositories import semantic_model_repo, semantic_source_repo
from src.semantic.document_validation import validate_document

router = APIRouter(tags=["semantic-models"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SemanticModelCreate(BaseModel):
    document: str
    description: Optional[str] = None


class SemanticModelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SemanticSourceCreate(BaseModel):
    kind: str
    name: str
    adapter: str = "native"
    config: dict = {}
    enabled: bool = True


class SemanticSourceUpdate(BaseModel):
    name: Optional[str] = None
    adapter: Optional[str] = None
    config: Optional[dict] = None
    enabled: Optional[bool] = None


_VALID_KINDS = ("git", "upload", "connection")


# ---------------------------------------------------------------------------
# Access helper — the linked-package grant gate shared by export + search
# ---------------------------------------------------------------------------


def _can_read_model(user: dict, model_row: dict, conn: duckdb.DuckDBPyConnection) -> bool:
    """True iff ``user`` may read ``model_row``: admin, a grant on any Data
    Package the model is linked to, or a direct grant on the model itself.

    The direct grant layers UNDER the package path the way per-table grants
    layer under the package stack. It is not decoration: ``ResourceType
    .SEMANTIC_MODEL`` is registered, so ``/admin/access`` offers it as a
    grantable resource — and a control an admin can set but nothing ever reads
    is worse than no control at all, because it reports success while granting
    nothing.
    """
    from app.auth.access import can_access, is_user_admin

    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        return False
    if is_user_admin(user_id, conn):
        return True
    if can_access(user_id, ResourceType.SEMANTIC_MODEL.value, model_row["id"], conn):
        return True
    package_ids = semantic_model_repo().list_packages_for_model(model_row["id"])
    return any(can_access(user_id, ResourceType.DATA_PACKAGE.value, pkg_id, conn) for pkg_id in package_ids)


def _export_denied_message(slug: str) -> str:
    return (
        f"Semantic model '{slug}' is not linked to a Data Package you have access to. "
        "Ask an admin to link it to a Data Package you have access to, or grant you one it already belongs to."
    )


def _resolve_model(model_ref: str) -> Optional[dict]:
    """Accept either a model id or its slug — ids are opaque
    (``<source>/<source_ref>/<slug>``), so a slug is the friendlier handle
    for an interactive admin."""
    repo = semantic_model_repo()
    return repo.get(model_ref) or repo.get_by_slug(model_ref)


# ---------------------------------------------------------------------------
# Admin: semantic-models CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-models")
async def list_semantic_models(
    source: Optional[str] = None,
    source_ref: Optional[str] = None,
    user: dict = Depends(require_admin),
):
    """List every stored semantic model (any status), admin-only."""
    return semantic_model_repo().list_all(source=source, source_ref=source_ref)


@router.post("/api/admin/semantic-models", status_code=201)
async def create_semantic_model(
    body: SemanticModelCreate,
    user: dict = Depends(require_admin),
):
    """Create (or replace) a hand-authored (``source='manual'``) model from
    a pasted Ossie document. Invalid input 422s with the schema errors —
    never stored half-valid."""
    result = validate_document(body.document)
    if not result.ok:
        raise HTTPException(status_code=422, detail={"errors": result.errors})

    models = (result.parsed or {}).get("semantic_model") or []
    slug = models[0].get("name") if models else None
    if not slug:
        raise HTTPException(
            status_code=422,
            detail={"errors": ["Document declares no semantic_model entry with a name"]},
        )

    content_hash = hashlib.sha256(body.document.encode()).hexdigest()
    row = semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=body.description,
        document=body.document,
        document_json=result.parsed,
        spec_version=result.spec_version,
        content_hash=content_hash,
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(timezone.utc),
    )
    return row


@router.get("/api/admin/semantic-models/{model_id:path}")
async def get_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    return row


@router.put("/api/admin/semantic-models/{model_id:path}")
async def update_semantic_model(model_id: str, body: SemanticModelUpdate, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if row["source"] != "manual":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_owned",
                "message": (
                    f"this model is owned by source '{row['source']}'"
                    + (f" (source_ref={row['source_ref']!r})" if row.get("source_ref") else "")
                    + " — edit it there, then re-sync, rather than here"
                ),
            },
        )
    updated = semantic_model_repo().upsert(
        id=row["id"],
        slug=row["slug"],
        name=body.name if body.name is not None else row["name"],
        description=body.description if body.description is not None else row["description"],
        document=row["document"],
        document_json=row["document_json"],
        spec_version=row["spec_version"],
        content_hash=row["content_hash"],
        source=row["source"],
        source_ref=row["source_ref"],
        status=row["status"],
        validation_errors=row["validation_errors"],
        validated_at=row["validated_at"],
    )
    return updated


@router.delete("/api/admin/semantic-models/{model_id:path}", status_code=204)
async def delete_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    semantic_model_repo().delete(row["id"])


# ---------------------------------------------------------------------------
# Admin: semantic-sources CRUD + sync
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-sources")
async def list_semantic_sources(enabled_only: bool = False, user: dict = Depends(require_admin)):
    return semantic_source_repo().list_all(enabled_only=enabled_only)


@router.post("/api/admin/semantic-sources", status_code=201)
async def create_semantic_source(body: SemanticSourceCreate, user: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if body.kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {body.kind!r} (expected one of {', '.join(_VALID_KINDS)})",
        )
    from uuid import uuid4

    source_id = f"ss_{uuid4().hex[:12]}"
    return semantic_source_repo().create(
        id=source_id,
        kind=body.kind,
        name=body.name.strip(),
        adapter=body.adapter,
        config=body.config,
        enabled=body.enabled,
    )


@router.get("/api/admin/semantic-sources/{source_id}")
async def get_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    row = semantic_source_repo().get(source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    return row


@router.put("/api/admin/semantic-sources/{source_id}")
async def update_semantic_source(source_id: str, body: SemanticSourceUpdate, user: dict = Depends(require_admin)):
    repo = semantic_source_repo()
    if repo.get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return repo.get(source_id)
    return repo.update(source_id, **fields)


@router.delete("/api/admin/semantic-sources/{source_id}", status_code=204)
async def delete_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    if not semantic_source_repo().delete(source_id):
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")


@router.post("/api/admin/semantic-sources/{source_id}/sync")
async def sync_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    if semantic_source_repo().get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")

    from dataclasses import asdict

    from src.semantic.transports import import_source

    try:
        report = import_source(source_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
    return asdict(report)


# ---------------------------------------------------------------------------
# Public: search + export, gated on the linked Data Package's grant
# ---------------------------------------------------------------------------


@router.get("/api/semantic-models/search")
async def search_semantic_models(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Case-insensitive substring search over slug/name/description, RBAC
    filtered to models linked to a Data Package the caller can access
    (admins see everything)."""
    needle = q.lower()
    matches: list[dict[str, Any]] = []
    for row in semantic_model_repo().list_all():
        haystack = " ".join(filter(None, [row.get("slug"), row.get("name"), row.get("description")])).lower()
        if needle not in haystack:
            continue
        if not _can_read_model(user, row, conn):
            continue
        matches.append(row)
        if len(matches) >= limit:
            break
    return {"query": q, "models": matches, "count": len(matches)}


@router.get("/api/semantic-models/{slug}.yaml")
async def export_semantic_model(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Export the stored document byte-for-byte — never re-serialized, so
    comments and key order survive."""
    row = semantic_model_repo().get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")
    if not _can_read_model(user, row, conn):
        raise HTTPException(status_code=403, detail=_export_denied_message(slug))
    return Response(content=row["document"], media_type="text/yaml")
