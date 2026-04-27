"""Shared identifier-validation helpers for SQL identifier safety.

Used wherever code constructs SQL by string interpolation against caller-controlled
identifiers (table/dataset names from registry, alias from _remote_attach, etc.).
The DuckDB BQ extension treats identifiers literally — escaping at the call site
is unsafe; whitelist via regex instead.
"""

import logging
import re

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def is_safe_identifier(name: str) -> bool:
    """Return True if `name` is a safe SQL identifier (alnum+underscore, ≤64 chars, leading non-digit)."""
    if not isinstance(name, str):
        return False
    return bool(_SAFE_IDENTIFIER.match(name))


def validate_identifier(name: str, context: str) -> bool:
    """Validate a SQL identifier; log a warning if rejected. Returns True if safe."""
    if not is_safe_identifier(name):
        logger.warning("Rejected unsafe %s identifier: %r", context, name)
        return False
    return True
