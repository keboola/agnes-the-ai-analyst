"""Universal MCP extractor — produces ``extract.duckdb + data/*.parquet`` for materialize-mode tools.

Per the extract.duckdb contract (see ``.claude/skills/agnes-connectors.md``):
every connector writes a ``_meta`` table with one row per produced table. The
``SyncOrchestrator`` scans ``/data/extracts/*/extract.duckdb``, ATTACHes each
into ``analytics.duckdb``, and creates master views automatically.

For Universal MCP, one ``mcp_sources`` row maps to one ``extract.duckdb``.
Each materialize-mode tool registered under that source contributes one
table (= one parquet file + one ``_meta`` row + one view inside
``extract.duckdb``).

Passthrough-mode tools are NOT materialized — they live in ``tool_registry``
and are invoked live at query time by the outbound MCP server's passthrough
handler (see RFC #461 §7).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from connectors.mcp.client import call_tool
from src.duckdb_conn import _open_duckdb
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import MATERIALIZE, ToolRegistryRepository

logger = logging.getLogger(__name__)


# ── backend-aware repo accessors ────────────────────────────────────────────
#
# ``extract_source`` / ``extract_source_async`` take a caller-supplied
# ``system_conn`` (a DuckDB connection) for reading ``mcp_sources`` /
# ``tool_registry``. On a Postgres-backed instance those tables live in
# Postgres — a direct ``MCPSourceRepository(system_conn)`` would silently
# read an empty DuckDB shard regardless of what the caller passed in (the
# admin materialize endpoint hands over a ``Depends(_get_db)`` connection,
# which is always DuckDB). Mirror the escape-hatch pattern used by
# ``app.services.stack_resolver`` / ``app.auth.access``: honor the caller's
# connection only when the active backend actually is DuckDB, otherwise
# route through the factory.


def _sources_repo(system_conn: duckdb.DuckDBPyConnection) -> Any:
    from src.repositories import mcp_sources_repo, use_pg

    if use_pg():
        return mcp_sources_repo()
    return MCPSourceRepository(system_conn)


def _tools_repo(system_conn: duckdb.DuckDBPyConnection) -> Any:
    from src.repositories import tool_registry_repo, use_pg

    if use_pg():
        return tool_registry_repo()
    return ToolRegistryRepository(system_conn)


# ── result parsing ──────────────────────────────────────────────────────────

def _find_data_array(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Heuristic — find the first list-of-dicts inside an MCP tool's JSON payload.

    MCP tools commonly wrap data in a top-level dict like
    ``{"accounts": [...], "total": N}`` or ``{"items": [...]}``. We scan keys
    in insertion order; the first value that is a non-empty list of dicts
    becomes the materialized table.

    If the top-level itself is a list of dicts, use that directly.
    """
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload):
            return payload
        return None
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
    return None


# ── output paths ────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    """Resolve the extracts root. Honors AGNES_DATA_DIR; defaults to ./data."""
    root = os.environ.get("AGNES_DATA_DIR") or os.environ.get("DATA_DIR") or "data"
    return Path(root) / "extracts"


def output_dir_for_source(source_name: str) -> Path:
    return _data_dir() / source_name


# ── _meta + extract.duckdb writers ──────────────────────────────────────────

def _create_meta(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute(
        """CREATE TABLE _meta (
            table_name   VARCHAR NOT NULL,
            description  VARCHAR,
            rows         BIGINT,
            size_bytes   BIGINT,
            extracted_at TIMESTAMP,
            query_mode   VARCHAR DEFAULT 'local'
        )"""
    )


def _insert_meta(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    description: Optional[str],
    rows: int,
    size_bytes: int,
    extracted_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
        [table_name, description, rows, size_bytes, extracted_at],
    )


def _create_view(conn: duckdb.DuckDBPyConnection, table_name: str, parquet_path: Path) -> None:
    # DuckDB does not accept prepared parameters inside read_parquet(),
    # so we inline the path. parquet_path comes from output_dir_for_source()
    # which derives from mcp_sources.name (DB-enforced unique) + the
    # exposed_name we control — no user-supplied path components.
    safe_name = table_name.replace('"', '""')
    safe_path = str(parquet_path).replace("'", "''")
    conn.execute(
        f'CREATE OR REPLACE VIEW "{safe_name}" AS SELECT * FROM read_parquet(\'{safe_path}\')'
    )


# ── extraction ──────────────────────────────────────────────────────────────

async def _materialize_one_tool_async(
    *,
    source: Dict[str, Any],
    tool: Dict[str, Any],
    output_path: Path,
) -> Tuple[int, int]:
    """Call the upstream tool (async-safe), write parquet, return (rows, size_bytes).

    Async-only because the parent extract path may run inside FastAPI's
    event loop (admin /materialize endpoint). The sync wrapper around it
    used ``asyncio.run`` which blows up in that case.
    """
    from connectors.mcp.client import call_tool_async
    original_name = tool["original_name"]
    logger.info("materialize: calling %s.%s", source["name"], original_name)
    result = await call_tool_async(source, original_name, arguments=None)
    if result.is_error:
        raise RuntimeError(f"upstream tool {original_name} returned error: {result.text[:300]}")
    if result.data is None:
        raise ValueError(
            f"tool {original_name} did not return parseable JSON; "
            f"materialize mode requires a JSON response with a list-of-dicts"
        )
    rows = _find_data_array(result.data)
    if rows is None:
        raise ValueError(
            f"tool {original_name} response has no list-of-dicts; "
            f"either reclassify as passthrough or wrap the response"
        )
    df = pd.DataFrame(rows)
    parquet_path = output_path / "data" / f"{tool['exposed_name']}.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(parquet_path, index=False)
    size_bytes = parquet_path.stat().st_size
    return (len(df), size_bytes)


def _materialize_one_tool(
    *,
    source: Dict[str, Any],
    tool: Dict[str, Any],
    output_path: Path,
) -> Tuple[int, int]:
    """Sync wrapper around ``_materialize_one_tool_async`` — only for the
    scheduler / CLI paths that run outside an event loop. FastAPI handlers
    MUST call the async variant directly."""
    return asyncio.run(_materialize_one_tool_async(
        source=source, tool=tool, output_path=output_path,
    ))


async def extract_source_async(
    *,
    system_conn: duckdb.DuckDBPyConnection,
    source_id: str,
    only_tool_id: Optional[str] = None,
    output_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Async variant of ``extract_source`` — call from FastAPI handlers.

    Same return shape as the sync version; the only difference is that
    each upstream call awaits ``_materialize_one_tool_async`` instead of
    going through ``asyncio.run`` (which is illegal inside a running
    event loop).
    """
    sources_repo = _sources_repo(system_conn)
    tools_repo = _tools_repo(system_conn)

    source = sources_repo.get(source_id)
    if source is None:
        raise ValueError(f"mcp_source not found: {source_id}")
    if not source.get("enabled"):
        raise ValueError(f"mcp_source disabled: {source_id}")

    all_tools = tools_repo.list_for_source(source_id)
    tools = [t for t in all_tools if t["mode"] == MATERIALIZE and t.get("enabled", True)]
    if only_tool_id:
        tools = [t for t in tools if t["tool_id"] == only_tool_id]
    if not tools:
        return {"source_name": source["name"], "tables": [], "errors": [], "note": "no materialize tools to run"}

    if output_root is None:
        output_root = output_dir_for_source(source["name"])
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "data").mkdir(exist_ok=True)

    db_path = output_root / "extract.duckdb"
    tmp_db_path = output_root / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    summary_tables: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    out_conn = _open_duckdb(str(tmp_db_path))
    try:
        _create_meta(out_conn)
        for tool in tools:
            extracted_at = datetime.now(timezone.utc)
            try:
                rows, size_bytes = await _materialize_one_tool_async(
                    source=source, tool=tool, output_path=output_root
                )
                _insert_meta(
                    out_conn,
                    table_name=tool["exposed_name"],
                    description=tool.get("description"),
                    rows=rows,
                    size_bytes=size_bytes,
                    extracted_at=extracted_at,
                )
                _create_view(out_conn, tool["exposed_name"], output_root / "data" / f"{tool['exposed_name']}.parquet")
                summary_tables.append({"table": tool["exposed_name"], "rows": rows, "size_bytes": size_bytes})
            except Exception as exc:
                logger.exception("materialize failed for %s.%s", source["name"], tool["original_name"])
                errors.append({"tool": tool["exposed_name"], "error": str(exc)})
    finally:
        out_conn.close()

    if db_path.exists():
        db_path.unlink()
    tmp_db_path.rename(db_path)

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "extract_duckdb": str(db_path),
        "tables": summary_tables,
        "errors": errors,
    }


def extract_source(
    *,
    system_conn: duckdb.DuckDBPyConnection,
    source_id: str,
    only_tool_id: Optional[str] = None,
    output_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Materialize all (or one) materialize-mode tools for an MCP source.

    Writes ``extract.duckdb`` + ``data/*.parquet`` under
    ``<AGNES_DATA_DIR>/extracts/<source.name>/``. The orchestrator's next
    ``rebuild()`` will ATTACH it into ``analytics.duckdb`` automatically.

    Args:
        system_conn: open connection to ``system.duckdb`` (for repo reads).
        source_id:   ``mcp_sources.id`` to extract from.
        only_tool_id: if set, only materialize this one tool (skip others).
        output_root:  override the extracts root (defaults to AGNES_DATA_DIR).

    Returns a summary dict: ``{"source_name": ..., "tables": [...], "errors": [...]}``.
    """
    sources_repo = _sources_repo(system_conn)
    tools_repo = _tools_repo(system_conn)

    source = sources_repo.get(source_id)
    if source is None:
        raise ValueError(f"mcp_source not found: {source_id}")
    if not source.get("enabled"):
        raise ValueError(f"mcp_source disabled: {source_id}")

    all_tools = tools_repo.list_for_source(source_id)
    tools = [t for t in all_tools if t["mode"] == MATERIALIZE and t.get("enabled", True)]
    if only_tool_id:
        tools = [t for t in tools if t["tool_id"] == only_tool_id]
    if not tools:
        return {"source_name": source["name"], "tables": [], "errors": [], "note": "no materialize tools to run"}

    if output_root is None:
        output_root = output_dir_for_source(source["name"])
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "data").mkdir(exist_ok=True)

    db_path = output_root / "extract.duckdb"
    tmp_db_path = output_root / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    summary_tables: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    out_conn = _open_duckdb(str(tmp_db_path))
    try:
        _create_meta(out_conn)
        for tool in tools:
            extracted_at = datetime.now(timezone.utc)
            try:
                rows, size_bytes = _materialize_one_tool(
                    source=source, tool=tool, output_path=output_root
                )
                _insert_meta(
                    out_conn,
                    table_name=tool["exposed_name"],
                    description=tool.get("description"),
                    rows=rows,
                    size_bytes=size_bytes,
                    extracted_at=extracted_at,
                )
                _create_view(out_conn, tool["exposed_name"], output_root / "data" / f"{tool['exposed_name']}.parquet")
                summary_tables.append({"table": tool["exposed_name"], "rows": rows, "size_bytes": size_bytes})
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("materialize failed for %s.%s", source["name"], tool["original_name"])
                errors.append({"tool": tool["exposed_name"], "error": str(exc)})
    finally:
        out_conn.close()

    if db_path.exists():
        db_path.unlink()
    tmp_db_path.rename(db_path)

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "extract_duckdb": str(db_path),
        "tables": summary_tables,
        "errors": errors,
    }


# ── introspect (used at source registration time) ───────────────────────────

def introspect_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Connect to the source and return discovered tools (as plain dicts).

    Convenience wrapper around ``connectors.mcp.client.list_tools`` for the
    admin CLI introspection flow. Async callers (FastAPI handlers) MUST
    use ``introspect_source_async`` — calling this from an async loop
    blows up with ``asyncio.run() cannot be called from a running event
    loop`` because the underlying ``list_tools`` sync wrapper invokes
    ``asyncio.run`` internally.
    """
    from connectors.mcp.client import list_tools  # local import keeps duckdb-free
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in list_tools(source)
    ]


async def introspect_source_async(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Async-safe variant of ``introspect_source`` — call from FastAPI
    handlers (and any code already inside a running event loop)."""
    from connectors.mcp.client import list_tools_async  # local import keeps duckdb-free
    tools = await list_tools_async(source)
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]
