"""Before/after benchmark for Jira issues parquet bloom filters (issue #749).

Compares the current pyarrow writer (min/max statistics + page index, no bloom
filters — pyarrow 23.x exposes no bloom-write API) against a DuckDB ``COPY``
writer that keeps ``issue_key`` dictionary-encoded so DuckDB emits a per-row-group
bloom filter for it.

The benchmark builds a hive-partitioned ``issues`` dataset (one parquet per
``month=YYYY-MM``) of representative shape, then times the acceptance-criterion
query — a single ``issue_key`` point lookup that spans every month — against each
variant, and reports write time, on-disk size, and bloom-filter presence.

Run:

    python -m connectors.jira.scripts.bloom_benchmark
    python -m connectors.jira.scripts.bloom_benchmark --months 36 --per-month 1200 --repeats 50

No Jira credentials or network access required — the corpus is synthetic but
mirrors ``ISSUES_SCHEMA`` (unique high-cardinality ``issue_key``, long ADF-style
text, timestamps, low-cardinality categoricals).
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import duckdb
import pandas as pd

from connectors.jira.transform import (
    PARQUET_WRITE_OPTIONS,
    apply_schema,
    issues_schema,
    write_hive_parquet,
)

# DuckDB COPY options that keep issue_key dictionary-encoded (so a bloom filter
# is written for it) while matching the pyarrow writer's compression. The
# dictionary-size limit is generous enough that a month's worth of unique
# issue_keys stays dictionary-encoded rather than falling back to PLAIN (which
# is what drops the bloom filter), but bounded so genuinely huge text columns
# still fall back to PLAIN. See bloom_benchmark for the size/latency trade-off.
BLOOM_DICTIONARY_SIZE_LIMIT = 512 * 1024  # 512 KiB per row group


def _synthetic_month(month_key: str, n: int, start_id: int) -> pd.DataFrame:
    """A month's worth of synthetic issues shaped like real Jira output."""
    statuses = ["Open", "In Progress", "Waiting for Customer", "Resolved", "Closed"]
    priorities = ["Lowest", "Low", "Medium", "High", "Highest"]
    types = ["Bug", "Task", "Incident", "Service Request"]
    body = (
        "Customer reports an issue with the export pipeline. Steps to reproduce: "
        "open the project, run the job, observe the failure in the logs. "
    ) * 6
    ts = f"{month_key}-15T12:00:00+00:00"
    rows = []
    for i in range(n):
        gid = start_id + i
        rows.append(
            {
                "issue_key": f"SUPPORT-{gid}",
                "issue_id": str(100000 + gid),
                "summary": f"Issue {gid}: export failure in project P{gid % 200}",
                "description": body,
                "issue_type": types[gid % len(types)],
                "status": statuses[gid % len(statuses)],
                "priority": priorities[gid % len(priorities)],
                "project_key": f"P{gid % 200}",
                "reporter_email": f"user{gid % 5000}@example.com",
                "created_at": ts,
                "updated_at": ts,
                "labels": '["export","pipeline"]',
                "attachment_count": gid % 4,
                "comment_count": gid % 12,
            }
        )
    return pd.DataFrame(rows)


def _write_duckdb_bloom(table, table_dir: Path, month_key: str) -> Path:
    """Write one month via DuckDB COPY with a bloom filter on issue_key."""
    hive_dir = table_dir / f"month={month_key}"
    hive_dir.mkdir(parents=True, exist_ok=True)
    dest = hive_dir / "data.parquet"
    con = duckdb.connect()
    try:
        con.register("t", table)
        con.execute(
            f"COPY t TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD, "
            f"DICTIONARY_SIZE_LIMIT {BLOOM_DICTIONARY_SIZE_LIMIT})"
        )
    finally:
        con.close()
    return dest


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*.parquet"))


def _bloom_report(root: Path) -> tuple[int, int]:
    """(row groups with a bloom filter on issue_key, total row groups)."""
    con = duckdb.connect()
    try:
        glob = str(root / "**" / "*.parquet")
        rows = con.execute(
            "SELECT count(*) FILTER (bloom_filter_offset IS NOT NULL), count(*) "
            "FROM parquet_metadata(?) WHERE path_in_schema = 'issue_key'",
            [glob],
        ).fetchone()
    finally:
        con.close()
    return int(rows[0]), int(rows[1])


def _median_ms(root: Path, sql: str, params_seq: list[list], repeats: int) -> float:
    """Median wall-clock (ms) of a query, object cache disabled between runs."""
    con = duckdb.connect()
    con.execute("PRAGMA disable_object_cache")
    glob = str(root / "**" / "*.parquet")
    stmt = sql.format(glob=glob)
    samples = []
    try:
        for i in range(repeats):
            params = params_seq[i % len(params_seq)]
            t0 = time.perf_counter()
            con.execute(stmt, params).fetchall()
            samples.append((time.perf_counter() - t0) * 1000)
    finally:
        con.close()
    samples.sort()
    return samples[len(samples) // 2]


# Representative selective query shapes an analyst runs against the extract.
QUERY_SHAPES = {
    "point lookup (hit/month)": "SELECT issue_key, status FROM read_parquet('{glob}') WHERE issue_key = ?",
    "absent key": "SELECT issue_key, status FROM read_parquet('{glob}') WHERE issue_key = ?",
    "IN-list (5 hits)": "SELECT issue_key, status FROM read_parquet('{glob}') WHERE issue_key IN (?,?,?,?,?)",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--per-month", type=int, default=800)
    ap.add_argument("--repeats", type=int, default=40)
    ap.add_argument(
        "--workdir",
        type=Path,
        default=Path("/tmp/jira_bloom_bench"),
        help="Scratch dir (wiped on start).",
    )
    args = ap.parse_args()

    if args.workdir.exists():
        shutil.rmtree(args.workdir)
    baseline_dir = args.workdir / "baseline_pyarrow"
    bloom_dir = args.workdir / "bloom_duckdb"

    schema = issues_schema()
    tables = []
    lookup_keys = []
    start_id = 1
    for m in range(args.months):
        year = 2024 + m // 12
        month = m % 12 + 1
        month_key = f"{year}-{month:02d}"
        df = _synthetic_month(month_key, args.per_month, start_id)
        # Pick a lookup key from the *middle* of each month so a hit exists in
        # every partition — the worst case for hive/min-max pruning.
        lookup_keys.append(f"SUPPORT-{start_id + args.per_month // 2}")
        start_id += args.per_month
        tables.append((month_key, apply_schema(df, schema)))

    total_rows = args.months * args.per_month
    print(
        f"Corpus: {args.months} months x {args.per_month} issues = {total_rows} rows "
        f"across {args.months} hive partitions\n"
    )

    # --- Baseline: current pyarrow writer -----------------------------------
    t0 = time.perf_counter()
    for month_key, table in tables:
        write_hive_parquet(table, baseline_dir, month_key)
    baseline_write_ms = (time.perf_counter() - t0) * 1000

    # --- Candidate: DuckDB COPY with bloom on issue_key ---------------------
    t0 = time.perf_counter()
    for month_key, table in tables:
        _write_duckdb_bloom(table, bloom_dir, month_key)
    bloom_write_ms = (time.perf_counter() - t0) * 1000

    base_bloom = _bloom_report(baseline_dir)
    cand_bloom = _bloom_report(bloom_dir)
    base_size = _dir_size(baseline_dir)
    cand_size = _dir_size(bloom_dir)

    # Params per shape: one existing key per month, one absent key, five hits.
    absent = [["SUPPORT-999999999"]]
    # Pad to exactly 5 keys for the 5-placeholder IN query. `lookup_keys * 5`
    # alone yields 10/15/20 entries for 2/3/4 months → DuckDB param-count
    # mismatch; slice back to 5 (#931 review).
    in_hits = [lookup_keys[:5]] if len(lookup_keys) >= 5 else [(lookup_keys * 5)[:5]]
    shape_params = {
        "point lookup (hit/month)": [[k] for k in lookup_keys],
        "absent key": absent,
        "IN-list (5 hits)": in_hits,
    }

    def mb(b: int) -> float:
        return b / 1024 / 1024

    print(f"{'metric':<30}{'pyarrow (baseline)':>22}{'duckdb+bloom':>18}{'delta':>10}")
    print("-" * 80)
    print(f"{'issue_key bloom filters':<30}{f'{base_bloom[0]}/{base_bloom[1]} rgs':>22}{f'{cand_bloom[0]}/{cand_bloom[1]} rgs':>18}{'':>10}")
    print(f"{'write time (ms)':<30}{baseline_write_ms:>22.1f}{bloom_write_ms:>18.1f}{f'{bloom_write_ms/baseline_write_ms:.1f}x':>10}")
    print(f"{'on-disk size (MB)':<30}{mb(base_size):>22.2f}{mb(cand_size):>18.2f}{f'{(cand_size/base_size-1)*100:+.0f}%':>10}")
    print("-" * 80)
    print("query median (ms), object cache disabled:")
    for name, sql in QUERY_SHAPES.items():
        b = _median_ms(baseline_dir, sql, shape_params[name], args.repeats)
        c = _median_ms(bloom_dir, sql, shape_params[name], args.repeats)
        print(f"  {name:<28}{b:>22.3f}{c:>18.3f}{f'{b/c:.2f}x':>10}")


if __name__ == "__main__":
    main()
