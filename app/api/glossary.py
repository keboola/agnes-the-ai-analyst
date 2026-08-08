"""Glossary API endpoints — read/search over glossary_terms.

Read/search tier mirrors /api/metrics and /api/knowledge/search: any
authenticated user, no per-resource grant. Write access is Keboola-sync-only
(connectors/keboola/semantic_layer.py) in this iteration — there is no
admin-authored manual-entry endpoint yet.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import get_current_user
from app.markdown_render import render_plain, stores_html
from src.repositories import glossary_repo

router = APIRouter(tags=["glossary"])


def with_definition_text(term: dict) -> dict:
    """``term`` plus a ``definition_text`` plain-text projection.

    ``glossary_terms.definition`` is the sibling of
    ``metric_definitions.description`` and has the same two dialects, written
    by the same importer in the same pass (``connectors/keboola/
    semantic_layer.py`` builds both rows and stamps both with
    ``source='keboola_semantic_layer'``). Every surface that renders a
    definition — the Glossary tab, ``agnes glossary`` — therefore needs the
    same flattening, keyed the same way (see
    :func:`app.markdown_render.stores_html`).

    The stored column travels unchanged alongside it.
    """
    return {**term, "definition_text": render_plain(term.get("definition"), html_source=stores_html(term))}


@router.get("/api/glossary")
async def list_glossary_terms(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    """List glossary terms, ordered by term."""
    repo = glossary_repo()
    terms = [with_definition_text(t) for t in repo.list(limit=limit)]
    return {"terms": terms, "count": len(terms)}


@router.get("/api/glossary/search")
async def search_glossary_terms(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Relevance-ranked search across term + definition (BM25, ILIKE fallback)."""
    repo = glossary_repo()
    terms = [with_definition_text(t) for t in repo.search(q, limit=limit)]
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
    return with_definition_text(term)
