# Jira parquet bloom filters — benchmark & decision (#749)

**Status:** evidence-based **won't-do** (measured 2026-07-20). Reproducible via
`python -m connectors.jira.scripts.bloom_benchmark`.

## Background

#406 asked for hive partitioning + bloom filters, with before/after benchmarks
as an acceptance criterion. PR #665 shipped the hive layout, ZSTD compression,
and `write_statistics=True` (column min/max + page index) but **no actual bloom
filters and no benchmarks**; the issue auto-closed on merge, leaving the
remainder untracked (→ #749).

This note supplies the missing benchmarks and records the decision.

## Can we even write bloom filters?

- **pyarrow (pinned 23.0.1):** no. Neither `write_table` nor `ParquetWriter`
  exposes a bloom-filter write option in the Python surface (Arrow C++ supports
  it; the binding does not). Verified against the installed signatures.
- **DuckDB (pinned ≥1.5.2, already a core dep):** yes, via `COPY … (FORMAT
  PARQUET)` — **but only for dictionary-encoded columns**. `issue_key` is unique
  and high-cardinality, so DuckDB falls back to PLAIN encoding and writes **no**
  bloom filter for it unless we force it to stay dictionary-encoded with
  `DICTIONARY_SIZE_LIMIT`. Forcing dictionary encoding on a unique column is
  what drives the file-size and write-time cost below.

## Benchmark

Synthetic corpus shaped like `ISSUES_SCHEMA` (unique `issue_key`, long ADF-style
text, timestamps, low-cardinality categoricals), written in the production
hive-partitioned layout (one parquet per `month=YYYY-MM`).

Command: `python -m connectors.jira.scripts.bloom_benchmark --months 36 --per-month 20000 --repeats 60`
(720 000 issues across 36 monthly partitions):

| metric | pyarrow (today) | DuckDB + bloom | delta |
|---|---|---|---|
| `issue_key` bloom filters | 0/36 rgs | **36/36 rgs** | — |
| write time | 1 827 ms | 21 008 ms | **11.5× slower** |
| on-disk size | 6.39 MB | 9.37 MB | **+47 %** |
| point lookup (hit, spans all months) | 8.861 ms | 8.844 ms | **1.00×** |
| absent key | 10.230 ms | 9.104 ms | 1.12× |
| IN-list (5 hits) | 23.345 ms | 23.368 ms | **1.00×** |

Supplementary probe on a single 1 M-row file with 20 row groups (warm cache,
not our layout): bloom gives up to **1.6×** on a present-key lookup and ~1.0× on
an absent key. This is the *best* case and it still doesn't survive the real
layout.

## Why the win doesn't materialize

1. **Hive partitioning by created-month + min/max stats + page index already
   prune** most row groups for our selective queries. On the real layout each
   month is one row group in its own file; the fixed cost is reading 36 footers,
   which bloom doesn't reduce.
2. Bloom filters only help *after* a row group survives min/max pruning — for
   `issue_key` lookups there is little left for them to skip.
3. Getting a bloom filter onto the unique `issue_key` column requires forcing
   dictionary encoding, which inflates files by ~47 % and writes ~11× slower —
   the opposite of the compression win #665 delivered.

## Decision

**Do not add bloom filters** to the Jira parquet writer. The existing hive
layout + `write_statistics` + `write_page_index` capture the selective-query win
for our data volumes and query shapes; bloom filters would trade a large,
permanent size/write-time regression for a ≤1.12× query change on the real
layout.

Revisit if either holds:
- pyarrow exposes a bloom-filter write API (avoids the dictionary-encoding tax),
  **and** the extract grows large enough that per-file min/max pruning stops
  isolating the target partition; or
- query shapes shift toward high-selectivity lookups on a **non**-partition,
  non-monotonic key where min/max stats are useless.

Re-run `connectors/jira/scripts/bloom_benchmark.py` against the then-current
extract to re-decide with fresh numbers.
