"""Collections API — file corpus CRUD + multipart upload (Slice 2).

Endpoints:

  POST   /api/collections                         require_admin
  GET    /api/collections                         auth (RBAC-filtered list)
  GET    /api/collections/{collection_id}         require_resource_access(COLLECTION, "{collection_id}")
  DELETE /api/collections/{collection_id}         require_admin
  POST   /api/collections/{collection_id}/files   require_resource_access(COLLECTION, "{collection_id}")
  GET    /api/collections/{collection_id}/files   require_resource_access(COLLECTION, "{collection_id}")
  DELETE /api/collections/{collection_id}/files/{file_id}
                                                  require_resource_access(COLLECTION, "{collection_id}")

RBAC model: collection **create/delete** = admin-only; file **upload/list/delete**
and collection **read** = any user whose groups hold an explicit
``resource_grants`` row for ``(collection, <collection_id>)``. Admins
short-circuit every grant check.

Fail-closed: the GET list returns only collections the caller can access;
unknown collections on entity-scoped endpoints return 404 (not 403) so callers
cannot probe for existence of collections they are not granted.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth.access import (
    can_access,
    can_access_session,
    require_admin,
    require_resource_access,
)
from app.auth.dependencies import get_current_user
from app.auth.session_principal import SessionPrincipal
from app.resource_types import ResourceType
from src.corpus_allowlist import classify
from src.file_storage import delete_corpus_file, store_corpus_file
from src.repositories import (
    corpus_chunks_repo,
    corpus_files_repo,
    file_corpora_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collections", tags=["collections"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _auto_slug(name: str) -> str:
    """Generate a URL-safe slug from a collection name.

    Falls back to ``"collection"`` for names with no alphanumerics (e.g. "!!!"),
    which would otherwise yield an empty slug (degenerate ``/library/`` URL +
    spurious 409 collisions on the second such name).

    The trailing ``strip("-")`` runs *after* the ``[:100]`` cap: truncation can
    re-expose a hyphen at the boundary (a long name whose 100th char lands on a
    word separator), so we strip once more to keep the stored slug clean.
    """
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "collection"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _collection_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "created_by": row["created_by"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def _file_out(row: dict) -> dict:
    return {
        "file_id": row["id"],
        "corpus_id": row["corpus_id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "file_type": row["file_type"],
        "size_bytes": row["size_bytes"],
        "processing_status": row["processing_status"],
        "processing_detail": row.get("processing_detail"),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_collection(
    payload: CreateCollectionRequest,
    user: dict = Depends(require_admin),
):
    """Create a new file corpus (admin only).

    Returns the created collection object (id, slug, name, …).
    ``slug`` is auto-generated from ``name`` when omitted, and an explicit
    ``slug`` is normalised to a URL-safe form (``[a-z0-9-]``) so it always
    resolves via ``/library/{slug}``; a collision on the unique slug index
    returns **409**.
    """
    # Always normalise through _auto_slug so the stored slug is URL-safe
    # ([a-z0-9-]) and reachable via /library/{slug}, whether it was admin-
    # provided or derived from the name. An explicit slug like "my/collection"
    # becomes "my-collection"; a whitespace-only or all-symbol slug collapses to
    # empty and falls back to the name (then _auto_slug's "collection" default).
    slug = _auto_slug(payload.slug) if (payload.slug or "").strip() else _auto_slug(payload.name)
    repo = file_corpora_repo()
    try:
        corpus_id = repo.create(
            name=payload.name,
            slug=slug,
            description=payload.description,
            created_by=user["id"],
        )
    except Exception as exc:
        # DuckDB raises ConstraintException; PG raises IntegrityError.
        # Both contain "slug" in the message for a UNIQUE collision.
        err = str(exc).lower()
        if "unique" in err or "duplicate" in err or "constraint" in err:
            raise HTTPException(
                status_code=409,
                detail=f"collection_slug_conflict:{slug}",
            ) from exc
        raise

    row = repo.get(corpus_id)
    logger.info("collection created id=%s slug=%s by=%s", corpus_id, slug, user.get("email"))
    return _collection_out(row)


def _accessible_corpus_ids(user) -> list[str]:
    """The collection ids the caller may access (fail-closed).

    Admins are waved through by ``can_access`` (Admin-group short-circuit);
    non-admins get only granted collections; ``SessionPrincipal`` co-session
    callers route through ``can_access_session``. Goes through the repository
    factory (no raw DuckDB conn) → correct on the Postgres backend.
    """
    rows = file_corpora_repo().list()
    if isinstance(user, SessionPrincipal):
        return [r["id"] for r in rows if can_access_session(user, ResourceType.COLLECTION.value, r["id"])]
    uid = user["id"]
    return [r["id"] for r in rows if can_access(uid, ResourceType.COLLECTION.value, r["id"])]


@router.get("")
async def list_collections(
    user=Depends(get_current_user),
):
    """List collections accessible to the caller (fail-closed)."""
    allowed = set(_accessible_corpus_ids(user))
    rows = [r for r in file_corpora_repo().list() if r["id"] in allowed]
    return {"items": [_collection_out(r) for r in rows]}


@router.get("/search")
async def search_collections(
    q: str,
    k: int = 10,
    corpus_id: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Hybrid search across the caller's accessible collections.

    Fail-closed: only the caller's granted collections are searched; an
    optional ``corpus_id`` narrows to one (ignored if not accessible). Declared
    before ``/{collection_id}`` so ``search`` isn't captured as a collection id.
    """
    from src.ingest.retrieval import search as _search

    allowed = _accessible_corpus_ids(user)
    if corpus_id is not None:
        allowed = [c for c in allowed if c == corpus_id]
    k = max(1, min(k, 50))
    return {"results": _search(allowed, q, k=k)}


@router.get("/{collection_id}")
async def get_collection(
    collection_id: str,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{collection_id}")),
):
    """Return a collection's metadata + file list.

    Requires the caller to hold a grant on this collection (admins exempt).
    Returns **404** (not 403) when the collection does not exist, so that
    unprivileged callers cannot probe for existence via the error code
    difference.
    """
    row = file_corpora_repo().get(collection_id)
    if not row:
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(collection_id)
    return {**_collection_out(row), "files": [_file_out(f) for f in files]}


def _purge_derived_tabular_rows(corpus_id: str) -> None:
    """Remove derived table_registry rows + parquet files for a corpus.

    Called synchronously from both ``delete_file`` (single-file variant, by
    table_id) and ``delete_collection`` (corpus-wide variant). After removing
    registry rows we call ``orchestrator.rebuild_source`` so the master views
    in ``analytics.duckdb`` no longer expose the deleted table(s). Best-effort:
    a rebuild failure is logged but not raised — the durable artefacts (registry
    + parquet) are already gone.
    """

    from src.db import _get_data_dir
    from src.orchestrator import SyncOrchestrator

    deleted_ids = table_registry_repo().delete_for_corpus(corpus_id)
    if not deleted_ids:
        return

    source_name = f"collection_{corpus_id}"
    data_dir = _get_data_dir() / "extracts" / source_name / "data"
    ext_db = _get_data_dir() / "extracts" / source_name / "extract.duckdb"

    # Remove parquet files and drop views from extract.duckdb.
    for table_id in deleted_ids:
        parquet = data_dir / f"{table_id}.parquet"
        if parquet.exists():
            try:
                parquet.unlink()
            except OSError as exc:
                logger.warning("could not remove parquet %s: %s", parquet, exc)

    # Drop the views from extract.duckdb (best-effort — DB may not exist yet
    # if the file was never ingested, e.g. processing_status='rejected').
    if ext_db.exists():
        try:
            from src.duckdb_conn import _open_duckdb

            ec = _open_duckdb(str(ext_db))
            try:
                for table_id in deleted_ids:
                    safe_name = table_id.replace('"', '""')
                    ec.execute(f'DROP VIEW IF EXISTS "{safe_name}"')
                    ec.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
            finally:
                ec.close()
        except Exception as exc:
            logger.warning("could not clean extract.duckdb for %s: %s", source_name, exc)

    # Rebuild master views so the deleted tables are no longer queryable.
    try:
        SyncOrchestrator().rebuild_source(source_name)
    except Exception as exc:
        logger.warning("rebuild_source(%s) after derived-table purge failed: %s", source_name, exc)


def _purge_derived_tabular_row_for_file(corpus_id: str, file_id: str) -> None:
    """Variant of ``_purge_derived_tabular_rows`` for a single file deletion.

    The table_id encoding is defined in ``src/ingest/tabular.py``::

        fid_suffix = file_id.replace("cf_", "")[:8]
        table_id = f"collection_{corpus_id}_{base}_{fid_suffix}"

    Rather than re-derive the base from the filename (fragile), we query the
    registry directly for the row whose ``source_table`` ends with the
    fid_suffix, which is a unique-enough discriminator for a given corpus.
    """
    fid_suffix = file_id.replace("cf_", "")[:8]
    source_name = f"collection_{corpus_id}"
    rows = table_registry_repo().list_by_source("collection")
    matching = [r for r in rows if r.get("bucket") == corpus_id and r.get("id", "").endswith(fid_suffix)]
    if not matching:
        return  # non-tabular file or not yet indexed — nothing to purge
    for row in matching:
        table_registry_repo().unregister(row["id"])

    from src.db import _get_data_dir
    from src.orchestrator import SyncOrchestrator

    data_dir = _get_data_dir() / "extracts" / source_name / "data"
    ext_db = _get_data_dir() / "extracts" / source_name / "extract.duckdb"

    for row in matching:
        table_id = row["id"]
        parquet = data_dir / f"{table_id}.parquet"
        if parquet.exists():
            try:
                parquet.unlink()
            except OSError as exc:
                logger.warning("could not remove parquet %s: %s", parquet, exc)

    if ext_db.exists():
        try:
            from src.duckdb_conn import _open_duckdb

            ec = _open_duckdb(str(ext_db))
            try:
                for row in matching:
                    table_id = row["id"]
                    safe_name = table_id.replace('"', '""')
                    ec.execute(f'DROP VIEW IF EXISTS "{safe_name}"')
                    ec.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
            finally:
                ec.close()
        except Exception as exc:
            logger.warning("could not clean extract.duckdb for %s: %s", source_name, exc)

    try:
        SyncOrchestrator().rebuild_source(source_name)
    except Exception as exc:
        logger.warning("rebuild_source(%s) after single-file purge failed: %s", source_name, exc)


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: str,
    user: dict = Depends(require_admin),
):
    """Soft-delete a collection (admin only).

    Sets ``deleted_at``; the collection becomes invisible on GET list and
    returns 404 on entity-scoped reads. Derived table_registry rows, parquets,
    and extract.duckdb views are purged synchronously (they are regenerable from
    the uploaded files; soft-delete of the collection is treated as hard-delete
    for the derived rows).
    """
    row = file_corpora_repo().get(collection_id)
    if not row:
        raise HTTPException(status_code=404, detail="collection_not_found")
    _purge_derived_tabular_rows(collection_id)
    file_corpora_repo().soft_delete(collection_id)
    logger.info("collection deleted id=%s by=%s", collection_id, user.get("email"))


# ---------------------------------------------------------------------------
# File upload / list / delete
# ---------------------------------------------------------------------------


@router.post("/{collection_id}/files", status_code=201)
async def upload_files(
    collection_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{collection_id}")),
):
    """Upload one or more files into a collection.

    Each file passes through the extension allowlist:

    * **tier1** (txt, pdf, docx, …) → content-addressed write +
      ``processing_status='pending'``.
    * **tier2** (png, jpg, tiff, …) → same write + ``'pending'``
      (vision/OCR ingestion deferred to Slice 5).
    * **unsupported** (.dwg, .exe, …) → stored raw +
      ``processing_status='rejected'`` with ``processing_detail`` describing
      the reason. The *whole response* returns **422** when any file is
      rejected (all results are still returned so the caller sees which
      files succeeded and which were rejected).

    Returns a list of ``{file_id, filename, processing_status, …}`` for every
    uploaded file (in upload order).
    """
    # Verify the collection exists (grant check already done by the dependency).
    corpus = file_corpora_repo().get(collection_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="collection_not_found")

    cf_repo = corpus_files_repo()
    results = []
    any_rejected = False
    _to_ingest: List[str] = []

    for upload in files:
        fname = upload.filename or "unknown"
        tier = classify(fname)

        if tier is None:
            # Unsupported type — store raw bytes but record as rejected.
            # Per spec: we do store the bytes (content-addressed, same path)
            # and write a corpus_files row with status='rejected'.
            try:
                stored = await store_corpus_file(collection_id, fname, upload)
                storage_path = stored.storage_path
                sha = stored.sha256
                size = stored.size_bytes
                ext = stored.ext.lstrip(".")
            except HTTPException:
                # Oversize or empty — still record as rejected with no path.
                storage_path = None
                sha = ""
                size = 0
                ext = fname.rsplit(".", 1)[-1] if "." in fname else ""

            file_id = cf_repo.add(
                corpus_id=collection_id,
                filename=fname,
                sha256=sha,
                file_type=ext or None,
                size_bytes=size or None,
                storage_path=storage_path,
            )
            cf_repo.set_status(
                file_id,
                status="rejected",
                detail={"reason": "unsupported_type", "filename": fname},
            )
            row = cf_repo.get(file_id)
            results.append(_file_out(row))
            any_rejected = True

        else:
            # tier1 or tier2 — store and mark pending.
            try:
                stored = await store_corpus_file(collection_id, fname, upload)
            except HTTPException as exc:
                # Size cap or empty — treat as rejected so the rest of the
                # batch still processes.
                file_id = cf_repo.add(
                    corpus_id=collection_id,
                    filename=fname,
                    sha256="",
                    file_type=None,
                    size_bytes=None,
                    storage_path=None,
                )
                cf_repo.set_status(
                    file_id,
                    status="rejected",
                    detail={"reason": f"storage_error:{exc.detail}"},
                )
                row = cf_repo.get(file_id)
                results.append(_file_out(row))
                any_rejected = True
                continue

            file_id = cf_repo.add(
                corpus_id=collection_id,
                filename=fname,
                sha256=stored.sha256,
                file_type=stored.ext.lstrip(".") or None,
                size_bytes=stored.size_bytes,
                storage_path=stored.storage_path,
            )
            # Default status is 'pending' (set by the repo on insert).
            row = cf_repo.get(file_id)
            results.append(_file_out(row))
            _to_ingest.append(file_id)
            logger.info(
                "corpus_file uploaded collection=%s file_id=%s sha=%s tier=%s",
                collection_id,
                file_id,
                stored.sha256[:12],
                tier,
            )

    # Kick off Tier-1 ingestion in the background (tabular → registered DuckDB
    # table; documents → chunks). Rejected/unsupported files are not scheduled.
    from src.ingest.runner import ingest_file

    for fid in _to_ingest:
        background_tasks.add_task(ingest_file, fid)

    if any_rejected:
        # Return 422 with full result list so clients know which files
        # succeeded and which were rejected.
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content=results)

    return results


@router.get("/{collection_id}/files")
async def list_files(
    collection_id: str,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{collection_id}")),
):
    """List all files in a collection (all processing statuses)."""
    corpus = file_corpora_repo().get(collection_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(collection_id)
    return {"files": [_file_out(f) for f in files]}


@router.delete("/{collection_id}/files/{file_id}", status_code=204)
async def delete_file(
    collection_id: str,
    file_id: str,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{collection_id}")),
):
    """Delete a file from a collection.

    Removes the blob from disk (best-effort) and the ``corpus_files`` row.
    """
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")
    if row.get("storage_path"):
        delete_corpus_file(row["storage_path"])
    # Delete the file's chunks first — otherwise they linger and still surface
    # in search results (with a null filename once the file row is gone).
    corpus_chunks_repo().delete_for_file(file_id)
    # Hard-delete the corpus_files row — no soft-delete on individual files.
    cf_repo.delete(file_id)
    # Remove derived table_registry row + parquet if this was a tabular file.
    _purge_derived_tabular_row_for_file(collection_id, file_id)
    logger.info(
        "corpus_file deleted file_id=%s collection=%s by=%s",
        file_id,
        collection_id,
        user.get("id") if isinstance(user, dict) else "?",
    )
