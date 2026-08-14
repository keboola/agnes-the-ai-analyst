"""Pick the SQL a DuckDB-backed instance can actually run.

Preference order is DUCKDB, then ANSI_SQL. Anything else is reported as
unusable WITH ITS REASON rather than spliced into a query: a warehouse-specific
fragment that happens to parse is more dangerous than one that fails.
"""

from __future__ import annotations

from typing import Optional, Tuple

_PREFERRED = ("DUCKDB", "ANSI_SQL")


def resolve_expression(expression: dict) -> Tuple[Optional[str], Optional[str]]:
    dialects = (expression or {}).get("dialects") or []
    by_name = {
        d.get("dialect"): d.get("expression")
        for d in dialects
        if d.get("expression") and isinstance(d.get("dialect"), str)
    }

    for name in _PREFERRED:
        if by_name.get(name):
            return by_name[name], None

    if not by_name:
        return None, "no expression in any usable dialect"
    offered = ", ".join(sorted(by_name))
    return None, f"only warehouse-specific dialects offered ({offered}); no DUCKDB or ANSI_SQL"
