"""Glossary API endpoints — read/search over glossary_terms.

Read/search tier mirrors /api/metrics and /api/knowledge/search: any
authenticated user, no per-resource grant. Write access is Keboola-sync-only
(connectors/keboola/semantic_layer.py) in this iteration — there is no
admin-authored manual-entry endpoint yet.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import get_current_user
from src.repositories import glossary_repo

router = APIRouter(tags=["glossary"])


@router.get("/api/glossary")
async def list_glossary_terms(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    """List glossary terms, ordered by term."""
    repo = glossary_repo()
    terms = repo.list(limit=limit)
    return {"terms": terms, "count": len(terms)}


@router.get("/api/glossary/search")
async def search_glossary_terms(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Relevance-ranked search across term + definition (BM25, ILIKE fallback)."""
    repo = glossary_repo()
    terms = repo.search(q, limit=limit)
    return {"query": q, "terms": terms, "count": len(terms)}


@router.get("/api/glossary/{glossary_id:path}")
async def get_glossary_term(
    glossary_id: str,
    user: dict = Depends(get_current_user),
):
    """Get a single glossary term by ID."""
    repo = glossary_repo()
    term = repo.get(glossary_id)
    if term is None:
        raise HTTPException(status_code=404, detail=f"Glossary term '{glossary_id}' not found")
    return term
