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

from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import SessionPrincipal
from src.audit_helpers import client_kind_from_user
from src.rbac import can_access_table
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
        from app.resource_types import ResourceType

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
    """
    from app.api.collections import _accessible_corpus_ids

    corpus_ids = _accessible_corpus_ids(user)
    groups, domains = _resolve_knowledge_grants(user)
    tables = [t for t in table_registry_repo().list_all() if can_access_table(user, t["id"], conn)]

    results = unified_search(
        q,
        corpus_ids=corpus_ids,
        user_groups=groups,
        granted_domains=domains,
        tables=tables,
        k=k,
    )
    return {"query": q, "results": results}


@router.get("/artifacts/{corpus_id}/download")
async def download_knowledge_artifact(
    corpus_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    """Stream a per-collection knowledge.duckdb artifact (K3, #798).

    Consumed by ``agnes pull``; the PAT is the only credential. RBAC =
    collection grants, fail-closed: ungranted or unknown corpus, or a
    granted corpus whose artifact isn't built yet, all return 404 (no
    existence leak). ETag mirrors ``/api/data/{table_id}/download``.
    """
    from app.api.collections import _accessible_corpus_ids
    from src.knowledge_packaging import artifacts_dir

    if corpus_id not in set(_accessible_corpus_ids(user)):
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = artifacts_dir() / f"{corpus_id}.duckdb"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact not built yet")

    stat = path.stat()
    etag = f'"{stat.st_mtime_ns}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="knowledge.artifact_download",
            resource=f"collection:{corpus_id}"[:256],
            params={"bytes": stat.st_size},
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for knowledge.artifact_download; continuing")

    return FileResponse(
        path=path,
        filename=f"{corpus_id}.duckdb",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )
