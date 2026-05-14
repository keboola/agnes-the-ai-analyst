"""Session-diagnostic data-assembly helpers for the /me/profile troubleshooting section.

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
from typing import Any, Dict, Optional

import duckdb
from fastapi import HTTPException, Request

from app.auth.jwt import verify_token

logger = logging.getLogger(__name__)


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
