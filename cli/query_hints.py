"""Shared hint for unresolvable table names in local DuckDB.

Used by `agnes query` (CLI) and the stdio MCP `query_local` tool so both
surfaces explain query_mode='remote' / server_only tables the same way.
"""

from __future__ import annotations

import re

_TABLE_MISS_RE = re.compile(r"Table with name ([A-Za-z_][A-Za-z0-9_]*) does not exist")


def missing_table(error_text: str) -> str | None:
    """Extract the unresolvable table name from a DuckDB CatalogException
    message, or None if ``error_text`` doesn't match that shape (e.g. a
    plain syntax error)."""
    m = _TABLE_MISS_RE.search(error_text)
    return m.group(1) if m else None


def remote_table_hint(table: str, *, surface: str = "cli") -> str:
    """Human-readable hint explaining that ``table`` might be a
    `query_mode='remote'` or `server_only` table with no local view.

    ``surface`` picks the wording appropriate to the caller: "cli" points
    the user at `agnes query --remote`; "mcp" points the calling agent at
    the `query` MCP tool.
    """
    if surface == "mcp":
        return (
            f"`{table}` might be a `query_mode='remote'` or `server_only` table — "
            "neither has a local view. Use the `query` tool instead: it runs "
            "server-side and routes local/remote tables automatically."
        )
    return (
        f"Note: `{table}` might be a `query_mode='remote'` or "
        "`server_only` table. Local DuckDB only holds views for tables "
        "`agnes pull` downloads — `remote` ones live on BigQuery, and "
        "`server_only` ones are kept server-side and not distributed to "
        "the laptop. Both are queryable server-side:\n"
        "  - List all registered tables:    agnes catalog\n"
        "  - Inspect column schema:         agnes schema <name>\n"
        '  - Run it server-side:            agnes query --remote "<SQL>"'
    )
