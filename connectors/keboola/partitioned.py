"""Keboola partitioned sync — per-partition parquet files with per-partition merge.

Mirrors internal repo's `src/data_sync.py:529-1103` (partitioned sync
strategies) and `src/parquet_manager.py:425-600` (merge logic). Output
layout matches the legacy repo: flat directory of single files keyed by
partition string, e.g. `data/sales/2025_11.parquet`. Hive-style
(`year=2025/month=11/`) was rejected as overkill for the partition
counts we see (≤ 365 partitions/day-grain, ≤ 12 partitions/month-grain
per year).

The orchestrator exposes the directory as a single DuckDB view via
`read_parquet('<table>/*.parquet')` — to the analyst, a partitioned
table reads identically to a single-file table.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from connectors.keboola.parquet_io import (
    apply_schema_to_table,
    convert_date_columns_to_date32,
    csv_to_parquet,
    _convert_column,
)
from connectors.keboola.incremental import compute_changed_since

logger = logging.getLogger(__name__)

SUPPORTED_GRANULARITIES = frozenset({"day", "month", "year"})
DEFAULT_GRANULARITY = "month"
DEFAULT_INITIAL_LOAD_CHUNK_DAYS = 30
INITIAL_LOAD_OVERLAP_DAYS = 1
INITIAL_LOAD_MAX_CHUNKS_SAFETY = 120  # ~10 years at 30-day chunks
INITIAL_LOAD_EMPTY_CHUNKS_TO_STOP = 2


class InvalidPartitionConfigError(ValueError):
    """Partition config (column, granularity) is malformed."""


# ───────────────────────────── partition keying ───────────────────────────────


def partition_key_for(value: Any, granularity: str) -> str:
    """Return the partition-key string for a single date/timestamp value.

    Accepts `datetime.date`, `datetime.datetime`, and `pandas.Timestamp`.
    """
    if granularity not in SUPPORTED_GRANULARITIES:
        raise InvalidPartitionConfigError(
            f"granularity must be one of {sorted(SUPPORTED_GRANULARITIES)}, got {granularity!r}"
        )

    if isinstance(value, pd.Timestamp):
        d = value.to_pydatetime()
    elif isinstance(value, datetime):
        d = value
    elif isinstance(value, date):
        d = value
    else:
        raise InvalidPartitionConfigError(f"value must be date/datetime, got {type(value).__name__}")

    if granularity == "day":
        return d.strftime("%Y_%m_%d")
    if granularity == "month":
        return d.strftime("%Y_%m")
    return d.strftime("%Y")


# ───────────────────────────── per-partition merge ────────────────────────────


def merge_partition(
    *,
    partition_path: Path,
    delta_df: pd.DataFrame,
    primary_key: List[str],
    pyarrow_schema: Optional[pa.Schema],
    date_columns: List[str],
) -> Dict[str, int]:
    """Merge a delta DataFrame into a single partition parquet.

    If the partition file does not exist yet, this is the first batch for
    that partition — write it directly. Otherwise read existing → concat →
    drop_duplicates(PK, keep='last') → atomic write.

    Caller has already type-converted `delta_df` (pandas dtypes from
    Keboola metadata via `process_csv_to_partitions`); `apply_schema_to_table`
    runs at write time to enforce the explicit PyArrow schema.
    """
    partition_path = Path(partition_path)
    partition_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-normalize date columns in delta_df to pandas datetime so the concat
    # with existing_df (which carries datetime64 from the typed parquet) doesn't
    # produce a mixed object/str column that pa.Table.from_pandas can't reduce
    # back to date32.
    delta_df = delta_df.copy()
    for col in date_columns or []:
        if col in delta_df.columns:
            delta_df[col] = pd.to_datetime(delta_df[col], errors="coerce")

    if partition_path.exists():
        existing_df = pq.read_table(partition_path).to_pandas()
        combined = pd.concat([existing_df, delta_df], ignore_index=True)
        if primary_key:
            combined = combined.drop_duplicates(subset=primary_key, keep="last")
    else:
        combined = delta_df

    table = pa.Table.from_pandas(combined, preserve_index=False)
    if date_columns:
        table = convert_date_columns_to_date32(table, date_columns)
    if pyarrow_schema is not None:
        table = apply_schema_to_table(table, pyarrow_schema)

    tmp_path = partition_path.with_suffix(partition_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        pq.write_table(table, tmp_path, compression="snappy")
        os.replace(tmp_path, partition_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return {"rows": len(combined), "delta_rows": len(delta_df)}


# ───────────────────────────── CSV → partition groups ─────────────────────────


def process_csv_to_partitions(
    *,
    csv_path: Path,
    partition_by: str,
    granularity: str,
    dtypes: Dict[str, str],
) -> Dict[str, pd.DataFrame]:
    """Split a delta CSV into a `{partition_key: DataFrame}` map.

    Loads with `dtype=str`, applies dtypes via `_convert_column`, parses
    `partition_by` column as date, computes `partition_key_for` per row,
    groups. Rows with unparseable partition values are dropped with a
    warning (matches legacy: don't drop the whole sync over a few bad
    rows; admin sees them in the log).
    """
    if granularity not in SUPPORTED_GRANULARITIES:
        raise InvalidPartitionConfigError(
            f"granularity must be one of {sorted(SUPPORTED_GRANULARITIES)}"
        )

    df = pd.read_csv(csv_path, dtype=str)
    if df.empty:
        return {}

    if partition_by not in df.columns:
        raise InvalidPartitionConfigError(
            f"partition_by column {partition_by!r} not present in CSV "
            f"(columns: {list(df.columns)})"
        )

    if dtypes:
        for col, dtype in dtypes.items():
            if col not in df.columns or "datetime" in dtype:
                continue
            try:
                df[col] = _convert_column(df[col], dtype, col_name=col)
            except Exception as e:
                logger.warning("partition: failed to apply dtype %s to %r: %s", dtype, col, e)

    parsed = pd.to_datetime(df[partition_by], errors="coerce")
    invalid_count = int(parsed.isna().sum())
    if invalid_count > 0:
        logger.warning(
            "partition: %d rows with unparseable %r values dropped from this delta",
            invalid_count, partition_by,
        )
    df = df.assign(_partition_dt=parsed)
    df = df.dropna(subset=["_partition_dt"])

    if df.empty:
        return {}

    df["_partition_key"] = df["_partition_dt"].apply(
        lambda v: partition_key_for(v, granularity)
    )
    df = df.drop(columns=["_partition_dt"])

    groups: Dict[str, pd.DataFrame] = {}
    for key, group_df in df.groupby("_partition_key", sort=False):
        groups[str(key)] = group_df.drop(columns=["_partition_key"]).reset_index(drop=True)
    return groups


# ───────────────────────────── chunked initial load windows ───────────────────


def compute_chunk_windows(
    *,
    now: datetime,
    chunk_days: int,
    max_history_days: Optional[int],
    overlap_days: int = INITIAL_LOAD_OVERLAP_DAYS,
) -> List[Tuple[str, str]]:
    """Compute the list of (changed_since, changed_until) ISO pairs for a
    chunked initial load.

    Walks history backwards: chunk[0] = (now-chunk_days, now), chunk[1] =
    (now-2*chunk_days-overlap, now-chunk_days+overlap), etc. The
    `+overlap` on the upper boundary deliberately re-fetches one day from
    the previous chunk so boundary rows aren't lost; the caller dedupes
    after.

    When `max_history_days` is None, returns up to
    `INITIAL_LOAD_MAX_CHUNKS_SAFETY` chunks; the caller stops earlier if
    two consecutive chunks return zero rows.
    """
    if max_history_days is not None:
        if max_history_days <= 0:
            return []
        num_chunks = (max_history_days + chunk_days - 1) // chunk_days
    else:
        num_chunks = INITIAL_LOAD_MAX_CHUNKS_SAFETY

    windows: List[Tuple[str, str]] = []
    for i in range(num_chunks):
        chunk_end_offset = i * chunk_days
        chunk_start_offset = chunk_end_offset + chunk_days + overlap_days
        if i == 0:
            chunk_until = now
        else:
            chunk_until = now - timedelta(days=chunk_end_offset - overlap_days)
        chunk_since = now - timedelta(days=chunk_start_offset)
        windows.append((chunk_since.isoformat(), chunk_until.isoformat()))
    return windows


# ───────────────────────────── orchestrator ───────────────────────────────────


def extract_partitioned(
    *,
    table_config: Dict[str, Any],
    output_dir: Path,
    last_sync: Optional[datetime],
    keboola_url: str,
    keboola_token: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Extract one Keboola table into per-partition parquet files.

    `output_dir` is `data/<table>/`. The function writes
    `<output_dir>/<partition_key>.parquet` files; the orchestrator then
    creates a DuckDB view via `read_parquet('<output_dir>/*.parquet')`.

    Branches:
    - First sync (last_sync=None): chunked initial load via
      `compute_chunk_windows`, walking history backwards. Stops after 2
      consecutive empty chunks or `max_history_days` reached.
    - Subsequent sync: single delta pull with `changedSince`, group by
      partition key, per-affected-partition merge.
    """
    from connectors.keboola.client import KeboolaClient

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    granularity = table_config.get("partition_granularity") or DEFAULT_GRANULARITY
    if granularity not in SUPPORTED_GRANULARITIES:
        raise InvalidPartitionConfigError(
            f"partition_granularity must be one of {sorted(SUPPORTED_GRANULARITIES)}, got {granularity!r}"
        )
    partition_by = table_config.get("partition_by")
    if not partition_by:
        raise InvalidPartitionConfigError("partition_by must be set for partitioned strategy")

    table_id = table_config.get("id") or (
        f"{table_config['bucket']}.{table_config['source_table']}"
    )
    primary_key = table_config.get("primary_key") or []
    if isinstance(primary_key, str):
        primary_key = [primary_key]

    now = now or datetime.now(timezone.utc)

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

    if last_sync is None:
        return _first_sync_chunked(
            client=client,
            table_id=table_id,
            output_dir=output_dir,
            partition_by=partition_by,
            granularity=granularity,
            primary_key=primary_key,
            dtypes=dtypes,
            date_columns=date_columns,
            pyarrow_schema=pyarrow_schema,
            chunk_days=table_config.get("initial_load_chunk_days") or DEFAULT_INITIAL_LOAD_CHUNK_DAYS,
            max_history_days=table_config.get("max_history_days"),
            now=now,
        )
    return _incremental_sync(
        client=client,
        table_id=table_id,
        output_dir=output_dir,
        partition_by=partition_by,
        granularity=granularity,
        primary_key=primary_key,
        dtypes=dtypes,
        date_columns=date_columns,
        pyarrow_schema=pyarrow_schema,
        last_sync=last_sync,
        window_days=table_config.get("incremental_window_days"),
        max_history_days=table_config.get("max_history_days"),
        now=now,
    )


def _first_sync_chunked(
    *,
    client,
    table_id: str,
    output_dir: Path,
    partition_by: str,
    granularity: str,
    primary_key: List[str],
    dtypes: Dict[str, str],
    date_columns: List[str],
    pyarrow_schema: Optional[pa.Schema],
    chunk_days: int,
    max_history_days: Optional[int],
    now: datetime,
) -> Dict[str, Any]:
    windows = compute_chunk_windows(
        now=now, chunk_days=chunk_days, max_history_days=max_history_days,
    )

    total_rows = 0
    consecutive_empty = 0
    chunks_run = 0

    for since, until in windows:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            csv_path = Path(tmp.name)
        try:
            export_info = client.export_table(
                table_id, csv_path, changed_since=since, changed_until=until,
            )
            chunks_run += 1
            rows_in_chunk = export_info.get("exported_rows", 0)
            if rows_in_chunk == 0:
                consecutive_empty += 1
                if max_history_days is None and consecutive_empty >= INITIAL_LOAD_EMPTY_CHUNKS_TO_STOP:
                    logger.info(
                        "Initial load: %d consecutive empty chunks, stopping (table_id=%s)",
                        consecutive_empty, table_id,
                    )
                    break
                continue
            consecutive_empty = 0
            groups = process_csv_to_partitions(
                csv_path=csv_path, partition_by=partition_by,
                granularity=granularity, dtypes=dtypes,
            )
            for partition_key, group_df in groups.items():
                merge_partition(
                    partition_path=output_dir / f"{partition_key}.parquet",
                    delta_df=group_df, primary_key=primary_key,
                    pyarrow_schema=pyarrow_schema, date_columns=date_columns,
                )
                total_rows += len(group_df)
        finally:
            if csv_path.exists():
                csv_path.unlink()

    return {
        "rows": _count_total_rows(output_dir),
        "delta_rows": total_rows,
        "chunks_run": chunks_run,
        "partitions_written": len(list(output_dir.glob("*.parquet"))),
        "changed_since_used": None,
    }


def _incremental_sync(
    *,
    client,
    table_id: str,
    output_dir: Path,
    partition_by: str,
    granularity: str,
    primary_key: List[str],
    dtypes: Dict[str, str],
    date_columns: List[str],
    pyarrow_schema: Optional[pa.Schema],
    last_sync: datetime,
    window_days: Optional[int],
    max_history_days: Optional[int],
    now: datetime,
) -> Dict[str, Any]:
    changed_since = compute_changed_since(
        last_sync=last_sync, window_days=window_days,
        max_history_days=max_history_days, now=now,
    )

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = Path(tmp.name)
    try:
        export_info = client.export_table(
            table_id, csv_path, changed_since=changed_since,
        )
        delta_rows = export_info.get("exported_rows", 0)
        if delta_rows == 0:
            return {
                "rows": _count_total_rows(output_dir),
                "delta_rows": 0,
                "partitions_touched": 0,
                "changed_since_used": changed_since,
            }
        groups = process_csv_to_partitions(
            csv_path=csv_path, partition_by=partition_by,
            granularity=granularity, dtypes=dtypes,
        )
        for partition_key, group_df in groups.items():
            merge_partition(
                partition_path=output_dir / f"{partition_key}.parquet",
                delta_df=group_df, primary_key=primary_key,
                pyarrow_schema=pyarrow_schema, date_columns=date_columns,
            )
        return {
            "rows": _count_total_rows(output_dir),
            "delta_rows": delta_rows,
            "partitions_touched": len(groups),
            "changed_since_used": changed_since,
        }
    finally:
        if csv_path.exists():
            csv_path.unlink()


def _count_total_rows(output_dir: Path) -> int:
    total = 0
    for p in output_dir.glob("*.parquet"):
        try:
            total += pq.read_metadata(p).num_rows
        except Exception:
            continue
    return total
