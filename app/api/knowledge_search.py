"""Unified knowledge search endpoint (K2, #797).

REST surface for ``src.search.unified.unified_search``: resolves the caller's
grant sets fail-closed (collection grants, memory-domain grants + audience
groups, ``can_access_table``) and fans the query out over Collections chunks,
knowledge items, and table catalog cards. See the module docstring in
``src/search/unified.py`` for merge semantics.
"""

from __future__ import annotations

import logging

import duckdb
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import _get_db, get_current_user
from src.rbac import can_access_table
from src.repositories import table_registry_repo
from src.search.unified import unified_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


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
    from app.api.memory import _caller_granted_memory_domains, _effective_groups

    corpus_ids = _accessible_corpus_ids(user)
    groups = _effective_groups(user, conn)
    domains = _caller_granted_memory_domains(user, conn)
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
