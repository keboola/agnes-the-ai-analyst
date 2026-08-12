"""Resolution layer for the `/admin` dashboard signals declared in
`app/web/admin_signals.py`.

Two things live here that the spec list deliberately does not:

**Isolation.** Every resolver runs inside its own try/except. `/admin` is the
page an admin lands on when something is already wrong, so it is exactly the
page that must not 500 because one repo raised. A failing resolver degrades to
a single row rendered as "unavailable" and the other eight still render.

**Cost control.** Zone 1 resolvers are COUNT-shaped and run inline during the
page render. Zone 2 resolvers read `sync_history` and `usage_events` — the
tables that grow without bound on a busy instance — so they are fetched
after first paint via `GET /api/admin/dashboard/signals` and memoised behind a
short process-local TTL. The TTL is what stops a tab left open on `/admin`
(or three admins during an incident) from turning a dashboard into a load
source; it is deliberately short enough that an admin who fixes a failing sync
and refreshes sees it clear within the minute.

The cache is per-process and unsynchronised across replicas on purpose: it
memoises a read-only rollup, so the worst case of a cold replica is one extra
aggregate query, not an inconsistency anyone can observe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.web.admin_signals import (
    ADMIN_SIGNALS,
    ZONE_NEEDS_FIXING,
    ZONE_NEEDS_YOU,
    Signal,
    SignalSpec,
    signals_for_zone,
)

logger = logging.getLogger(__name__)

# Zone 2 only. Zone 1 is cheap and always fresh — an approval queue that keeps
# showing a submission the admin just approved is worse than one extra COUNT.
_ZONE_FIXING_TTL_SECONDS = 60


@dataclass(frozen=True)
class ResolvedSignal:
    """A spec plus its outcome, ready to render.

    ``signal is None and not failed`` means "nothing to report" — the row is
    dropped by `resolve_zone`. ``failed`` means the resolver raised; the row
    survives so the admin knows a check is broken rather than clear.
    """

    key: str
    title: str
    zone: str
    severity: str
    signal: Optional[Signal]
    failed: bool = False

    @property
    def count(self) -> int:
        return self.signal.count if self.signal else 0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "zone": self.zone,
            "severity": self.severity,
            "failed": self.failed,
            "count": self.count,
            "href": self.signal.href if self.signal else None,
            "blurb": self.signal.blurb if self.signal else "Could not be checked.",
        }


def _resolve_one(spec: SignalSpec) -> Optional[ResolvedSignal]:
    try:
        signal = spec.resolve()
    except Exception:
        # Never let one bad signal take the admin's home page with it.
        logger.warning("admin dashboard signal %r failed to resolve", spec.key, exc_info=True)
        return ResolvedSignal(
            key=spec.key,
            title=spec.title,
            zone=spec.zone,
            severity="warn",
            signal=None,
            failed=True,
        )
    if signal is None:
        return None
    return ResolvedSignal(
        key=spec.key,
        title=spec.title,
        zone=spec.zone,
        severity=spec.severity,
        signal=signal,
    )


def resolve_zone(zone: str) -> list[ResolvedSignal]:
    """Every signal in *zone* that has something to say, in declaration order.

    Clear signals are dropped entirely (rule 1 in `admin_signals`), so an
    empty list is the "nothing needs your attention" state and the caller
    renders it as such.
    """
    out = []
    for spec in signals_for_zone(zone):
        resolved = _resolve_one(spec)
        if resolved is not None:
            out.append(resolved)
    return out


def resolve_needs_you() -> list[ResolvedSignal]:
    """Zone 1, resolved inline during the `/admin` render."""
    return resolve_zone(ZONE_NEEDS_YOU)


# --- Zone 2 cache -----------------------------------------------------------

_cache_lock = threading.Lock()
_cache_value: Optional[list[ResolvedSignal]] = None
_cache_at: float = 0.0


def resolve_needs_fixing(*, force: bool = False) -> list[ResolvedSignal]:
    """Zone 2, memoised for `_ZONE_FIXING_TTL_SECONDS`.

    The lock is held across resolution so a burst of concurrent requests on a
    cold cache produces ONE pass over the audit/history tables rather than one
    per caller — the stampede is the whole reason this is cached.
    """
    global _cache_value, _cache_at
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache_value is not None and (now - _cache_at) < _ZONE_FIXING_TTL_SECONDS:
            return _cache_value
        _cache_value = resolve_zone(ZONE_NEEDS_FIXING)
        _cache_at = time.monotonic()
        return _cache_value


def invalidate_cache() -> None:
    """Drop the Zone-2 cache. Used by tests, which must not inherit a rollup
    computed against a previous fixture's data."""
    global _cache_value, _cache_at
    with _cache_lock:
        _cache_value = None
        _cache_at = 0.0


def signal_keys() -> list[str]:
    """Every declared key — used by the guard test and for debugging."""
    return [s.key for s in ADMIN_SIGNALS]


# ---------------------------------------------------------------------------
# Journey — the setup path + the three gap cards on `/admin`.
#
# A different contract from the signal zones above, which is why these are NOT
# SignalSpecs: rule 1 there is "zero renders nothing", while a gap card always
# renders — the healthy state ("every table is in a package") is information,
# not noise, because it is the chain People → Data → Access read end to end.
# The setup path self-retires instead: it renders only until all four stages
# are done, the same pattern as the rail's onboarding card.
#
# Same isolation rule as the zones: each section resolves inside its own
# try/except and degrades to a `failed` marker, never a 500 — `/admin` is the
# page an admin opens when something is already wrong.
# ---------------------------------------------------------------------------

# `query_mode` values whose parquet is distributed to analysts via data
# packages (`agnes pull`). Blank/NULL reads as `local`, the same fold the
# manifest and the distribution gate use (see `_assert_distributable` in
# app/api/data.py). `remote` tables are reachable without a package
# (server-side execution), so they are deliberately NOT counted as
# "unreachable" when unpackaged.
_DISTRIBUTABLE_QUERY_MODES = ("", "local", "materialized")


def _is_distributable(row: dict) -> bool:
    return (row.get("query_mode") or "") in _DISTRIBUTABLE_QUERY_MODES


def _data_package_grants() -> list[dict]:
    from src.repositories import resource_grants_repo

    return resource_grants_repo().list_all(resource_type="data_package")


def _resolve_setup() -> dict:
    """The four stages between an empty instance and data in someone's hands.

    Each step names the dependency the product otherwise leaves invisible
    ("tables reach analysts only through a data package") and links to the
    page where that step is done.
    """
    from src.repositories import (
        data_packages_repo,
        source_connections_repo,
        table_registry_repo,
    )

    connections = source_connections_repo().list()
    tables = table_registry_repo().count_non_internal()
    member_ids = data_packages_repo().list_member_ids_bulk()
    packages_with_tables = sum(1 for ids in member_ids.values() if ids)
    grants = _data_package_grants()

    steps = [
        {
            "key": "connect",
            "title": "Connect a source",
            "done": bool(connections) or tables > 0,
            "detail": (
                f"{len(connections)} source connection{'s' if len(connections) != 1 else ''} configured."
                if connections
                else "Connect a Keboola project or another source."
            ),
            "href": "/admin/data-sources",
            "cta": "Connect →",
        },
        {
            "key": "tables",
            "title": "Add tables",
            "done": tables > 0,
            "detail": (
                f"{tables} table{'s' if tables != 1 else ''} registered."
                if tables
                else "Pick the tables Agnes should manage."
            ),
            "href": "/admin/data-sources",
            "cta": "Add tables →",
        },
        {
            "key": "package",
            "title": "Bundle into packages",
            "done": packages_with_tables > 0,
            "detail": (
                f"{packages_with_tables} package{'s hold' if packages_with_tables != 1 else ' holds'} tables."
                if packages_with_tables
                else "Tables reach analysts only through a data package."
            ),
            "href": "/admin/tables",
            "cta": "Bundle →",
        },
        {
            "key": "share",
            "title": "Share with people",
            "done": bool(grants),
            "detail": (
                f"{len(grants)} grant{'s' if len(grants) != 1 else ''} on data packages."
                if grants
                else "Grant packages to groups so analysts can pull them."
            ),
            "href": "/admin/data-packages",
            "cta": "Share →",
        },
    ]
    # The first not-done step is the one the CTA belongs to; later steps stay
    # CTA-less so the path reads as a sequence rather than four alarms.
    current = next((s for s in steps if not s["done"]), None)
    for s in steps:
        s["current"] = s is current
    return {"complete": current is None, "steps": steps}


def _resolve_gap_people() -> Optional[dict]:
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    groups = user_groups_repo().list_all()
    active_users = [u for u in users_repo().list_all() if u.get("active", True)]
    granted_group_ids = {g["group_id"] for g in _data_package_grants()}

    everyone_ids = {g["id"] for g in groups if g.get("is_system") and g.get("name") == "Everyone"}
    admin_ids = {g["id"] for g in groups if g.get("is_system") and g.get("name") == "Admin"}

    members = user_group_members_repo()
    if granted_group_ids & everyone_ids:
        # Auto-membership: a grant to Everyone covers every account.
        uncovered = 0
    else:
        covered: set[str] = set()
        for gid in granted_group_ids | admin_ids:  # admins have god-mode
            # `list_members_for_group` joins users — `id` is the USER id.
            covered.update(m["id"] for m in members.list_members_for_group(gid))
        uncovered = sum(1 for u in active_users if u["id"] not in covered)

    return {
        "key": "people",
        "title": "People",
        "href": "/admin/users",
        "facts": [
            {"n": len(active_users), "label": "people"},
            {"n": len(groups), "label": "groups"},
        ],
        "gap_count": uncovered,
        "gap_text": (
            f"{uncovered} {'person is' if uncovered == 1 else 'people are'} in no group with access to a data package"
        ),
        "ok_text": "Everyone with an account can reach at least one data package.",
    }


def _resolve_gap_data() -> Optional[dict]:
    from src.repositories import (
        data_packages_repo,
        source_connections_repo,
        table_registry_repo,
    )

    tables = [t for t in table_registry_repo().list_all() if (t.get("source_type") or "") != "internal"]
    packages_repo = data_packages_repo()
    packaged: set[str] = set()
    for ids in packages_repo.list_member_ids_bulk().values():
        packaged.update(ids)
    unpackaged = sum(1 for t in tables if _is_distributable(t) and t["id"] not in packaged)
    sources = len(source_connections_repo().list())
    packages = len(packages_repo.list())

    return {
        "key": "data",
        "title": "Data",
        "href": "/admin/tables",
        "facts": [
            {"n": sources, "label": "sources"},
            {"n": len(tables), "label": "tables"},
            {"n": packages, "label": "packages"},
        ],
        "gap_count": unpackaged,
        "gap_text": (
            f"{unpackaged} table{' is' if unpackaged == 1 else 's are'} in no package "
            "— analysts cannot pull " + ("it" if unpackaged == 1 else "them")
        ),
        "ok_text": "Every distributable table is in a package.",
    }


def _resolve_gap_access() -> Optional[dict]:
    from src.repositories import data_packages_repo

    packages = data_packages_repo().list()
    granted_ids = {g["resource_id"] for g in _data_package_grants()}
    unshared = sum(1 for p in packages if p["id"] not in granted_ids)
    shared = len(packages) - unshared

    return {
        "key": "access",
        "title": "Access",
        "href": "/admin/data-packages",
        "facts": [
            {"n": shared, "label": "packages shared"},
            {"n": len(packages), "label": "total"},
        ],
        "gap_count": unshared,
        "gap_text": (
            f"{unshared} package{' is' if unshared == 1 else 's are'} shared with nobody — invisible to analysts"
        ),
        "ok_text": "Every package is shared with at least one group.",
    }


_GAP_RESOLVERS = (
    ("people", "People", "/admin/users", _resolve_gap_people),
    ("data", "Data", "/admin/tables", _resolve_gap_data),
    ("access", "Access", "/admin/data-packages", _resolve_gap_access),
)


def resolve_journey() -> dict:
    """Setup path + gap cards for `/admin` — every count from existing repo
    reads, resolved inline (they are all COUNT/short-list shaped).

    ``setup`` is None when its resolver raised (the template then simply
    omits the panel); a gap card that raised survives as ``failed`` so a
    broken check never reads as a healthy chain.
    """
    try:
        setup = _resolve_setup()
    except Exception:
        logger.exception("admin journey: setup path failed to resolve")
        setup = None

    gaps: list[dict] = []
    for key, title, href, resolver in _GAP_RESOLVERS:
        try:
            card = resolver()
        except Exception:
            logger.exception("admin journey: gap card %r failed to resolve", key)
            card = {
                "key": key,
                "title": title,
                "href": href,
                "facts": [],
                "gap_count": 0,
                "gap_text": "",
                "ok_text": "",
                "failed": True,
            }
        if card is not None:
            card.setdefault("failed", False)
            gaps.append(card)
    return {"setup": setup, "gaps": gaps}
