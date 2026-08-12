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

_MISS_WINDOW_SECONDS = 60.0
_MAX_MISSES_PER_IP = 10  # cache-miss verify calls per IP per window
_MAX_MISSES_GLOBAL = 30  # ... and per process per window
_FAILURES_BEFORE_BACKOFF = 5  # consecutive failures from one IP → backoff
_FAILURE_BACKOFF_SECONDS = 60.0

_GLOBAL_KEY = "__global__"

_lock = threading.Lock()
_cache: dict[str, tuple[float, "kv.VerifiedKeboolaIdentity"]] = {}
_miss_windows: dict[str, tuple[float, int]] = {}
_failure_state: dict[str, tuple[float, int]] = {}


def enabled() -> bool:
    from app.switches import switch_value

    if not switch_value("keboola_token_header"):
        return False
    return bool(kv.stack_url() and kv.configured_project_id())


def reset_state_for_tests() -> None:
    with _lock:
        _cache.clear()
        _miss_windows.clear()
        _failure_state.clear()


def _bump_window(key: str, now: float) -> int:
    start, count = _miss_windows.get(key, (now, 0))
    if now - start >= _MISS_WINDOW_SECONDS:
        start, count = now, 0
    _miss_windows[key] = (start, count + 1)
    return count + 1


def _admit_miss(ip: str, now: float) -> Optional[str]:
    """None to admit the upstream verify; 'rate_limited' to refuse."""
    backoff_until, failures = _failure_state.get(ip, (0.0, 0))
    if failures >= _FAILURES_BEFORE_BACKOFF and now < backoff_until:
        return "rate_limited"
    if _bump_window(ip, now) > _MAX_MISSES_PER_IP:
        return "rate_limited"
    if _bump_window(_GLOBAL_KEY, now) > _MAX_MISSES_GLOBAL:
        return "rate_limited"
    return None


def _record_failure(ip: str, now: float) -> None:
    _, failures = _failure_state.get(ip, (0.0, 0))
    failures += 1
    _failure_state[ip] = (now + _FAILURE_BACKOFF_SECONDS, failures)


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
        with _lock:
            _failure_state.pop(ip, None)
            _cache[key] = (now, identity)
            _prune_cache(now)

    from src.repositories import users_repo

    user = users_repo().get_by_email(identity.email)
    if not user:
        return None, "keboola_user_unknown"
    if not bool(user.get("active", True)):
        return None, "deactivated"
    # PAT-equivalent narrowing: an admin authenticated by a data-plane
    # credential gets the 'stack' read surface, never the implicit 'all'.
    user["credential_surface"] = "stack"
    user["token_type"] = "keboola_token"
    return user, ""
