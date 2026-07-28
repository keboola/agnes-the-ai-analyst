"""Lightweight DuckDB connection helper — pins session timezone to UTC.

Lives in its own module (separate from `src.db`, which carries heavy
deps like `connectors.bigquery.auth`) so connectors, CLI commands, and
scripts can route through it without paying for unrelated imports.
See `docs/superpowers/specs/2026-05-26-frontend-timezone-fix-design.md`
for the contract this helper enforces.
"""

from __future__ import annotations

import logging
import os

import duckdb

logger = logging.getLogger(__name__)


def _open_duckdb(path, **kwargs):
    """Open a DuckDB connection with session timezone pinned to UTC.

    All `duckdb.connect(...)` call sites in the codebase should funnel
    through this helper. DuckDB's TIMESTAMP type stores naive values, and
    the ICU extension's default session timezone is the host's local zone
    (not UTC). Without pinning, a `datetime.now(timezone.utc)` write gets
    shifted into the host zone before tzinfo is stripped, leading to
    naive-but-local-tz values on disk.

    Uses ``SET GLOBAL`` rather than session-only ``SET`` so cursors
    created via ``conn.cursor()`` inherit the pin (DuckDB cursors do not
    inherit session-level ``SET TimeZone`` — they boot with the ICU
    default). The pin is scoped to the *DuckDB instance* the connection
    opens; a separate ``duckdb.connect()`` to the same path in the same
    process opens a fresh instance and would re-default to ICU's host
    zone — which is exactly why every call site must funnel through here.

    After ``SET``, we ``SELECT current_setting('TimeZone')`` to verify
    the pin actually took. A silent failure (e.g. a future DuckDB
    version that renames the setting) would otherwise produce host-tz
    drift in production with no telemetry; the warning at least surfaces
    in logs.
    """
    conn = duckdb.connect(path, **kwargs)
    try:
        conn.execute("SET GLOBAL TimeZone='UTC'")
    except duckdb.Error as e:
        # Older DuckDB builds without the ICU extension already behave
        # as naive-UTC; nothing to pin. Log at debug to keep diagnostic
        # noise low but discoverable.
        logger.debug("DuckDB SET GLOBAL TimeZone='UTC' failed (no-ICU build?): %s", e)
        return conn
    try:
        tz = conn.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    except duckdb.Error as e:
        logger.warning("DuckDB current_setting('TimeZone') probe failed after SET: %s", e)
        return conn
    if tz != "UTC":
        # Use root logger to ensure visibility even if the caller's
        # module-level logger is filtered. This is a configuration error
        # that silently corrupts every TIMESTAMP write — operators must
        # see it. The env var `AGNES_DUCKDB_TZ_STRICT=1` upgrades this
        # to a hard error for CI / strict deployments.
        msg = (
            f"DuckDB session TimeZone is {tz!r}, not 'UTC' — the pin in "
            "_open_duckdb did not take. All TIMESTAMP writes through "
            "this connection will be host-local-clock naive."
        )
        if os.environ.get("AGNES_DUCKDB_TZ_STRICT") == "1":
            raise RuntimeError(msg)
        logger.warning(msg)
    return conn
