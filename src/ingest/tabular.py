"""Tabular-file ingestion — uploaded CSV/TSV/Parquet/JSON/XLSX become queryable
DuckDB tables via the ``extract.duckdb`` contract.

A tabular file is converted to parquet under
``$DATA_DIR/extracts/collection_<corpus_id>/data/<table>.parquet``, exposed
through that source's ``extract.duckdb`` (``_meta`` row + view), and registered
in ``table_registry`` so it flows through the normal catalog / sync path. This
is Agnes's structural advantage over generic "chat with your files" tools: the
table is answered with SQL, not embedding similarity.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


from src.duckdb_conn import _open_duckdb
from src.repositories import table_registry_repo

logger = logging.getLogger(__name__)

# Extensions handled by DuckDB native readers (no extra dependency).
_NATIVE_READERS = {
    "csv": "read_csv_auto",
    "txt": "read_csv_auto",
    "tsv": "read_csv_auto",
    "parquet": "read_parquet",
    "json": "read_json_auto",
    "jsonl": "read_json_auto",
}
_XLSX_EXTS = {"xlsx", "xls"}


class UnsupportedTabular(Exception):
    """Raised when a file's extension has no tabular reader."""


def _extracts_dir() -> Path:
    root = os.environ.get("DATA_DIR") or os.environ.get("AGNES_DATA_DIR") or "data"
    return Path(root) / "extracts"


def _sanitize(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower()
    return s or "table"


def _reader_expr(ext: str, storage_path: str) -> Optional[str]:
    safe = str(storage_path).replace("'", "''")
    fn = _NATIVE_READERS.get(ext)
    if fn == "read_csv_auto" and ext == "tsv":
        return f"read_csv_auto('{safe}', delim='\\t')"
    if fn:
        return f"{fn}('{safe}')"
    if ext in _XLSX_EXTS:
        return None  # handled via the excel extension in ingest_tabular
    raise UnsupportedTabular(f"no tabular reader for '.{ext}'")


def ingest_tabular(
    corpus_id: str,
    file_id: str,
    storage_path: str,
    file_type: Optional[str],
    *,
    filename: str = "",
    registered_by: str = "ingest",
) -> str:
    """Convert a tabular file to a registered DuckDB table. Returns its table id."""
    ext = (file_type or "").lower().lstrip(".")
    if not ext and "." in storage_path:
        ext = storage_path.rsplit(".", 1)[-1].lower()

    source_name = f"collection_{corpus_id}"
    out_dir = _extracts_dir() / source_name
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    base = _sanitize(filename or storage_path)[:24]
    # Append a slice of the unique file_id so two files whose names sanitize to
    # the same base (e.g. sales.csv + sales.xlsx, or two data.csv) don't collide
    # on the derived table — parquet path, _meta row, view, and table_registry id
    # all key off table_id, so a collision would silently overwrite the first.
    fid_suffix = (file_id or "").replace("cf_", "")[:8]
    table_id = f"{source_name}_{base}_{fid_suffix}" if fid_suffix else f"{source_name}_{base}"
    parquet_path = data_dir / f"{table_id}.parquet"

    con = _open_duckdb(":memory:")
    try:
        reader = _reader_expr(ext, storage_path)
        if reader is None:  # xlsx/xls
            try:
                con.execute("INSTALL excel; LOAD excel;")
                safe = str(storage_path).replace("'", "''")
                reader = f"read_xlsx('{safe}')"
            except Exception as exc:  # pragma: no cover - env-dependent
                raise UnsupportedTabular(f"xlsx needs the DuckDB excel extension (unavailable): {exc}") from exc
        safe_pq = str(parquet_path).replace("'", "''")
        con.execute(f"COPY (SELECT * FROM {reader}) TO '{safe_pq}' (FORMAT PARQUET)")
        rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{safe_pq}')").fetchone()[0]
    finally:
        con.close()

    size_bytes = parquet_path.stat().st_size

    ext_db = out_dir / "extract.duckdb"
    ec = _open_duckdb(str(ext_db))
    try:
        ec.execute(
            """CREATE TABLE IF NOT EXISTS _meta (
                table_name VARCHAR NOT NULL, description VARCHAR, rows BIGINT,
                size_bytes BIGINT, extracted_at TIMESTAMP,
                query_mode VARCHAR DEFAULT 'local'
            )"""
        )
        ec.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
        ec.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
            [table_id, f"Uploaded file in collection {corpus_id}", rows, size_bytes, datetime.now(timezone.utc)],
        )
        safe_name = table_id.replace('"', '""')
        safe_pq2 = str(parquet_path).replace("'", "''")
        ec.execute(f"CREATE OR REPLACE VIEW \"{safe_name}\" AS SELECT * FROM read_parquet('{safe_pq2}')")
    finally:
        ec.close()

    table_registry_repo().register(
        id=table_id,
        name=base,
        source_type="collection",
        bucket=corpus_id,
        source_table=table_id,
        query_mode="local",
        description=f"Uploaded file in collection {corpus_id}",
        registered_by=registered_by,
    )

    # Refresh the master views so the table is immediately queryable. Best-effort:
    # the durable contract (parquet + extract.duckdb + registry row) is already
    # written; a rebuild hiccup must not fail ingestion.
    try:
        from src.orchestrator import SyncOrchestrator

        SyncOrchestrator().rebuild_source(source_name)
    except Exception as exc:  # pragma: no cover - rebuild env-dependent
        logger.warning("rebuild_source(%s) after tabular ingest failed: %s", source_name, exc)

    return table_id
