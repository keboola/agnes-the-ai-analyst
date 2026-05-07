"""Incremental Keboola extraction — watermark, merge, and orchestration.

Mirrors internal repo's `src/data_sync.py:_incremental_single_file_sync`
(lines 366-527), simplified to a single-file (non-partitioned) flow.
Partitioned tables are handled by `connectors.keboola.partitioned`.

Pipeline per table:
  1. Read watermark (last_sync) from sync_state — caller's job.
  2. compute_changed_since: subtract incremental_window_days from last_sync,
     or use max_history_days for first sync.
  3. KeboolaClient.export_table(changed_since=...) — pulls only delta rows.
  4. If 0 rows, no-op (parquet untouched, log "no changes").
  5. If existing parquet, merge_parquet (concat → drop_duplicates by PK).
  6. If first sync, csv_to_parquet directly.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from connectors.keboola.parquet_io import (
    apply_schema_to_table,
    convert_date_columns_to_date32,
    csv_to_parquet,
    _convert_column,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 7


def compute_changed_since(
    *,
    last_sync: Optional[datetime],
    window_days: Optional[int],
    max_history_days: Optional[int],
    now: datetime,
) -> Optional[str]:
    """Compute the `changedSince` ISO timestamp to pass to Keboola Storage API.

    Args:
        last_sync: When this table was last successfully synced. None on first sync.
        window_days: Backtrack window applied to last_sync. None → DEFAULT_WINDOW_DAYS (7).
        max_history_days: Cap on first-sync history depth. None → unbounded (returns None).
        now: Current time, injected for testability.

    Returns:
        ISO 8601 timestamp string, or None when the first sync should download all rows.
    """
    if window_days is not None and window_days < 0:
        raise ValueError(f"window_days must be >= 0, got {window_days}")

    if last_sync is not None:
        win = window_days if window_days is not None else DEFAULT_WINDOW_DAYS
        return (last_sync - timedelta(days=win)).isoformat()

    if max_history_days is not None:
        return (now - timedelta(days=max_history_days)).isoformat()

    return None


def merge_parquet(
    *,
    existing_parquet: Path,
    new_csv: Path,
    primary_key: List[str],
    dtypes: Dict[str, str],
    date_columns: List[str],
    pyarrow_schema: Optional[pa.Schema],
) -> Dict[str, int]:
    """Merge a CSV delta into an existing parquet by primary key.

    Read existing parquet → load delta CSV with explicit dtypes → concat →
    drop_duplicates(subset=primary_key, keep='last') → write to a sibling
    `.tmp` → atomic rename. The .tmp lives in the same directory as the
    target so the rename is atomic on the same filesystem.

    No primary key = pure append (matches legacy behavior; logs a warning
    so operators notice the missing PK on a sync that needed dedup).

    Reuses the typed-schema helpers from `parquet_io.py` so the merged
    parquet has the same column types as a fresh `csv_to_parquet` write.

    NOTE on memory: this loads both existing parquet and delta CSV into
    pandas RAM. For tables in the multi-million-row range this may OOM.
    Switch to partitioned strategy for those tables (per-partition merge
    keeps memory bounded) — see `connectors.keboola.partitioned`.
    """
    existing_parquet = Path(existing_parquet)
    new_csv = Path(new_csv)

    existing_df = pq.read_table(existing_parquet).to_pandas()

    delta_df = pd.read_csv(new_csv, dtype=str)
    if dtypes:
        for col, dtype in dtypes.items():
            if col in delta_df.columns and "datetime" not in dtype:
                try:
                    delta_df[col] = _convert_column(delta_df[col], dtype, col_name=col)
                except Exception as e:
                    logger.warning("merge: failed to apply dtype %s to %r: %s", dtype, col, e)

    combined = pd.concat([existing_df, delta_df], ignore_index=True)

    if primary_key:
        combined = combined.drop_duplicates(subset=primary_key, keep="last")
        logger.info(
            "merge: %s, %d existing + %d delta rows → %d after dedup on %s",
            existing_parquet.name, len(existing_df), len(delta_df),
            len(combined), primary_key,
        )
    else:
        logger.warning(
            "merge: %s has no primary_key configured — appending without dedup. "
            "Duplicates between existing and delta will accumulate.",
            existing_parquet.name,
        )

    table = pa.Table.from_pandas(combined, preserve_index=False)
    if date_columns:
        table = convert_date_columns_to_date32(table, date_columns)
    if pyarrow_schema is not None:
        table = apply_schema_to_table(table, pyarrow_schema)

    tmp_path = existing_parquet.with_suffix(existing_parquet.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        pq.write_table(table, tmp_path, compression="snappy")
        os.replace(tmp_path, existing_parquet)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return {"rows": len(combined), "delta_rows": len(delta_df)}


def extract_incremental(
    *,
    table_config: Dict[str, Any],
    parquet_path: Path,
    last_sync: Optional[datetime],
    keboola_url: str,
    keboola_token: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Extract one Keboola table incrementally.

    `table_config` keys consumed: id, name, bucket, source_table,
    primary_key, incremental_window_days, max_history_days.

    Returns:
        {
            "rows": total rows in the parquet after merge,
            "delta_rows": rows in the delta export (may be 0),
            "changed_since_used": ISO string passed to Storage API
                (None on first sync without max_history_days),
        }
    """
    from connectors.keboola.client import KeboolaClient

    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    table_id = table_config.get("id") or (
        f"{table_config['bucket']}.{table_config['source_table']}"
    )
    primary_key = table_config.get("primary_key") or []
    if isinstance(primary_key, str):
        primary_key = [primary_key]

    now = now or datetime.now(timezone.utc)
    changed_since = compute_changed_since(
        last_sync=last_sync,
        window_days=table_config.get("incremental_window_days"),
        max_history_days=table_config.get("max_history_days"),
        now=now,
    )

    client = KeboolaClient(token=keboola_token, url=keboola_url)

    try:
        pyarrow_schema = client.get_pyarrow_schema(table_id)
    except Exception as e:
        logger.warning("Schema unavailable for %s: %s", table_id, e)
        pyarrow_schema = None

    try:
        dtypes = client.get_pandas_dtypes(table_id) if pyarrow_schema else {}
    except Exception:
        dtypes = {}

    try:
        date_columns = client.get_date_columns(table_id) if pyarrow_schema else []
    except Exception:
        date_columns = []

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = Path(tmp.name)

    try:
        export_info = client.export_table(
            table_id, csv_path, changed_since=changed_since,
        )
        delta_rows = export_info.get("exported_rows", 0)

        if delta_rows == 0:
            existing_rows = 0
            if parquet_path.exists():
                existing_rows = pq.read_metadata(parquet_path).num_rows
            logger.info("incremental: %s — no changes since %s", table_id, changed_since)
            return {
                "rows": existing_rows,
                "delta_rows": 0,
                "changed_since_used": changed_since,
            }

        if parquet_path.exists():
            merge_info = merge_parquet(
                existing_parquet=parquet_path,
                new_csv=csv_path,
                primary_key=primary_key,
                dtypes=dtypes,
                date_columns=date_columns,
                pyarrow_schema=pyarrow_schema,
            )
            return {
                "rows": merge_info["rows"],
                "delta_rows": delta_rows,
                "changed_since_used": changed_since,
            }
        else:
            csv_to_parquet(
                csv_path=csv_path,
                parquet_path=parquet_path,
                dtypes=dtypes,
                date_columns=date_columns,
                pyarrow_schema=pyarrow_schema,
                table_id=table_id,
            )
            rows = pq.read_metadata(parquet_path).num_rows
            return {
                "rows": rows,
                "delta_rows": delta_rows,
                "changed_since_used": changed_since,
            }
    finally:
        if csv_path.exists():
            csv_path.unlink()
