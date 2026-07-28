"""Admin REST endpoints for the news_template entity.

The single news entity (intro + content) is versioned in `news_template`;
this module is the HTTP surface for admin authoring. Web rendering of
the published version happens in `app.web.router.news_page` and the
`/home` handler — those don't go through this API.

Every endpoint is admin-gated; no analyst-side reads here. The /preview
endpoint runs the sanitizer without persisting so the admin UI can show
a live render before saving.
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories import (
    audit_repo,
    news_template_repo,
)
from src.repositories.news_template import (
    AlreadyDraftError,
    NoDraftError,
    NotFoundError,
    VersionConflictError,
)
from src.sanitize_news import sanitize


router = APIRouter(prefix="/api/admin/news", tags=["news"])


class NewsBody(BaseModel):
    intro: str = ""
    content: str = ""


def _serialize(row: dict | None) -> dict | None:
    """Convert datetime fields to ISO strings for JSON. Pydantic does this
    automatically when fields are typed; we use a free-form dict here so
    do it by hand."""
    if row is None:
        return None
    out = dict(row)
    for k in ("created_at", "updated_at", "published_at"):
        v = out.get(k)
        if v is not None and hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


# -- read endpoints -----------------------------------------------------


@router.get("/current", dependencies=[Depends(require_admin)])
def get_current(conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    """Latest published version (or {published: false} if none)."""
    row = news_template_repo().get_current_published()
    if row is None:
        return {"published": False}
    return _serialize(row)


@router.get("/draft", dependencies=[Depends(require_admin)])
def get_draft(conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    """Active draft. 404 if none — UI shows 'create new draft' button."""
    row = news_template_repo().get_active_draft()
    if row is None:
        raise HTTPException(status_code=404, detail="no_draft")
    return _serialize(row)


@router.get("/versions", dependencies=[Depends(require_admin)])
def list_versions(
    limit: int = 50,
    offset: int = 0,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = news_template_repo().list_versions(limit=limit, offset=offset)
    return {"versions": [_serialize(r) for r in rows]}


@router.get("/versions/{version}", dependencies=[Depends(require_admin)])
def get_version(
    version: int,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = news_template_repo().get_version(version)
    if row is None:
        raise HTTPException(status_code=404, detail="version_not_found")
    return _serialize(row)


# -- write endpoints ----------------------------------------------------


@router.put("/draft", dependencies=[Depends(require_admin)])
def put_draft(
    body: NewsBody,
    expected_version: int | None = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Upsert the active draft. Sanitizes both fields BEFORE writing.

    Optimistic-lock: when `expected_version` is supplied (query string),
    the request fails with 409 unless the active draft is at that
    version. Pass `expected_version=0` when you intend to create the
    first draft and want the call to fail if another admin already
    started one.
    """
    repo = news_template_repo()
    try:
        row = repo.save_draft(
            intro=body.intro,
            content=body.content,
            by=user["email"],
            expected_version=expected_version,
        )
    except VersionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "expected": e.expected,
                "actual": e.actual,
                "actual_by": e.actual_by,
            },
        ) from e
    audit_repo().log(
        user_id=user["id"],
        action="news_draft_saved",
        params={"version": row["version"], "by": user["email"]},
        result="ok",
    )
    return _serialize(row)


@router.post("/publish", dependencies=[Depends(require_admin)])
def post_publish(
    expected_version: int | None = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Publish the active draft.

    When `expected_version` is supplied (query string), the request
    fails with 409 unless the active draft is at that version. Use
    this when reviewing a specific draft before flipping it live so
    a concurrent admin's edit doesn't slip through under your name.
    """
    repo = news_template_repo()
    try:
        row = repo.publish_draft(by=user["email"], expected_version=expected_version)
    except NoDraftError as e:
        raise HTTPException(status_code=409, detail="no_draft") from e
    except VersionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "expected": e.expected,
                "actual": e.actual,
                "actual_by": e.actual_by,
            },
        ) from e
    audit_repo().log(
        user_id=user["id"],
        action="news_published",
        params={"version": row["version"], "by": user["email"]},
        result="ok",
    )
    return _serialize(row)


@router.post("/unpublish/{version}", dependencies=[Depends(require_admin)])
def post_unpublish(
    version: int,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = news_template_repo()
    try:
        row = repo.unpublish(version=version, by=user["email"])
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail="version_not_found") from e
    except AlreadyDraftError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit_repo().log(
        user_id=user["id"],
        action="news_unpublished",
        params={"version": row["version"], "by": user["email"]},
        result="ok",
    )
    return _serialize(row)


@router.post("/preview", dependencies=[Depends(require_admin)])
def post_preview(body: NewsBody):
    """Sanitize candidate intro + content and return — no DB writes."""
    return {"intro": sanitize(body.intro), "content": sanitize(body.content)}
