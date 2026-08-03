"""Admin elevation consent gate.

Historically an Admin-group member's god-mode applied unconditionally on
every request. This module makes elevation a per-browser choice: an admin
can PAUSE their own elevation, and while paused the Admin short-circuit in
``can_access`` is skipped and ``require_admin`` refuses — the admin
experiences the app with exactly their explicit group grants, like any
other user.

State rides a dedicated cookie (``agnes_elevation``) read into a request
context variable by middleware in ``app/main.py``. The cookie can only
ever REDUCE privilege: enforcement remains the server-side Admin-group
membership check either way, so a forged cookie value cannot add
authority a non-admin doesn't have — it can at most re-state the default
an admin already holds. This is a consent gate against accidental
god-mode use (and an audit hook), not a containment boundary: an admin
can re-elevate at will via ``POST /api/me/elevation``.

The instance default flips via ``access.admin_default_elevation`` in
``instance.yaml`` (``"elevated"`` — historical behavior — or
``"paused"`` for consent-first deployments). Non-request contexts
(scheduler, CLI, background jobs) always see the elevated default so
system automation never silently loses authority.
"""

from __future__ import annotations

from contextvars import ContextVar

ELEVATION_COOKIE = "agnes_elevation"

#: Values the cookie/config may carry.
ELEVATED = "elevated"
PAUSED = "paused"

# Request-scoped elevation-paused flag. Default False: non-request
# contexts (scheduler, CLI) and instances that never touch the feature
# behave exactly as before.
_elevation_paused: ContextVar[bool] = ContextVar("agnes_elevation_paused", default=False)


def default_elevation() -> str:
    """Instance-wide default for admins that have not chosen: config key
    ``access.admin_default_elevation``, normalized; unknown values fall
    back to ``elevated`` (the historical behavior)."""
    from app.instance_config import get_value

    raw = str(get_value("access.admin_default_elevation", ELEVATED) or ELEVATED).lower()
    return PAUSED if raw == PAUSED else ELEVATED


def resolve_from_cookie(cookie_value: str | None) -> bool:
    """True (= paused) for this request, given the raw cookie value.

    An explicit cookie wins; its absence (or garbage) means the instance
    default.
    """
    if cookie_value == PAUSED:
        return True
    if cookie_value == ELEVATED:
        return False
    return default_elevation() == PAUSED


def set_paused_for_request(paused: bool):
    """Set the request-scoped flag; returns the contextvar token so the
    middleware can reset it after the response."""
    return _elevation_paused.set(paused)


def reset_for_request(token) -> None:
    _elevation_paused.reset(token)


def elevation_paused() -> bool:
    """Is the current request's admin elevation paused?

    False outside request context by construction (contextvar default).
    """
    return _elevation_paused.get()
