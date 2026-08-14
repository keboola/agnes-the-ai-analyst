"""Per-instance auth provider allowlist (spec 2026-08-12).

``auth.providers`` in instance.yaml (env override ``AGNES_AUTH_PROVIDERS``,
comma-separated) narrows which login methods this instance offers. Unset =
every available provider — byte-for-byte the pre-allowlist behavior. An
explicitly empty (or all-unknown) list is a misconfiguration: rejected at
the admin API, and treated here as unset with a loud error log so one
overlay write can never lock every user out of the instance. The same
fail-open applies at read time when the list names only *unconfigured*
providers (e.g. ``keboola`` with no stack configured): an allowlist that
would leave zero usable login methods is treated as unset, so the env /
static-file path — which the admin API's lockout guard never sees — cannot
lock the instance out either.
"""

import importlib
import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException

from app.instance_config import get_value

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: tuple[str, ...] = ("google", "email", "password", "keboola")

# Single-slot cache for the parsed allowlist, keyed by the raw configured value.
# See configured_allowlist() for why parsing + misconfig logging must not re-run
# per request.
_ALLOWLIST_CACHE: Optional[tuple[tuple, Optional[list[str]]]] = None

# Providers whose usability depends on instance configuration; ``password``
# needs none and is always usable. Mirrors the login page's per-provider
# ``is_available()`` probes.
_AVAILABILITY_PROBES: dict[str, str] = {
    "google": "app.auth.providers.google",
    "email": "app.auth.providers.email",
    "keboola": "app.auth.providers.keboola",
}

# One-shot marker so the lockout rescue logs once per distinct configuration,
# not on every request (same rationale as the parse cache above).
_LOCKOUT_RESCUE_LOGGED: Optional[tuple] = None


def _provider_available(name: str) -> bool:
    """Config-completeness of one provider (``password``: nothing to
    configure). Probes lazily and treats a raising probe as unavailable,
    matching the login page's try/except around the same calls."""
    module_path = _AVAILABILITY_PROBES.get(name)
    if module_path is None:
        return True
    try:
        return bool(importlib.import_module(module_path).is_available())
    except Exception:
        return False


def configured_allowlist() -> Optional[list[str]]:
    raw_env = os.environ.get("AGNES_AUTH_PROVIDERS")
    if raw_env is not None:
        cache_key: tuple = ("env", raw_env)
        source: Optional[object] = raw_env
    else:
        source = get_value("auth", "providers")
        cache_key = ("cfg", repr(source))

    # Single-slot cache keyed on the raw configured value. `configured_allowlist`
    # runs on every /auth/* request (router dependency) and several times per
    # /login render, so parsing — and especially the misconfig `logger.warning`/
    # `logger.error` below — must not re-fire per request: a stale typo would
    # otherwise write duplicate lines on every page load. The key changes when
    # the env/config value changes (typical in tests), so the cache transparently
    # re-parses and the diagnostic is emitted once per distinct configuration.
    # Same pattern as `_LOCAL_DEV_GROUPS_CACHE` in app.auth.dependencies.
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is not None and _ALLOWLIST_CACHE[0] == cache_key:
        return _rescue_if_unusable(cache_key, _ALLOWLIST_CACHE[1])

    result = _parse_allowlist(source)
    _ALLOWLIST_CACHE = (cache_key, result)
    return _rescue_if_unusable(cache_key, result)


def _rescue_if_unusable(cache_key: tuple, allowlist: Optional[list[str]]) -> Optional[list[str]]:
    """Treat an allowlist naming only unconfigured providers as unset.

    ``auth.providers: [keboola]`` with no stack configured would render zero
    login buttons and 404 every ``/auth/*`` route — an unrecoverable lockout
    reachable via env/instance.yaml, which the admin API's write-time guard
    never sees (Devin Review on PR #1288). Availability is re-probed per call
    (NOT folded into the parse cache) because provider configuration can
    change at runtime via the settings overlay; the probes are cheap config
    reads and short-circuit on the first available provider. The error log is
    once per distinct configuration, like the parse diagnostics."""
    if allowlist is None or any(_provider_available(name) for name in allowlist):
        return allowlist
    global _LOCKOUT_RESCUE_LOGGED
    state = (cache_key, tuple(allowlist))
    if _LOCKOUT_RESCUE_LOGGED != state:
        _LOCKOUT_RESCUE_LOGGED = state
        logger.error(
            "auth.providers names only unconfigured providers (%s) — no login method "
            "would be usable; treating as unset (all providers) so the instance stays "
            "reachable; fix the configuration",
            ", ".join(allowlist),
        )
    return None


def _parse_allowlist(source: Optional[object]) -> Optional[list[str]]:
    """Parse the raw ``auth.providers`` value into a known-provider allowlist,
    logging misconfiguration exactly once per distinct value (the caller caches
    on the raw value). ``None`` (unset, or set-but-all-unknown) ⇒ all providers."""
    if source is None:
        return None
    if isinstance(source, str):
        values = [v.strip() for v in source.split(",") if v.strip()]
    elif isinstance(source, (list, tuple, set)):
        values = [str(v).strip() for v in source if str(v).strip()]
    else:
        # A YAML scalar (auth.providers: true / 5) is not iterable. Treat it as
        # unset (all providers) with a loud error rather than letting a TypeError
        # propagate out of the per-request router dependency and 500 every login
        # page — the same fail-open contract as an all-unknown list.
        logger.error(
            "auth.providers must be a list or comma-separated string, got %s — treating as unset",
            type(source).__name__,
        )
        return None
    unknown = [v for v in values if v not in KNOWN_PROVIDERS]
    for name in unknown:
        logger.warning("auth.providers: unknown provider %r ignored", name)
    known = [v for v in values if v in KNOWN_PROVIDERS]
    if not known:
        logger.error(
            "auth.providers is set but names no known provider — treating as unset "
            "(all providers) so the instance stays reachable; fix the configuration"
        )
        return None
    return known


def provider_allowed(name: str) -> bool:
    allowlist = configured_allowlist()
    return allowlist is None or name in allowlist


def require_provider(name: str) -> Callable[[], None]:
    """Router-level dependency: excluded provider endpoints return 404
    (not 403 — an excluded method should not advertise its existence)."""

    def _dep() -> None:
        if not provider_allowed(name):
            raise HTTPException(status_code=404, detail="Not Found")

    return _dep
