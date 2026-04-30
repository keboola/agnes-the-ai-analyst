"""Audit log read API — surfaces ``audit_log`` rows for the /admin/audit
admin console. Read-only; the only writers are the per-module ``_audit()``
helpers in users.py / marketplaces.py / scripts.py / metrics.py / sync.py
/ upload.py / admin.py / permissions.py / access_requests.py / metadata.py
/ catalog.py / memory.py.

Filters: ``user`` (user_id exact), ``action`` (exact), ``action_prefix``
(``LIKE prefix%``), ``resource`` (exact), ``limit`` (clamped 1–500).

Admin-only — audit rows include error messages and identifiers operators
may not want surfaced to ordinary users.
"""

from __future__ import annotations

from typing import Optional

import duckdb
from fastapi import APIRouter, Depends

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.audit import AuditRepository

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def query_audit_log(
    limit: int = 100,
    user: Optional[str] = None,
    action: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource: Optional[str] = None,
    actor: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return ``audit_log`` rows newest-first, with optional filters.

    Response shape: ``[{id, timestamp, user_id, action, resource, params,
    result, duration_ms}, ...]`` — same row shape ``AuditRepository.query``
    returns; ``timestamp`` serialized as ISO string for JSON.
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    repo = AuditRepository(conn)
    rows = repo.query(
        user_id=user,
        action=action,
        action_prefix=action_prefix,
        resource=resource,
        limit=limit,
    )
    out = []
    for r in rows:
        item = dict(r)
        ts = item.get("timestamp")
        if ts is not None:
            item["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        out.append(item)
    return out


@router.get("/actions")
async def list_audit_actions(
    actor: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List the distinct ``action`` values currently present in the
    audit log. Powers the action filter dropdown on /admin/audit."""
    rows = conn.execute(
        "SELECT DISTINCT action FROM audit_log "
        "WHERE action IS NOT NULL AND action != '' "
        "ORDER BY action"
    ).fetchall()
    return [r[0] for r in rows]
