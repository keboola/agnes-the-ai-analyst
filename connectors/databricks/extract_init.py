"""Build the Databricks `extract.duckdb` for `query_mode='remote'` rows.

The materialized path never needed this file: it writes parquets into
``/data/extracts/databricks/data/`` and the orchestrator's ordinary local
discovery picks them up. A ``query_mode='remote'`` row has no parquet, so the
orchestrator needs the two things only an extract can carry —

- a ``_remote_attach`` row telling it to ``INSTALL``/``LOAD`` ``uc_catalog``
  and ``ATTACH`` the workspace's Unity Catalog under the ``dbx`` alias, and
- one view per registered row (``SELECT * FROM dbx."<schema>"."<table>"``),
  which becomes the master view an analyst names in ``agnes query``.

Both halves are written into the SAME ``extract.duckdb`` the materialize path
already registers its parquets in, because the orchestrator ATTACHes exactly
one file per source directory. Writes are additive: a rebuild replaces the
remote views and the ``_remote_attach`` row and leaves every materialized
``_meta`` entry alone.

The catalog must be attached before the views exist
---------------------------------------------------
DuckDB binds a view's catalog reference at ``CREATE VIEW`` time, not at query
time (``Binder Error: Catalog "dbx" does not exist!``), so this builder has to
ATTACH Unity Catalog before it can write a single view — exactly what the
BigQuery extractor does for the same reason. That is a feature: a table that
has been dropped in Unity Catalog fails here, at build time, with the row named,
instead of at 9am inside somebody's query.

It also means the build needs a reachable workspace. ``attach_fn`` is the seam:
production passes the real Unity Catalog ATTACH, and tests substitute a local
DuckDB file attached under the same alias, which exercises the DDL, the
ordering, and the failure handling without a workspace.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import duckdb

from connectors.databricks.attach import UC_ALIAS, UC_EXTENSION, UC_TOKEN_ENV, build_remote_attach_url
from src.db import _open_duckdb  # pins the session to UTC — see tests/test_duckdb_session_tz.py

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_]+$")


def _quote_ident(name: str) -> str:
    """Double-quote a DuckDB identifier, escaping embedded quotes.

    Applied even after the safe-alphabet check above: the check is the gate,
    this is the belt — a future caller that loosens the gate should not silently
    become an injection point (the security playbook's `quote_ident`, never bare
    f-string interpolation).
    """
    return '"' + name.replace('"', '""') + '"'


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS _meta (
            table_name VARCHAR,
            description VARCHAR,
            rows BIGINT,
            size_bytes BIGINT,
            extracted_at TIMESTAMP,
            query_mode VARCHAR
        )"""
    )


def write_remote_attach(conn: duckdb.DuckDBPyConnection, host: str, catalog: str) -> None:
    """(Re)write the single ``_remote_attach`` row for this workspace.

    Dropped and recreated rather than upserted so a changed host or catalog in
    ``instance.yaml`` cannot leave a stale row behind — the BigQuery connector
    learned that lesson the hard way (issue #343: an overlay project change
    left the extract ATTACHing the old project, and every error message pointed
    at a project the operator had already stopped using).
    """
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute(
        """CREATE TABLE _remote_attach (
            alias VARCHAR,
            extension VARCHAR,
            url VARCHAR,
            token_env VARCHAR
        )"""
    )
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        [UC_ALIAS, UC_EXTENSION, build_remote_attach_url(host, catalog), UC_TOKEN_ENV],
    )


def _remote_view_sql(name: str, catalog: str, schema: str, table: str, default_catalog: str) -> str:
    """View DDL for one remote row.

    DuckDB addresses an attached catalog as ``<alias>.<schema>.<table>``, so a
    row pinning a catalog other than the attached one cannot be reached: one
    ATTACH serves one catalog. Such a row is refused by the caller rather than
    silently pointed at the wrong catalog.
    """
    if catalog.lower() != default_catalog.lower():
        raise ValueError(
            f"row targets catalog {catalog!r} but the extract attaches {default_catalog!r}; "
            "the Unity Catalog ATTACH serves one catalog per instance"
        )
    for label, segment in (("name", name), ("schema", schema), ("table", table)):
        pattern = _SAFE_IDENTIFIER if label == "name" else _SAFE_SEGMENT
        if not segment or not pattern.match(segment):
            raise ValueError(f"unsafe databricks {label}: {segment!r}")
    return (
        f"CREATE OR REPLACE VIEW {_quote_ident(name)} AS "
        f"SELECT * FROM {UC_ALIAS}.{_quote_ident(schema)}.{_quote_ident(table)}"
    )


def _default_attach_fn(conn: duckdb.DuckDBPyConnection, *, url: str, token: str) -> None:
    """Install + load the extensions and ATTACH Unity Catalog under ``dbx``.

    The production seam. Kept tiny so the injectable test double has an obvious
    contract: after this returns, ``dbx.<schema>.<table>`` must resolve.
    """
    from connectors.databricks.attach import attach_unity_catalog

    conn.execute(f"INSTALL {UC_EXTENSION} FROM community; LOAD {UC_EXTENSION};")
    conn.execute("INSTALL delta FROM community; LOAD delta;")
    attach_unity_catalog(conn, alias=UC_ALIAS, url=url, token=token)


def init_extract(
    output_dir: str,
    host: str,
    catalog: str,
    table_configs: List[Dict[str, Any]],
    *,
    token: str = "",
    attach_fn=None,
) -> Dict[str, Any]:
    """Write ``_remote_attach`` + one view per remote row into extract.duckdb.

    Returns ``{"tables_registered": int, "errors": [{"table", "error"}]}``. A
    row that cannot be expressed (unsafe identifier, foreign catalog) is
    recorded as an error and skipped — one bad registry row must not cost the
    instance every other remote view.

    When the ATTACH itself fails (extension unavailable, workspace unreachable,
    PAT rejected) every row is reported as skipped and the previous pass's views
    are left in place: a transient workspace outage must not silently empty the
    analyst's catalog.
    """
    from connectors.databricks.extractor import split_bucket

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stats: Dict[str, Any] = {"tables_registered": 0, "errors": []}

    conn = _open_duckdb(str(out / "extract.duckdb"))
    try:
        _ensure_meta_table(conn)
        attach = attach_fn or _default_attach_fn
        try:
            attach(conn, url=build_remote_attach_url(host, catalog), token=token)
        except Exception as e:
            logger.error("databricks extract: Unity Catalog ATTACH failed: %s", e)
            for tc in table_configs:
                stats["errors"].append({"table": tc.get("name"), "error": f"skipped: Unity Catalog ATTACH failed: {e}"})
            return stats

        write_remote_attach(conn, host, catalog)

        # Forget the previous pass's remote rows before rewriting them, so a
        # row unregistered in the admin UI stops appearing in `agnes catalog`.
        # Materialized rows are written by the sync trigger and are left alone.
        conn.execute("DELETE FROM _meta WHERE query_mode = 'remote'")

        for tc in table_configs:
            name = str(tc.get("name") or "")
            try:
                row_catalog, schema = split_bucket(str(tc.get("bucket") or ""), catalog)
                sql = _remote_view_sql(name, row_catalog, schema, str(tc.get("source_table") or ""), catalog)
            except ValueError as e:
                stats["errors"].append({"table": name, "error": str(e)})
                continue
            try:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'remote')",
                    [name, tc.get("description") or "", 0, 0],
                )
                stats["tables_registered"] += 1
            except Exception as e:  # pragma: no cover - DuckDB DDL rarely fails here
                stats["errors"].append({"table": name, "error": str(e)})
    finally:
        conn.close()

    logger.info(
        "databricks extract: %s remote view(s) registered, %s error(s)",
        stats["tables_registered"],
        len(stats["errors"]),
    )
    return stats


def rebuild_from_registry(output_dir: str | None = None) -> Dict[str, Any]:
    """Regenerate the Databricks remote extract from the current registry.

    Returns ``{"skipped": True, "reason": …}`` without touching disk whenever
    the instance has not opted in, is not configured, or has no remote rows —
    the three states in which writing a ``_remote_attach`` row would arm an
    ATTACH nobody asked for.
    """
    from connectors.databricks.attach import attach_enabled
    from connectors.databricks.semantic_layer import resolve_databricks_settings
    from src.repositories import table_registry_repo

    if not attach_enabled():
        return {"skipped": True, "reason": "attach_disabled", "tables_registered": 0, "errors": []}

    settings = resolve_databricks_settings()
    if settings is None or not settings.get("catalog"):
        return {"skipped": True, "reason": "not_configured", "tables_registered": 0, "errors": []}

    rows = [r for r in table_registry_repo().list_by_source("databricks") if (r.get("query_mode") or "") == "remote"]
    if not rows:
        return {"skipped": True, "reason": "no_remote_rows", "tables_registered": 0, "errors": []}

    if output_dir is None:
        output_dir = str(Path(os.environ.get("DATA_DIR", "./data")) / "extracts" / "databricks")

    result = init_extract(
        output_dir,
        settings["host"],
        settings["catalog"],
        rows,
        token=settings["token"],
    )
    result["skipped"] = False
    return result
