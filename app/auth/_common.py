"""Shared helpers for auth providers (Google OAuth, password, email link).

Kept out of `dependencies.py` so it doesn't pull FastAPI auth machinery into
thin provider modules that only need the sanitizer.
"""

from typing import Optional


def safe_next_path(candidate: Optional[str], default: Optional[str] = None) -> str:
    """Return `candidate` if it's a same-origin absolute path, else `default`.

    Open-redirect guard: must start with a single `/` and must NOT start with
    `//` (which browsers treat as protocol-relative, i.e. cross-origin).
    Accepts plain paths like `/catalog` or `/foo?bar=baz`. Rejects
    `javascript:...`, `http://...`, `//evil/`, bare `dashboard`, empty/None, etc.

    When `default` is None, resolves to the operator-configured home route
    (`AGNES_HOME_ROUTE` env > `instance.home_route` YAML > `/dashboard`) so an
    instance with `AGNES_HOME_ROUTE=/home` lands users on /home after OAuth /
    magic-link / password login instead of the legacy /dashboard.

    Lazy-imported to keep this module dependency-free for thin provider
    modules that don't otherwise need `app.instance_config`.
    """
    if default is None:
        from app.instance_config import get_home_route
        default = get_home_route()
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    return candidate
