"""Data download endpoint — streaming parquet files."""

import logging
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.identifier_validation import _SAFE_QUOTED_IDENTIFIER
from src.rbac import can_access_table

from src.repositories import (
    audit_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])

# A single path segment of a partitioned-table part relpath. Allows hive
# partition dirs (``month=2026-06``), flat partition keys (``2025_11.parquet``)
# and ``data.parquet`` — first char alnum/underscore, then alnum/_/./=/-.
# Deliberately excludes ``/`` and ``\`` (segments are split on ``/`` first).
_SAFE_PART_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.=-]*$")


def _resolve_part_path(extracts_dir: Path, table_id: str, part: str) -> Path | None:
    """Map a manifest ``part`` relpath to a safe on-disk file under the
    table's data directory, or ``None`` if it is unsafe / not found.

    Partitioned tables live at ``extracts/<source>/data/<table_id>/<part>``
    (Jira hive ``month=*/data.parquet``, Keboola flat ``<key>.parquet``).
    Path traversal is blocked three ways: reject absolute / ``\\`` / NUL /
    empty / ``.``|``..`` segments up front; require every segment to match
    ``_SAFE_PART_SEGMENT``; and finally assert the resolved path stays under
    the (resolved) table dir. Only an existing regular file is returned.
    """
    if not part or part.startswith("/") or "\\" in part or "\x00" in part:
        return None
    segments = part.split("/")
    if any(s in ("", ".", "..") for s in segments):
        return None
    if not all(_SAFE_PART_SEGMENT.match(s) for s in segments):
        return None
    if not extracts_dir.exists():
        return None
    for table_dir in extracts_dir.glob(f"*/data/{table_id}"):
        if not table_dir.is_dir():
            continue
        base = table_dir.resolve()
        candidate = (base / part).resolve()
        if (candidate == base or base in candidate.parents) and candidate.is_file():
            return candidate
    return None


# Distribution allowlist. A parquet may leave the server ONLY for these
# query modes — the same pair the manifest treats as downloadable. Stated as an
# allowlist rather than a denylist so a query mode added later is undistributable
# until someone decides otherwise, which is the safe direction for a gate that
# releases raw bytes.
_DISTRIBUTABLE_QUERY_MODES = frozenset({"local", "materialized"})


def _distribution_refusal(table_id: str) -> Optional[HTTPException]:
    """Raise 403 unless ``table_id`` is a table Agnes actually distributes.

    ``server_only`` and ``query_mode`` were **client-side advice**: `agnes pull`
    honours them (`cli/lib/pull.py`), but nothing on the server did, so the
    parquet of a table flagged "never leaves the server" was one authenticated
    GET away. On Caddy deployments the gap is wider than the handler below —
    `forward_auth` calls ``check-access`` and then `file_server` streams the
    file, so the app never sees the download at all. That is why this runs in
    ``check-access`` too, and why fixing that endpoint is what actually closes
    the fast path.

    This is **not** an authorization check and does not honour admin god-mode:
    "this table is not distributed" is a property of the table, true for every
    caller, exactly as the manifest reports it to every caller. Callers run
    ``can_access_table`` first, so an unauthorized caller still gets the RBAC
    403 and learns nothing about distribution from this one.

    A table absent from the registry is left alone — the caller's own
    existence handling (404) owns that case.

    The manifest ORs this with a per-user "granted but not subscribed" flag
    (`app/api/sync.py`); that half is deliberately NOT mirrored here. It is a
    stack-subscription property, not a table property: such a caller passes
    `can_access_table`, so what they skipped is subscribing to the package,
    not the authorization. Mirroring it would turn this into a second RBAC
    decision on top of the one that already ran, with two places to keep in
    agreement. (Devin Review on #1265 asked; this is the answer.)
    """
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    # id-or-name: the download path is reached by either (master views are named
    # by `name`, grants key on `id`), so resolve the same way the rest of the
    # read surface does rather than assuming one of them.
    row = repo.get(table_id) or repo.get_by_name(table_id)
    if row is None:
        return None
    if row.get("server_only"):
        return HTTPException(
            status_code=403,
            detail=(
                f"table '{table_id}' is server_only — it is kept fresh on the server and "
                "not distributed; query it with `agnes query` instead of downloading it"
            ),
        )
    # A blank/NULL `query_mode` means `local`, because that is what every other
    # surface makes of it: the manifest (`app/api/sync.py:1629`) and the
    # distribution mirror (`app/worker/kinds.py`) both fall back to "local", so
    # such a table IS advertised as downloadable. Refusing it here would leave
    # it permanently listed and permanently un-fetchable. (Devin Review on
    # #1265.) The allowlist keeps its job for every mode that is actually set.
    mode = (row.get("query_mode") or "").strip().lower() or "local"
    if mode not in _DISTRIBUTABLE_QUERY_MODES:
        return HTTPException(
            status_code=403,
            detail=(
                f"table '{table_id}' has query_mode='{mode}' and is not distributed; "
                "query it with `agnes query` instead of downloading it"
            ),
        )
    return None


def _assert_distributable(table_id: str) -> None:
    """``_distribution_refusal``, raised. For callers that audit afterwards."""
    refusal = _distribution_refusal(table_id)
    if refusal is not None:
        raise refusal


@router.get("/{table_id}/check-access")
async def check_access(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Lightweight RBAC probe used by Caddy's ``forward_auth`` directive
    to gate file_server-served parquet downloads without involving the
    app's request workers in the bulk byte transfer.

    Returns HTTP 204 No Content when the caller has read access to
    ``table_id``; HTTP 403 (via ``can_access_table`` returning False)
    otherwise. Caddy treats 2xx as authorized and forwards the request
    to its own ``file_server`` block; non-2xx is returned to the client
    verbatim.

    Why a separate endpoint and not just ``HEAD /download``: ``HEAD`` on
    the FileResponse-based ``download`` handler still opens the file and
    runs stat() to populate Content-Length / ETag. ``forward_auth`` calls
    this endpoint on every request, so the per-call cost matters; a pure
    RBAC check is ~1 ms while a HEAD path involves filesystem walks
    (``rglob`` for the parquet across source subdirs).
    """
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    if not _SAFE_QUOTED_IDENTIFIER.match(table_id):
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="data.access_check",
                resource=resource,
                params={
                    "granted": False,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "error": "invalid_table_id",
                },
                result="error.404",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for data.access_check (invalid id); continuing")
        raise HTTPException(status_code=404, detail="Table not found")
    granted = can_access_table(user, table_id, conn)
    # Authorized — but is this table distributed at all? On Caddy this probe is
    # the ONLY hook before file_server streams the parquet, so the check has to
    # live here and not only in the download handler below. Decided BEFORE the
    # audit write: this probe answers 204 or 403, and an audit trail that
    # records a refused probe as a granted success is worse than no record —
    # an operator reading it sees an access check that never happened that way.
    # (Devin Review on #1265.)
    try:
        refusal = _distribution_refusal(table_id) if granted else None
    except HTTPException:
        raise
    except Exception:
        # The registry read is a second DB touch after `can_access_table`, so a
        # failure here means state went away mid-request. Answering 204 would
        # release bytes this gate has not cleared; a 500 would tell the caller
        # nothing. Say what happened and let them retry. (Devin Review on
        # #1265.)
        logger.exception("data.access_check: distribution check failed for %s", table_id)
        refusal = HTTPException(
            status_code=503,
            detail={
                "code": "distribution_check_unavailable",
                "hint": "Could not read the table registry to check whether this table is distributed. Retry.",
            },
        )
    params = {
        "granted": granted,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    if refusal is not None:
        params["refused"] = "not_distributed"
    try:
        audit_repo().log(
            user_id=identity_for_audit(user)[0],
            action="data.access_check",
            resource=resource,
            params=params,
            result="success" if granted and refusal is None else "error.403",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for data.access_check; continuing")
    if not granted:
        from src.rbac import table_not_in_stack_message

        raise HTTPException(
            status_code=403,
            detail=table_not_in_stack_message(table_id),
        )
    if refusal is not None:
        raise refusal
    return Response(status_code=204)


@router.get("/{table_id}/download")
async def download_table(
    table_id: str,
    request: Request,
    part: str | None = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a parquet file for download. Supports ETag for caching.

    On Caddy-fronted deployments the matching Caddyfile rule intercepts
    ``GET /api/data/{table_id}/download``, calls ``check-access`` via
    ``forward_auth``, and serves the parquet directly via ``file_server``
    — bypassing this handler entirely. This handler stays as the
    canonical fallback for non-Caddy deployments (dev `docker compose
    up`, alternative reverse proxies, direct :8000 access) where the
    bulk transfer goes through uvicorn.
    """
    # Reject unsafe table_id before any filesystem or DB operations.
    # Use the relaxed quoted-identifier check that allows dots and hyphens
    # (Keboola table IDs like "in.c-crm.orders") while still blocking
    # path-traversal characters (/, .., \) and quote/control chars.
    if not _SAFE_QUOTED_IDENTIFIER.match(table_id):
        raise HTTPException(status_code=404, detail="Table not found")
    # Check access FIRST
    if not can_access_table(user, table_id, conn):
        from src.rbac import table_not_in_stack_message

        raise HTTPException(
            status_code=403,
            detail=table_not_in_stack_message(table_id),
        )
    # ...then whether the table is distributed at all. Order matters: an
    # unauthorized caller gets the RBAC refusal and learns nothing else.
    try:
        _assert_distributable(table_id)
    except HTTPException:
        raise
    except Exception:
        # Same reasoning as `check-access`: a registry read that fails leaves
        # this gate uncleared, and the bytes stay put until it can be read.
        # (Devin Review on #1265.)
        logger.exception("data.download: distribution check failed for %s", table_id)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "distribution_check_unavailable",
                "hint": "Could not read the table registry to check whether this table is distributed. Retry.",
            },
        )

    data_dir = _get_data_dir()
    extracts_dir = data_dir / "extracts"

    if part is not None:
        # Partitioned-table part download (partitioned distribution): serve
        # one part file under the table's data dir. `_resolve_part_path`
        # blocks traversal and confirms the file exists.
        file_path = _resolve_part_path(extracts_dir, table_id, part)
        if file_path is None:
            raise HTTPException(status_code=404, detail="Part not found")
    else:
        # Single-file table: v2 extract.duckdb path, then legacy layout.
        candidates = list(extracts_dir.rglob(f"data/{table_id}.parquet")) if extracts_dir.exists() else []
        if not candidates:
            parquet_dir = data_dir / "src_data" / "parquet"
            candidates = list(parquet_dir.rglob(f"{table_id}.parquet"))
            if not candidates:
                candidates = list(parquet_dir.rglob(f"*/{table_id}.parquet"))
        if not candidates:
            raise HTTPException(status_code=404, detail=f"Table '{table_id}' not found")
        file_path = candidates[0]

    # ETag support
    stat = file_path.stat()
    etag = f'"{stat.st_mtime_ns}"'
    if_none_match = request.headers.get("if-none-match")
    if if_none_match == etag:
        return Response(status_code=304)

    audit_params = {"bytes": stat.st_size, "format": "parquet"}
    if part is not None:
        audit_params["part"] = part
    try:
        audit_repo().log(
            user_id=identity_for_audit(user)[0],
            action="data.download",
            resource=f"table:{table_id}"[:256],
            params=audit_params,
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for data.download; continuing")

    return FileResponse(
        path=file_path,
        filename=file_path.name if part is not None else f"{table_id}.parquet",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )
