"""Attachment download endpoint — streams connector-catalogued binaries.

A connector that stores attachment files on the server declares its
catalogue in ``src/attachment_sources.py`` (table, id column, path column,
permitted root); this route serves any declared source. Jira is the first:
``jira_attachments.local_path`` records where
``JiraService.download_all_attachments()`` put each file.

RBAC is table-level and deliberately so: "can this caller read attachment
X" reduces to "can this caller read the table that catalogues X"
(``can_access_table``), exactly like the parquet download route. Agnes has
no row-level entitlement model — do not invent one here.

Misses must stay distinguishable from denials: a catalogued attachment can
have no bytes on the server (over-50MB skip, transform-time miss, file
removed since), and a client needs to tell that 404 apart from a 403 so it
can fall back to the upstream system's own API for exactly those cases.
"""

import logging
import os
import stat as stat_module
from pathlib import Path

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth.dependencies import _get_db, get_current_user
from src.attachment_sources import AttachmentSource, get_attachment_source, list_attachment_sources
from src.audit_helpers import client_kind_from_user, identity_for_audit, log_safe
from src.db import get_analytics_db_readonly
from src.rbac import can_access_table, table_not_in_stack_message
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


def _audit(user: dict, source: str, attachment_id: str, result: str, **params) -> None:
    """Audit one download attempt, granted or denied — same shape as
    ``data.download``; ``log_safe`` guarantees a failed audit write never
    fails the request itself."""
    log_safe(
        user_id=identity_for_audit(user)[0],
        action="attachment.download",
        resource=f"attachment:{source}/{attachment_id}"[:256],
        params={"source": source, **params},
        result=result,
        client_kind=client_kind_from_user(user),
    )


def _lookup_stored_path(decl: AttachmentSource, attachment_id: str) -> tuple[bool, str | None]:
    """Look ``attachment_id`` up in the source's catalogue view.

    Returns ``(row_exists, stored_path)``. The id column is compared as
    VARCHAR so a BIGINT-typed catalogue column never throws on a
    non-numeric path parameter. A missing view (source declared but never
    synced) reads as "no row".
    """
    sql = (
        f"SELECT {quote_ident(decl.path_column)} FROM {quote_ident(decl.table)} "
        f"WHERE CAST({quote_ident(decl.id_column)} AS VARCHAR) = ? LIMIT 1"
    )
    analytics = get_analytics_db_readonly()
    try:
        row = analytics.execute(sql, [attachment_id]).fetchone()
    except duckdb.CatalogException:
        return False, None
    finally:
        analytics.close()
    if row is None:
        return False, None
    return True, row[0]


def _resolve_contained(root: Path, stored: str | None) -> tuple[Path | None, os.stat_result | None, str]:
    """Map the catalogue's path value to a safe regular file under ``root``.

    The value is data read from a table, not a trusted constant — so it is
    contained even though the connector wrote it: reject backslashes, NUL
    and ``..`` segments up front, then require the resolved path to stay
    under the resolved root (the guard pattern of ``_resolve_part_path`` in
    ``app/api/data.py``). Returns ``(path, stat_result, "")`` on success —
    the single ``stat()`` this endpoint performs, reused for the audit byte
    count and the FileResponse — or ``(None, None, reason)`` with reason
    ``no_path_recorded`` / ``path_rejected`` / ``file_missing`` for the
    audit trail.
    """
    if not stored:
        return None, None, "no_path_recorded"
    if "\\" in stored or "\x00" in stored:
        return None, None, "path_rejected"
    candidate = Path(stored)
    if ".." in candidate.parts:
        return None, None, "path_rejected"
    base = root.resolve()
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    except OSError:
        return None, None, "path_rejected"
    if not resolved.is_relative_to(base):
        logger.warning("attachment catalogue path escapes the permitted root and was not served: %r", stored)
        return None, None, "path_rejected"
    try:
        st = resolved.stat()
    except OSError:
        return None, None, "file_missing"
    if not stat_module.S_ISREG(st.st_mode):
        return None, None, "file_missing"
    return resolved, st, ""


# Deliberately sync (`def`, not `async def`): everything here blocks —
# `can_access_table`, the analytics lookup (which re-ATTACHes every extract
# and may refresh remote-extension credentials), stat(), and the audit
# write. FastAPI runs sync handlers on the anyio threadpool; declared
# `async`, that same work would hold the single uvicorn event loop and stall
# every other in-flight request (see app/api/mcp_per_table.py for the same
# choice, documented).
@router.get("/{source}/{attachment_id}/download")
def download_attachment(
    source: str,
    attachment_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream one connector-catalogued attachment binary.

    Outcomes a client can tell apart:

    - 404 ``unknown_attachment_source`` — ``{source}`` has no declaration
      (decided before any catalogue or filesystem work).
    - 403 — caller lacks read access to the source's catalogue table.
    - 404 ``attachment_not_found`` — no catalogue row with this id.
    - 404 ``attachment_not_stored`` — the row exists but the server holds
      no bytes (over-size skip, transform-time miss, or removed since);
      fall back to the upstream system's own API for these.
    - 200 — the bytes, with the original filename in Content-Disposition.
    """
    decl = get_attachment_source(source)
    if decl is None:
        _audit(user, source, attachment_id, "error.404", error="unknown_attachment_source")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_attachment_source",
                "hint": f"declared sources: {', '.join(list_attachment_sources()) or '(none)'}",
            },
        )

    # RBAC first, before any lookup or filesystem work — an unauthorized
    # caller learns nothing beyond the refusal.
    if not can_access_table(user, decl.table, conn):
        _audit(user, source, attachment_id, "error.403", table=decl.table)
        raise HTTPException(status_code=403, detail=table_not_in_stack_message(decl.table))

    row_exists, stored = _lookup_stored_path(decl, attachment_id)
    if not row_exists:
        _audit(user, source, attachment_id, "error.404", error="attachment_not_found")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_found",
                "hint": f"no row with {decl.id_column}={attachment_id!r} in {decl.table}",
            },
        )

    file_path, st, miss_reason = _resolve_contained(decl.root(), stored)
    if file_path is None or st is None:
        _audit(user, source, attachment_id, "error.404", error="attachment_not_stored", reason=miss_reason)
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_stored",
                "hint": (
                    "the catalogue row exists but the server holds no bytes for it "
                    "(over-size skip, transform-time miss, or removed since); "
                    "fetch it from the upstream system directly"
                ),
            },
        )

    _audit(user, source, attachment_id, "success", bytes=st.st_size, filename=file_path.name)
    return FileResponse(
        path=file_path,
        stat_result=st,
        filename=file_path.name,
        media_type="application/octet-stream",
    )
