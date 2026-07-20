"""Unified knowledge search endpoint (K2, #797).

REST surface for ``src.search.unified.unified_search``: resolves the caller's
grant sets fail-closed (collection grants, memory-domain grants + audience
groups, ``can_access_table``) and fans the query out over Collections chunks,
knowledge items, and table catalog cards. See the module docstring in
``src/search/unified.py`` for merge semantics.

Grant resolution goes through the repository factories (``is_user_admin``,
``user_group_members_repo``, ``resource_grants_repo``) rather than
``app.api.memory``'s raw-conn helpers, so the endpoint filters correctly on
the Postgres backend too — the raw ``Depends(_get_db)`` connection reads
empty state tables there (the backend-split bug class).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.auth.access import can_access, can_access_session, is_user_admin, require_resource_access
from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import SessionPrincipal
from app.resource_types import ResourceType
from src.audit_helpers import client_kind_from_user
from src.rbac import get_accessible_tables
from src.repositories import audit_repo, resource_grants_repo, table_registry_repo, user_group_members_repo
from src.search.unified import unified_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _resolve_knowledge_grants(user) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """(user_groups, granted_domains) for the knowledge source, factory-routed.

    Semantics mirror ``app.api.memory._effective_groups`` /
    ``_caller_granted_memory_domains``: ``None``/``None`` for privileged
    viewers (no filter), ``group:<name>`` audience tokens + granted
    ``memory_domains.id`` values otherwise, ``[]``/``[]`` fail-closed for a
    caller with no memberships. SessionPrincipal co-sessions never get admin
    god-mode — their domain set comes from the session intersection.
    """
    if isinstance(user, SessionPrincipal):
        return [], list(user.intersection.get(ResourceType.MEMORY_DOMAIN.value, frozenset()))
    user_id = user.get("id")
    if not user_id:
        return [], []
    if is_user_admin(user_id):
        return None, None
    memberships = user_group_members_repo().list_groups_with_meta_for_user(user_id)
    groups = [f"group:{m['name']}" for m in memberships]
    group_ids = [m["group_id"] for m in memberships]
    grants = resource_grants_repo().list_for_groups(group_ids, resource_type="memory_domain") if group_ids else []
    domains = sorted({g["resource_id"] for g in grants})
    return groups, domains


@router.get("/search")
async def knowledge_search(
    q: str = Query(..., min_length=1, description="Search query"),
    k: int = Query(10, ge=1, le=50),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """One query across documents, the knowledge base, and the table catalog.

    Results are typed (``chunk | knowledge | table``); table hits carry a
    pivot hint (query via SQL) instead of rows. Everything is filtered to the
    caller's grants, fail-closed per source.

    ``retrieval`` (``hybrid | lexical_only``) labels the chunk engine's mode:
    ``lexical_only`` means the embeddings extra is absent and document chunks
    were ranked without semantic scoring (#898). Knowledge and table hits are
    lexical by design and unaffected.
    """
    from app.api.collections import _accessible_corpus_ids
    from src.ingest.retrieval import retrieval_mode

    corpus_ids = _accessible_corpus_ids(user)
    groups, domains = _resolve_knowledge_grants(user)
    # Resolve the caller's accessible table-id set ONCE per request instead of
    # calling `can_access_table` per row (FAI-132 N+1 collapse: ~115 stack
    # resolutions -> 1). `None` means admin/all, mirroring `can_access_table`.
    _accessible_ids = get_accessible_tables(user, conn)
    allowed = None if _accessible_ids is None else set(_accessible_ids)
    tables = [t for t in table_registry_repo().list_all() if allowed is None or t["id"] in allowed]

    results = unified_search(
        q,
        corpus_ids=corpus_ids,
        user_groups=groups,
        granted_domains=domains,
        tables=tables,
        k=k,
    )
    return {"query": q, "results": results, "retrieval": retrieval_mode()}


@router.get("/artifacts/{corpus_id}/download")
async def download_knowledge_artifact(
    corpus_id: str,
    request: Request,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{corpus_id}")),
):
    """Stream a per-collection knowledge.duckdb artifact (K3, #798).

    Consumed by ``agnes pull``; the PAT is the only credential. RBAC =
    collection grants via ``require_resource_access``. ETag mirrors
    ``/api/data/{table_id}/download``.
    """
    from src.knowledge_packaging import artifacts_dir

    path = artifacts_dir() / f"{corpus_id}.duckdb"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact not built yet")

    stat = path.stat()
    etag = f'"{stat.st_mtime_ns}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    try:
        audit_repo().log(
            user_id=user.get("id") if isinstance(user, dict) else None,
            action="knowledge.artifact_download",
            resource=f"collection:{corpus_id}"[:256],
            params={"bytes": stat.st_size},
            result="success",
            client_kind=client_kind_from_user(user) if isinstance(user, dict) else "web",
        )
    except Exception:
        logger.exception("audit_log write failed for knowledge.artifact_download; continuing")

    return FileResponse(
        path=path,
        filename=f"{corpus_id}.duckdb",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )


def _caller_can_read_digest(user, digest_id: str) -> bool:
    """Fail-closed ``ResourceType.KNOWLEDGE_DIGEST`` grant check (K4, #799).

    Shared by the manifest section builder
    (``app.api.sync._build_knowledge_artifacts_section``, lazy-imported to
    avoid a cycle) and this module — it's the same ``can_access`` /
    ``can_access_session`` predicate that
    ``require_resource_access(ResourceType.KNOWLEDGE_DIGEST, ...)`` gates the
    content endpoint below with, exposed as a plain boolean so the manifest
    builder can filter a whole list without raising per row. Admin
    short-circuits via ``can_access``; ``SessionPrincipal`` co-session
    callers route through ``can_access_session`` instead — the
    ``_accessible_corpus_ids`` idiom (``app/api/collections.py``).
    """
    if isinstance(user, SessionPrincipal):
        return can_access_session(user, ResourceType.KNOWLEDGE_DIGEST.value, digest_id)
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        return False
    return can_access(user_id, ResourceType.KNOWLEDGE_DIGEST.value, digest_id)


@router.get("/digests/{digest_id}/content")
async def get_knowledge_digest_content(
    digest_id: str,
    user=Depends(require_resource_access(ResourceType.KNOWLEDGE_DIGEST, "{digest_id}")),
):
    """Serve one maintained digest's markdown (K4, #799).

    Consumed by ``agnes pull`` (writes ``.claude/rules/ka_<slug>.md``); the
    PAT is the only credential. RBAC gate is
    ``require_resource_access(ResourceType.KNOWLEDGE_DIGEST, ...)`` — the
    same house style the sibling ``download_knowledge_artifact`` endpoint
    above uses, so an ungranted caller on a real digest id sees **403**
    (matching ``test_download_ungranted_analyst_403``'s posture; supersedes
    the K4 plan's original 404-only guess — see the PR description). Unknown
    id or a digest that has never generated (``pending``, empty
    ``output_md``) is **404** for an admin, who always clears the gate — no
    existence leak beyond "granted but nothing here", the same posture as
    ``test_download_granted_corpus_no_artifact_built_404``. Staleness
    (status + reason) travels in the body so the client can render a
    visible banner — never a silent stale digest.
    """
    from src.repositories import knowledge_digests_repo

    d = knowledge_digests_repo().get(digest_id)
    if d is None or not (d.get("output_md") or "").strip():
        raise HTTPException(status_code=404, detail="Digest not found")

    try:
        audit_repo().log(
            user_id=user.get("id") if isinstance(user, dict) else None,
            action="knowledge.digest_download",
            resource=f"knowledge_digest:{digest_id}"[:256],
            params={"slug": d.get("slug")},
            result="success",
            client_kind=client_kind_from_user(user) if isinstance(user, dict) else "web",
        )
    except Exception:
        logger.exception("audit_log write failed for knowledge.digest_download; continuing")

    generated_at = d.get("generated_at")
    return {
        "id": d["id"],
        "slug": d["slug"],
        "title": d["title"],
        "output_md": d["output_md"],
        "status": d.get("status") or "pending",
        "status_reason": d.get("status_reason"),
        "generated_at": generated_at.isoformat() if generated_at else None,
    }
