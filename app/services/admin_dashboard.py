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
