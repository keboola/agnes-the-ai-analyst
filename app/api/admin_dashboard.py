"""GET /api/admin/dashboard/signals — the `/admin` dashboard's "Needs fixing"
zone, fetched after first paint.

Split out from the page render because these signals read `sync_history` and
`usage_events` — the tables that grow without bound on a busy instance.
Serving them inline would make `/admin` the slowest page in the product and
would do it on the page an admin opens when something is already wrong. The
page renders its "Needs you" zone server-side (cheap COUNTs) and fills this
zone in behind a skeleton.

Deliberately NOT audit-logged, unlike the /api/admin/reports/* endpoints: this
is a UI-internal rollup polled by a page the caller is already authorised to
see, and every number in it is derived from pages that audit their own reads.
Logging a row per poll would bury real admin activity in `/admin` page loads.

Resolution, per-signal error isolation, and the TTL cache all live in
`app/services/admin_dashboard.py`; the row inventory lives in
`app/web/admin_signals.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.access import require_admin
from app.services.admin_dashboard import resolve_needs_fixing
from app.web.admin_signals import ZONE_NEEDS_FIXING

router = APIRouter(prefix="/api/admin/dashboard", tags=["admin-dashboard"])


@router.get("/signals")
async def dashboard_signals(_admin: dict = Depends(require_admin)) -> dict:
    """Signals in the "Needs fixing" zone that currently have something to say.

    Clear signals are omitted entirely rather than returned with ``count: 0``
    — an empty ``signals`` list IS the healthy state, and the page renders it
    as such. A signal whose resolver raised is returned with ``failed: true``
    so the page can say "couldn't check" instead of implying all-clear.
    """
    resolved = resolve_needs_fixing()
    return {
        "zone": ZONE_NEEDS_FIXING,
        "signals": [r.as_dict() for r in resolved],
    }
