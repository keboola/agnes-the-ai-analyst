"""Shared helpers for auth providers (Google OAuth, password, email link).

Kept out of `dependencies.py` so it doesn't pull FastAPI auth machinery into
thin provider modules that only need the sanitizer.
"""

from typing import Optional


def safe_next_path(candidate: Optional[str], default: str = "/dashboard") -> str:
    """Return `candidate` if it's a same-origin absolute path, else `default`.

    Open-redirect guard: must start with a single `/` and must NOT start with
    `//` (which browsers treat as protocol-relative, i.e. cross-origin).
    Accepts plain paths like `/catalog` or `/foo?bar=baz`. Rejects
    `javascript:...`, `http://...`, `//evil/`, bare `dashboard`, empty/None, etc.
    """
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    return candidate
