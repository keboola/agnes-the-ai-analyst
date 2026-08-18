"""Per-instance auth provider allowlist (spec 2026-08-12).

``auth.providers`` in instance.yaml (env override ``AGNES_AUTH_PROVIDERS``,
comma-separated) narrows which login methods this instance offers. Unset =
every available provider — byte-for-byte the pre-allowlist behavior. An
explicitly empty (or all-unknown) list is a misconfiguration: rejected at
the admin API, and treated here as unset with a loud error log so one
overlay write can never lock every user out of the instance. A narrower
rescue applies at read time when the list names only *unconfigured*
providers (e.g. ``keboola`` with no stack configured): an allowlist that
would leave zero usable login methods falls back to password + magic link,
so the env / static-file path — which the admin API's lockout guard never
sees — cannot lock the instance out either. Deliberately NOT "treat as
unset": that would re-offer the self-provisioning OAuth providers, turning
one typo into a widening of who may sign in.
"""

import importlib
import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException

from app.instance_config import get_value

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: tuple[str, ...] = ("google", "email", "password", "keboola", "microsoft")

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
    "microsoft": "app.auth.providers.microsoft",
}

# What an unusable allowlist falls back to. Both require an existing user row
# to authenticate anybody (password: ``password_hash``; email: the magic link
# is only minted for a known address), so the fallback can never widen who may
# sign in — unlike "treat as unset", which re-offers the self-provisioning
# OAuth providers.
_RESCUE_PROVIDERS: tuple[str, ...] = ("password", "email")

# One-shot marker so the lockout rescue logs once per distinct configuration,
# not on every request (same rationale as the parse cache above).
_LOCKOUT_RESCUE_LOGGED: Optional[tuple] = None


def _provider_available(name: str) -> bool:
    """Config-completeness of one provider (``password``: nothing to
    configure). Probes lazily and treats a raising probe as unavailable,
    matching the login page's try/except around the same calls.

    Raise-as-unavailable is a deliberate direction of failure: on a healthy
    instance these probes are in-memory config/env reads that do not raise,
    and if one somehow does, reading it as available would leave the login
    page offering only a provider that is actively broken — a lockout. The
    cost is that a transient raise can trip ``_rescue_if_unusable`` and widen
    the offering to all providers for the fault's duration (Devin Review on
    PR #1288) — accepted, because the rescue's one-shot error log makes it
    loud and every offered provider still authenticates on its own merits;
    availability here gates OFFERING, never identity.
    """
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
    """Fall back to local sign-in when an allowlist names only unconfigured providers.

    ``auth.providers: [keboola]`` with no stack configured would render zero
    login buttons and 404 every ``/auth/*`` route — an unrecoverable lockout
    reachable via env/instance.yaml, which the admin API's write-time guard
    never sees (Devin Review on PR #1288). Availability is re-probed per call
    (NOT folded into the parse cache) because provider configuration can
    change at runtime via the settings overlay; the probes are cheap config
    reads and short-circuit on the first available provider. The error log is
    once per distinct configuration, like the parse diagnostics.

    The rescue lands on ``_RESCUE_PROVIDERS`` rather than on "unset", because
    "unset" means *every* provider and that turns a misconfiguration into a
    widening: an operator who narrowed to one OAuth provider and then mistyped
    its configuration would get Google back on the login page, and with
    ``auth.allowed_domain`` unset any Google account self-provisions. Password
    and magic link both need an existing user row, so they end the lockout
    without admitting anyone new."""
    if allowlist is None or any(_provider_available(name) for name in allowlist):
        return allowlist
    global _LOCKOUT_RESCUE_LOGGED
    state = (cache_key, tuple(allowlist))
    if _LOCKOUT_RESCUE_LOGGED != state:
        _LOCKOUT_RESCUE_LOGGED = state
        # Say what the fallback can actually do, rather than asserting
        # reachability. Both rescue providers authenticate only EXISTING
        # accounts, and on an instance with no mail transport that leaves
        # password — which needs a row that already carries a hash. An
        # OAuth-only instance may therefore have no usable door until the
        # configuration is fixed, and the operator has to hear that here
        # rather than discover it on the login page.
        if _provider_available("email"):
            logger.error(
                "auth.providers names only unconfigured providers (%s) — no login method "
                "would be usable; falling back to %s. Neither can self-provision an "
                "account, so only EXISTING users can sign in until the configuration "
                "is fixed.",
                ", ".join(allowlist),
                ", ".join(_RESCUE_PROVIDERS),
            )
        else:
            logger.error(
                "auth.providers names only unconfigured providers (%s) AND no mail "
                "transport is configured — the fallback is password sign-in alone, "
                "which works only for accounts that already hold a password. If none "
                "do, NOBODY can sign in until the configuration is fixed; recover with "
                "`agnes admin break-glass grant-admin` (operates on the database "
                "directly, no login) or SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD.",
                ", ".join(allowlist),
            )
    return list(_RESCUE_PROVIDERS)


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
