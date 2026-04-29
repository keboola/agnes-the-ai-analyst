"""Self-service auth diagnostic page.

Behind the ``AGNES_DEBUG_AUTH=true`` env flag (default off → 404). Lets a
logged-in user inspect their own session: decoded JWT claims, group
memberships with sources, resource grants, and what Google Workspace would
return on a fresh sync (dry-run, no DB writes).

Hard rules — designed so even if the env flag accidentally lands in
production, no sensitive material leaks:

- Never render the raw JWT, only its claims + a short sha256 fingerprint
  (so it can be correlated against logs without being replayable).
- Never render password hashes, full PAT tokens, or session cookie values.
- Self-only — the user_id comes from the validated session, not a query
  parameter or path param. There is no admin-views-anyone surface here.
- Refetch-from-Google is dry-run: returns a diff of what the next real
  sync would do, but performs zero ``user_group_members`` writes.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import _get_db, get_current_user
from app.auth.jwt import verify_token

logger = logging.getLogger(__name__)

# Mounted at /me/debug. The prefix is intentionally short so the navbar
# link and the bookmarkable URL stay readable.
router = APIRouter(prefix="/me/debug", tags=["me-debug"])

templates = Jinja2Templates(directory="app/web/templates")


def is_debug_auth_enabled() -> bool:
    """True iff the env flag is one of the accepted truthy spellings.

    Default off — production VMs leave the var unset, the page returns
    404, and no debug surface exists. Dev/staging VMs set it to ``true``
    in their .env (provisioned via the agnes-vm Terraform module).
    """
    return os.environ.get("AGNES_DEBUG_AUTH", "").strip().lower() in (
        "1", "true", "yes",
    )


async def require_debug_auth_enabled() -> None:
    """Dependency: 404 unless the env flag is on. Returning 404 instead of
    403 makes the route's existence undetectable in production — an
    attacker scanning for diag endpoints can't distinguish "you're not
    allowed" from "this Agnes doesn't ship the debug feature"."""
    if not is_debug_auth_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


def _token_fingerprint(token: Optional[str]) -> Optional[str]:
    """Short sha256 of the raw token, for log correlation.

    The full hash isn't a credential (HMAC-SHA256 is one-way) but truncating
    to 12 hex chars makes the displayed value visually distinct from the
    raw token so screenshots can't accidentally leak the JWT.
    """
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _read_session_token(request: Request) -> Optional[str]:
    """The session JWT lives in the ``access_token`` cookie (set by every
    auth provider's callback). Authorization-header bearers are PATs and
    are out of scope for this diagnostic — the page is for interactive
    sessions."""
    return request.cookies.get("access_token")


def _decoded_claims(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return verified JWT claims (or ``None`` if missing/invalid).

    Goes through the project's :func:`app.auth.jwt.verify_token` so an
    expired or mis-signed token produces ``None`` rather than a partial
    decode — same trust boundary the rest of the auth path uses.
    """
    if not token:
        return None
    return verify_token(token)


def _user_memberships(
    user_id: str, conn: duckdb.DuckDBPyConnection
) -> List[Dict[str, Any]]:
    """Group memberships for the given user, with source labels and the
    bound external_id (NULL for unbound groups). Sorted by group name so
    the output is stable across reloads."""
    # external_id is the v14 column. Tolerate its absence — the same
    # template that ships in the v13-base PR #2 must also work on a v14
    # install where the column exists.
    has_ext = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user_groups' AND column_name = 'external_id'"
    ).fetchone()
    select_ext = "g.external_id" if has_ext else "NULL"
    rows = conn.execute(
        f"""SELECT g.id, g.name, g.is_system, {select_ext} AS external_id,
                   m.source, m.added_at, m.added_by
              FROM user_group_members m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = ?
             ORDER BY g.name""",
        [user_id],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r)) for r in rows]


def _accessible_grants(
    user_id: str, conn: duckdb.DuckDBPyConnection
) -> List[Dict[str, Any]]:
    """Resource grants the user can reach via at least one of their groups.
    Distinct on (resource_type, resource_id) so a grant held by two of the
    user's groups appears once.

    The plain ``SELECT DISTINCT`` covers all SELECT-list columns, so listing
    ``via_group`` would re-double a grant reachable through two groups (and
    inflate the "Distinct N grant(s)" count rendered by ``me_debug.html``).
    DuckDB supports PostgreSQL's ``DISTINCT ON`` to dedupe on the leading
    columns; the ORDER BY picks the alphabetically-first group as the
    representative ``via_group`` for the row.
    """
    rows = conn.execute(
        """SELECT DISTINCT ON (rg.resource_type, rg.resource_id)
                  rg.resource_type, rg.resource_id, g.name AS via_group
             FROM resource_grants rg
             JOIN user_group_members m ON m.group_id = rg.group_id
             JOIN user_groups g ON g.id = rg.group_id
            WHERE m.user_id = ?
            ORDER BY rg.resource_type, rg.resource_id, g.name""",
        [user_id],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r)) for r in rows]


def _last_sync_summary(
    user_id: str, conn: duckdb.DuckDBPyConnection
) -> Dict[str, Any]:
    """Summary of the most recent google_sync run for this user, drawn from
    user_group_members. Not authoritative timestamps (Google sync writes
    DELETE+INSERT every login, so all rows share the same added_at), but
    sufficient to answer "when did Agnes last hear from Google about me?"."""
    row = conn.execute(
        """SELECT COUNT(*) AS n, MAX(added_at) AS last_at
             FROM user_group_members
            WHERE user_id = ? AND source = 'google_sync'""",
        [user_id],
    ).fetchone()
    n, last_at = row if row else (0, None)
    return {
        "google_sync_count": int(n or 0),
        "last_added_at": str(last_at) if last_at else None,
    }


# ---------------------------------------------------------------------------
# GET /me/debug  — render the diagnostic page
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse, name="me_debug_page")
async def me_debug_page(
    request: Request,
    _: None = Depends(require_debug_auth_enabled),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Reuse the project's shared template-context builder so config /
    # static_url / session / theme overrides are populated the same way
    # every other HTML page gets them. Adding a debug page must not bypass
    # the shared chrome.
    from app.web.router import _build_context
    raw_token = _read_session_token(request)
    # Strip sensitive columns before handing the row to the template. The
    # current me_debug.html only renders id/email/name/active/created_at, but
    # passing the full row would let a future template edit (e.g. an admin
    # adding `{{ user_record | tojson }}` while debugging) accidentally leak
    # the password hash. Defense-in-depth — the module docstring at line 13
    # explicitly establishes "Never render password hashes" as an invariant.
    _SENSITIVE_USER_COLUMNS = (
        "password_hash", "setup_token", "reset_token",
    )
    user_record_safe = {
        k: v for k, v in user.items() if k not in _SENSITIVE_USER_COLUMNS
    }
    ctx = _build_context(
        request, user=user_record_safe,
        user_record=user_record_safe,
        claims=_decoded_claims(raw_token),
        token_fingerprint=_token_fingerprint(raw_token),
        memberships=_user_memberships(user["id"], conn),
        grants=_accessible_grants(user["id"], conn),
        sync_summary=_last_sync_summary(user["id"], conn),
        google_group_prefix=os.environ.get(
            "AGNES_GOOGLE_GROUP_PREFIX", ""
        ).strip(),
    )
    return templates.TemplateResponse(request, "me_debug.html", ctx)


# ---------------------------------------------------------------------------
# POST /me/debug/refetch-groups  — dry-run live Google fetch
# ---------------------------------------------------------------------------


@router.post("/refetch-groups", name="me_debug_refetch_groups")
async def me_debug_refetch_groups(
    _: None = Depends(require_debug_auth_enabled),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-issue ``fetch_user_groups`` for the current user and return a
    diff against the cached ``user_group_members`` snapshot, *without*
    writing anything. The "real" sync runs only at OAuth callback —
    forcing a write here would let any logged-in user trigger a Google
    Admin SDK call on demand, which is both noisy and a quota footgun.
    """
    from app.auth.group_sync import fetch_user_groups

    fetched = fetch_user_groups(user["email"])
    # The function returns Optional[list] on the v14 branch and List[str]
    # on earlier branches. Normalize either shape: ``None`` becomes an
    # explicit soft-fail marker and a list passes through untouched.
    soft_failed = fetched is None
    fetched_list: List[str] = list(fetched) if fetched else []

    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    if prefix:
        relevant = [g.lower() for g in fetched_list if g.lower().startswith(prefix)]
    else:
        relevant = [g.lower() for g in fetched_list]

    # Current state — google_sync rows joined to user_groups for the
    # external_id label (NULL on pre-v14 schemas; tolerate that).
    has_ext = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user_groups' AND column_name = 'external_id'"
    ).fetchone()
    select_ext = "g.external_id" if has_ext else "NULL"
    current_rows = conn.execute(
        f"""SELECT g.name, {select_ext} AS external_id
              FROM user_group_members m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = ? AND m.source = 'google_sync'
             ORDER BY g.name""",
        [user["id"]],
    ).fetchall()
    current_external_ids = {
        r[1].lower() for r in current_rows if r[1]
    }
    current_names = [r[0] for r in current_rows]

    # Diff: prefix-relevant emails that have no matching external_id row
    # (would be added) and current external_ids no longer in fetched set
    # (would be removed).
    fetched_set = set(relevant)
    would_add = sorted(fetched_set - current_external_ids)
    would_remove = sorted(current_external_ids - fetched_set) if has_ext else []

    return {
        "soft_failed": soft_failed,
        "prefix": prefix or None,
        "fetched": fetched_list,
        "fetched_relevant": relevant,
        "current_names": current_names,
        "current_external_ids": sorted(current_external_ids),
        "would_add": would_add,
        "would_remove": would_remove,
        "applied": False,  # always — this endpoint never writes
    }
