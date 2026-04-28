"""DuckDB identifier validation — shared across orchestrator and extractors.

Issue #81 Group D — extractor-layer SQL injection (M15) is the peer of the
orchestrator's `_meta.table_name` SQLi (M14, fixed previously by
`src/orchestrator.py:_validate_identifier`). Same trust problem at a
different layer: an attacker who controls the contents of `table_registry`
(admin or whoever can write to that table) can inject SQL via identifier
interpolation in a connector's `CREATE OR REPLACE VIEW` / `COPY` /
`INSERT INTO _meta` statements.

Lifted from `src/orchestrator.py` so both layers use the same regex.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Strict DuckDB identifier — letter or underscore start, alphanumeric/underscore body,
# bounded length. Use for orchestrator-side aliases, extension names, view names —
# anything we generate or that comes from a tightly-controlled namespace.
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# Relaxed identifier allowing the dotted/dashed forms that real upstreams use
# — Keboola buckets (`in.c-foo`), BigQuery datasets, etc. Still refuses anything
# that could break out of a `"..."` quoted identifier (no `"`, no `'`, no `;`,
# no control chars, no NUL). 128-char cap matches common DB identifier limits.
_SAFE_QUOTED_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.\-]{0,127}$")


def is_safe_identifier(name: object) -> bool:
    """Return True if ``name`` is safe to interpolate into a strict
    DuckDB identifier position (alias, extension, schema we generate)."""
    return isinstance(name, str) and bool(_SAFE_IDENTIFIER.match(name))


def is_safe_quoted_identifier(name: object) -> bool:
    """Return True if ``name`` is safe to interpolate **inside** double-quotes
    in a DuckDB identifier position. Allows `.` and `-` for upstream
    naming conventions (Keboola buckets like `in.c-events`, BigQuery
    datasets) but refuses anything that could close the quote or
    inject control characters."""
    return isinstance(name, str) and bool(_SAFE_QUOTED_IDENTIFIER.match(name))


def validate_identifier(name: str, context: str) -> bool:
    """Strict check — returns True if safe, False (with WARNING log) if not.
    Use for identifiers that should match `[a-zA-Z_][a-zA-Z0-9_]*`."""
    if not is_safe_identifier(name):
        logger.warning("Rejected unsafe %s identifier: %r", context, name)
        return False
    return True


def validate_quoted_identifier(name: str, context: str) -> bool:
    """Relaxed check for upstream-typed identifiers (buckets, datasets).
    Accepts dots and dashes; refuses quote/semicolon/control chars."""
    if not is_safe_quoted_identifier(name):
        logger.warning("Rejected unsafe %s identifier: %r", context, name)
        return False
    return True
