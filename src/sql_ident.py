"""SQL identifier quoting — one implementation for the whole codebase.

Lived in ``src/profiler.py`` until the 2026-08-05 audit found ~40 sites across
connectors, the CLI, the orchestrator and the migration scripts still building
``f'"{name}"'`` by hand. A shared module (plus the ratchet guard in
``tests/test_security_audit_20260805.py``) is what keeps that from coming back:
a rule that lives inside one module gets re-invented outside it.

``src.profiler`` re-exports ``quote_ident`` so existing importers keep working.
"""


def quote_ident(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double-quotes.

    Security audit F1: column names reaching the profiler originate from a
    ``DESCRIBE`` of a parquet materialized from an attacker-uploaded collection
    file, so they are untrusted. Wrapping a name in bare ``"..."`` without
    doubling any embedded ``"`` lets a column named ``x") AS a, (SELECT ...) AS "b``
    break out of the quoted identifier and execute arbitrary SQL against the
    engine. Doubling ``"`` per the SQL standard closes that hole.

    Every ``f'"{ident}"'`` interpolation in the codebase MUST route through
    here — not just the profiler's. Table and view names are as
    attacker-reachable as column names: they come from a connector's
    ``extract.duckdb``, an admin-registered table row, or a server-supplied
    sync manifest.
    """
    return '"' + str(name).replace('"', '""') + '"'
