"""X-StorageApi-Token header authentication (spec piece 2).

A non-interactive, PAT-like credential: verified per request against the
Keboola stack (60 s positive cache), mapped to an EXISTING user only, pinned
to credential_surface='stack'. Never provisions accounts, never mints
sessions. The in-module flood guard exists because the slowapi route
decorator cannot wrap a dependency — distinct-invalid-token floods must
neither amplify traffic against the customer's stack nor exhaust the
threadpool.
"""

import hashlib
import logging
import threading
import time
from typing import Optional, Tuple

from app.auth.client_ip import trusted_client_ip
from app.auth.providers import keboola_verify as kv

logger = logging.getLogger(__name__)

VERIFY_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 1024
# Cap on the per-IP flood-guard bookkeeping dicts before a stale-entry sweep
# runs (see _prune_state). Generous — a legitimate deployment sees far fewer
# distinct client IPs than this; the sweep only matters under rotating-source
# abuse.
_STATE_MAX_ENTRIES = 4096

_FAILURE_WINDOW_SECONDS = 60.0
_MAX_FAILURES_PER_IP = 10  # FAILED cache-miss verify calls per IP per window
# Per-process failure cap. The PRIMARY DoS control is the per-IP cap +
# consecutive-failure backoff above (one IP is throttled after 5 failures);
# this global counter is only a coarse backstop bounding a DISTRIBUTED failure
# flood's amplification against the upstream Keboola stack (each miss = one
# /tokens/verify). It is deliberately high — with per-IP backoff arming at 5,
# tripping 300 needs dozens of distinct IPs (a genuine botnet, not "a few bad
# tokens"), so ordinary abuse can't use it to refuse other users' first
# (uncached) verifies. Only FAILED verifies count; successful verifies never
# consume it, so legitimate valid-token traffic never trips it.
_MAX_FAILURES_GLOBAL = 300
_FAILURES_BEFORE_BACKOFF = 5  # consecutive failures from one IP → backoff
_FAILURE_BACKOFF_SECONDS = 60.0

# Sentinel key for the process-wide counter in ``_failure_windows``. Per-IP
# keys are namespaced ``f"ip:{ip}"`` (see ``_ip_key``) so a client whose
# resolved IP literally is the string "__global__" can never alias the
# global bucket.
_GLOBAL_KEY = "__global__"

_lock = threading.Lock()
_cache: dict[str, tuple[float, "kv.VerifiedKeboolaIdentity"]] = {}
# Only FAILED verify attempts are recorded here (see _record_failure) — a
# successful upstream verify must never consume flood budget, or a burst of
# legitimate concurrent callers (bounded only by distinct valid master
# tokens) would 429 each other out. Keyed by "ip:<ip>" plus one shared
# _GLOBAL_KEY entry.
_failure_windows: dict[str, tuple[float, int]] = {}
# Consecutive-failure backoff arming, per IP. Decayed lazily in
# _admit_miss once the backoff window has fully elapsed (see there) so a
# once-armed IP that goes quiet isn't semi-permanently flagged.
_failure_state: dict[str, tuple[float, int]] = {}


def _ip_key(ip: str) -> str:
    return f"ip:{ip}"


def enabled() -> bool:
    from app.switches import switch_value

    if not switch_value("keboola_token_header"):
        return False
    return bool(kv.stack_url() and kv.configured_project_id())


def reset_state_for_tests() -> None:
    with _lock:
        _cache.clear()
        _failure_windows.clear()
        _failure_state.clear()


def _window_count(key: str, now: float) -> int:
    """Current count in the failure window for ``key``, without mutating
    state — an expired window reads as 0. Read-only counterpart to
    ``_bump_failure_window``; callers must hold ``_lock``.
    """
    start, count = _failure_windows.get(key, (now, 0))
    if now - start >= _FAILURE_WINDOW_SECONDS:
        return 0
    return count


def _bump_failure_window(key: str, now: float) -> int:
    start, count = _failure_windows.get(key, (now, 0))
    if now - start >= _FAILURE_WINDOW_SECONDS:
        start, count = now, 0
    count += 1
    _failure_windows[key] = (start, count)
    return count


def _admit_miss(ip: str, now: float) -> Optional[str]:
    """None to admit the upstream verify attempt; 'rate_limited' to refuse
    it before ever contacting the stack.

    Admission is gated ONLY on prior FAILURES (backoff arming + the two
    failure-window counters) — never on the volume of cache misses itself.
    A successful upstream verify records nothing here (see
    ``resolve_header_user``), so a burst of distinct legitimate callers
    (bounded only by distinct valid master tokens × the 60 s positive
    cache) can never trip this guard; only a failure flood — genuine
    invalid-token abuse, whether concentrated on one IP or spread across
    many under the per-IP cap — does. Callers must hold ``_lock``.
    """
    backoff_until, failures = _failure_state.get(ip, (0.0, 0))
    if failures >= _FAILURES_BEFORE_BACKOFF:
        if now < backoff_until:
            return "rate_limited"
        # The backoff window has fully elapsed with no further failures —
        # decay the arming so a once-flagged IP isn't semi-permanently
        # penalized; the next failure (if any) re-arms it from scratch.
        _failure_state.pop(ip, None)
    if _window_count(_ip_key(ip), now) >= _MAX_FAILURES_PER_IP:
        return "rate_limited"
    if _window_count(_GLOBAL_KEY, now) >= _MAX_FAILURES_GLOBAL:
        return "rate_limited"
    return None


def _record_failure(ip: str, now: float) -> None:
    """Record a FAILED cache-miss verify against the per-IP and global
    failure windows, and re-arm the per-IP backoff. Callers must hold
    ``_lock``. Never called on a successful verify — see
    ``resolve_header_user``.
    """
    _bump_failure_window(_ip_key(ip), now)
    _bump_failure_window(_GLOBAL_KEY, now)
    _, failures = _failure_state.get(ip, (0.0, 0))
    failures += 1
    _failure_state[ip] = (now + _FAILURE_BACKOFF_SECONDS, failures)
    _prune_state(now)


def _prune_state(now: float) -> None:
    """Drop stale per-IP flood-guard bookkeeping so a long-running process
    under rotating-source abuse can't accumulate entries indefinitely (Devin
    review on #1288). An elapsed failure window already reads as 0 and an
    elapsed backoff would be decayed on the IP's next touch, so removing them
    changes no admission decision — it only reclaims memory. Bounded work:
    only sweeps once a dict crosses the cap. Callers must hold ``_lock``.
    """
    if len(_failure_windows) > _STATE_MAX_ENTRIES:
        for k, (start, _c) in list(_failure_windows.items()):
            if k != _GLOBAL_KEY and now - start >= _FAILURE_WINDOW_SECONDS:
                _failure_windows.pop(k, None)
    if len(_failure_state) > _STATE_MAX_ENTRIES:
        for k, (backoff_until, _f) in list(_failure_state.items()):
            if now >= backoff_until:
                _failure_state.pop(k, None)


def _prune_cache(now: float) -> None:
    if len(_cache) <= _CACHE_MAX_ENTRIES:
        return
    for key in [k for k, (ts, _) in _cache.items() if now - ts >= VERIFY_CACHE_TTL_SECONDS]:
        _cache.pop(key, None)


def resolve_header_user(token: str, request) -> Tuple[Optional[dict], str]:
    """(user, "") on success; (None, reason) otherwise. Never raises.

    Only the upstream verify result is cached — the users_repo lookup,
    active check, and downstream RBAC run per request, so an Agnes-side
    deactivation takes effect immediately.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    identity = None
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < VERIFY_CACHE_TTL_SECONDS:
            identity = hit[1]

    if identity is None:
        ip = trusted_client_ip(request) or "unknown"
        with _lock:
            refusal = _admit_miss(ip, now)
        if refusal:
            logger.warning("keboola header verify rate-limited for %s (token sha256=%s…)", ip, key[:12])
            return None, refusal
        try:
            identity = kv.verify_storage_token(token)
        except kv.KeboolaVerifyError as exc:
            with _lock:
                _record_failure(ip, now)
            logger.info("keboola header token rejected: %s (sha256=%s…)", exc.reason, key[:12])
            return None, exc.reason
        except Exception:
            # "Never raises" is the contract get_current_user builds on — it
            # has no try/except around this call. kv translates the expected
            # failure modes into KeboolaVerifyError, but anything else (an
            # exception type outside its map, e.g. httpx.InvalidURL from a
            # misconfigured stack address) must still come back as a clean
            # refusal, not a 500 — and must still count against the flood
            # guard, or repeated failures of this shape never throttle
            # (Devin Review on PR #1288).
            with _lock:
                _record_failure(ip, now)
            logger.warning("keboola header verify failed unexpectedly (sha256=%s…)", key[:12], exc_info=True)
            return None, "keboola_verify_error"
        with _lock:
            _failure_state.pop(ip, None)
            _cache[key] = (now, identity)
            _prune_cache(now)

    from src.repositories import users_repo

    try:
        user = users_repo().get_by_email(identity.email)
    except Exception:
        # Same never-raises contract. Deliberately NOT recorded as a failure:
        # the token verified fine — this is a backend hiccup on a valid
        # credential, and backing off the caller's IP for it would lock out
        # legitimate users during a transient DB problem.
        logger.warning("keboola header user lookup failed (sha256=%s…)", key[:12], exc_info=True)
        return None, "keboola_lookup_error"
    if not user:
        return None, "keboola_user_unknown"
    if not bool(user.get("active", True)):
        return None, "deactivated"
    # PAT-equivalent narrowing: an admin authenticated by a data-plane
    # credential gets the 'stack' read surface, never the implicit 'all'.
    user["credential_surface"] = "stack"
    user["token_type"] = "keboola_token"
    return user, ""
