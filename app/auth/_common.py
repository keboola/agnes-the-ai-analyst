"""Shared helpers for auth providers (Google OAuth, password, email link).

Kept out of `dependencies.py` so it doesn't pull FastAPI auth machinery into
thin provider modules that only need the sanitizer.
"""

from typing import Optional


def safe_next_path(candidate: Optional[str], default: str = "/dashboard") -> str:
    """Return `candidate` if it's a same-origin absolute path, else `default`.

    Open-redirect guard: must start with a single ``/``, followed by something
    that is neither ``/`` nor ``\\``. Browsers normalise ``\\`` to ``/`` in URL
    paths, so ``Location: /\\evil.com`` resolves as ``//evil.com`` — a cross-
    origin redirect — even though Python's ``startswith("//")`` check sees
    ``/\\`` and lets it through. Also rejects ``javascript:...``, ``http://...``,
    bare ``dashboard``, empty/None, etc.
    """
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/"):
        return default
    # Second-char guard covers //evil/, /\evil.com, and similar.
    if len(candidate) > 1 and candidate[1] in ("/", "\\"):
        return default
    return candidate
