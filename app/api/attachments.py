"""Attachment download endpoint — streams connector-catalogued binaries.

A connector that stores attachment files on the server declares its
catalogue in ``src/attachment_sources.py`` (table, id column, path column,
permitted root); this route serves any declared source. Jira is the first:
the ``attachments`` catalogue's ``local_path`` records where
``JiraService.download_all_attachments()`` put each file.

RBAC is table-level and deliberately so: "can this caller read attachment
X" reduces to "can this caller read the table that catalogues X"
(``can_access_table``), the same gate as the parquet download route. Agnes
has no row-level entitlement model — do not invent one here. A catalogue
table marked ``server_only`` keeps its attachment binaries on the server
just as it keeps its parquet; the query-mode allowlist half of that
route's distribution gate is parquet-distribution business and
deliberately does not transfer (attachments are lazy per-id fetches, not
manifest sync).

Misses must stay distinguishable from denials: a catalogued attachment can
have no bytes on the server (over-50MB skip, transform-time miss, file
removed since), and a client needs to tell that 404 apart from a 403 so it
can fall back to the upstream system's own API for exactly those cases.
"""

import logging
import os
import stat as stat_module
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

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


def _lookup_stored_path(
    decl: AttachmentSource, view_name: str, attachment_id: str
) -> tuple[bool, str | None, str | None]:
    """Look ``attachment_id`` up in the source's catalogue view.

    Returns ``(row_exists, stored_path, original_name)`` —
    ``original_name`` comes from the declaration's ``filename_column`` (the
    name the upstream system shows, e.g. Jira's ``filename``) and is ``None``
    when the source declares none. The id column is compared as VARCHAR so a
    BIGINT-typed catalogue column never throws on a non-numeric path
    parameter. A missing view (source declared but never synced) reads as
    "no row".
    """
    cols = quote_ident(decl.path_column)
    if decl.filename_column:
        cols += f", {quote_ident(decl.filename_column)}"
    sql = (
        f"SELECT {cols} FROM {quote_ident(view_name)} WHERE CAST({quote_ident(decl.id_column)} AS VARCHAR) = ? LIMIT 1"
    )
    analytics = get_analytics_db_readonly()
    try:
        row = analytics.execute(sql, [attachment_id]).fetchone()
    except duckdb.CatalogException:
        return False, None, None
    finally:
        analytics.close()
    if row is None:
        return False, None, None
    return True, row[0], (row[1] if decl.filename_column else None)


def _open_contained(root: Path, stored: str | None) -> tuple[BinaryIO | None, os.stat_result | None, str]:
    """Open the catalogue's path value as a safe regular file under ``root``.

    The value is data read from a table, not a trusted constant — so it is
    contained even though the connector wrote it: reject backslashes, NUL
    and ``..`` segments up front, then require the resolved path to stay
    under the resolved root (the guard pattern of ``_resolve_part_path`` in
    ``app/api/data.py``). Returns ``(open file, fstat of that descriptor,
    "")`` on success — fstat of the OPEN descriptor, so the size the
    response advertises is of exactly the inode being streamed; a concurrent
    re-publish of the same path cannot make Content-Length disagree with the
    body — or ``(None, None, reason)`` with reason ``no_path_recorded`` /
    ``path_rejected`` / ``file_missing`` for the audit trail. The caller
    owns closing the file.
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
        fh = resolved.open("rb")
    except OSError:
        return None, None, "file_missing"
    st = os.fstat(fh.fileno())
    if not stat_module.S_ISREG(st.st_mode):
        fh.close()
        return None, None, "file_missing"
    return fh, st, ""


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

    # The declaration names the catalogue table; grants key on the registry
    # `id` while master views are named by `name`, and the two are not
    # guaranteed to coincide (see app/api/data.py — it resolves the same
    # way). Resolve the registry row once and use each half for its own job;
    # an unregistered (or unreadable-registry) table falls back to the
    # declared name for both, which then fails closed in `can_access_table`.
    rbac_key = view_name = decl.table
    reg_row = None
    try:
        from src.repositories import table_registry_repo

        reg_row = table_registry_repo().get(decl.table) or table_registry_repo().get_by_name(decl.table)
        if reg_row is not None:
            rbac_key = reg_row["id"]
            view_name = reg_row["name"]
    except Exception:
        logger.exception("attachment.download: registry resolution failed for %s; using declared name", decl.table)

    # RBAC first, before any lookup or filesystem work — an unauthorized
    # caller learns nothing beyond the refusal. Written as `denied`, not an
    # error: that is the audit read layer's class for correct policy
    # refusals (src/audit_helpers.py), and a 403 here is never a
    # malfunction, so it must not inflate the Activity Center error bucket.
    if not can_access_table(user, rbac_key, conn):
        _audit(user, source, attachment_id, "denied", table=rbac_key)
        raise HTTPException(status_code=403, detail=table_not_in_stack_message(rbac_key))

    # `server_only` is the admin's "these bytes do not leave the server"
    # lever; the parquet download honours it (`_distribution_refusal`,
    # app/api/data.py) and the route releasing the SAME table's attachment
    # binaries must not be the way around it. Like there, this is not an
    # authorization check — it is a property of the table, true for every
    # caller, admin god-mode included — which is why it runs after RBAC, so
    # an unauthorized caller still learns nothing beyond its own 403. The
    # query-mode allowlist half of that gate is parquet-distribution
    # business and deliberately does not transfer: attachments are lazy
    # per-id fetches, not manifest sync.
    if reg_row is not None and bool(reg_row.get("server_only")):
        _audit(user, source, attachment_id, "denied", error="attachment_table_server_only", table=rbac_key)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "attachment_table_server_only",
                "hint": (
                    "the catalogue table is marked server_only — its attachments "
                    "stay on the server just as its parquet does"
                ),
            },
        )

    row_exists, stored, original_name = _lookup_stored_path(decl, view_name, attachment_id)
    if not row_exists:
        _audit(user, source, attachment_id, "error.404", error="attachment_not_found")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_found",
                "hint": f"no row with {decl.id_column}={attachment_id!r} in {view_name}",
            },
        )

    fh, st, miss_reason = _open_contained(decl.root(), stored)
    if fh is None or st is None:
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

    # Label the download with the name the upstream system shows (Jira
    # stores files as "<id>_<filename>", so the path basename is id-prefixed).
    # The catalogue value is data, not a trusted constant — basename-only,
    # after the same unsafe-byte rejects the path guard applies.
    download_name = Path(fh.name).name
    if original_name and "\x00" not in original_name and "\\" not in original_name:
        download_name = Path(original_name).name or download_name

    _audit(user, source, attachment_id, "success", bytes=st.st_size, filename=download_name)

    # Same Content-Disposition shapes FileResponse would emit (the CLI
    # parses both): plain when the name survives percent-quoting unchanged,
    # RFC 5987 extended otherwise.
    quoted = quote(download_name)
    disposition = (
        f'attachment; filename="{download_name}"'
        if quoted == download_name
        else f"attachment; filename*=utf-8''{quoted}"
    )

    def _stream(f: BinaryIO = fh):
        # Streamed from the descriptor opened above: Content-Length comes
        # from fstat of that same descriptor, and the connector publishes
        # rewrites atomically (os.replace), so an overlapping refresh keeps
        # serving the complete old file rather than a torn one.
        try:
            while chunk := f.read(1 << 20):
                yield chunk
        finally:
            f.close()

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": disposition,
        },
    )
