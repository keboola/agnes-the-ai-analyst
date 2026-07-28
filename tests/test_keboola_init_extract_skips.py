"""Verify the legacy Keboola download path skips materialized rows.

Materialized rows are handled by `_run_materialized_pass` in
`app/api/sync.py`, not by the legacy extractor. Mirror of the BQ
extractor's existing skip behavior at line 188.

The Keboola extractor entrypoint is `run()` (not `init_extract` like
the BQ extractor). Tests below observe the skip via caplog and the
stats dict (no parquet written, table not in tables_extracted/failed).
"""
from connectors.keboola import extractor as kbe


def test_run_skips_materialized_rows(tmp_path, caplog):
    """Given a registry with one materialized row, run() must NOT write a
    parquet for it and must NOT count it in tables_extracted/failed."""
    output_dir = tmp_path / "extracts" / "keboola"
    output_dir.mkdir(parents=True)

    table_configs = [
        {
            "id": "orders_recent",
            "name": "orders_recent",
            "source_query": "SELECT * FROM kbc.\"in.c-sales\".\"orders\" WHERE date > '2026-01-01'",
            "query_mode": "materialized",
        },
    ]

    with caplog.at_level("INFO"):
        # Use bogus URL/token — the extractor will fail to ATTACH the
        # extension (legacy client fallback also unavailable for the
        # materialized row, but materialized rows must be skipped before
        # any of that code runs).
        stats = kbe.run(
            str(output_dir), table_configs,
            keboola_url="https://invalid.example/",
            keboola_token="bogus",
        )

    # No parquet files for the materialized row.
    parquet = output_dir / "data" / "orders_recent.parquet"
    assert not parquet.exists(), "materialized row was written by legacy extractor"

    # The materialized row must NOT count as extracted or failed.
    assert stats["tables_extracted"] == 0
    assert stats["tables_failed"] == 0

    # Skip log line for ops visibility.
    assert "Skipping" in caplog.text and "materialized" in caplog.text


def test_run_processes_local_alongside_skipped_materialized(tmp_path, caplog):
    """Mixed registry: one local + one materialized. Materialized is
    skipped, local row attempts extraction (and fails because there's
    no real Keboola in this test, but that's a separate failure path —
    the materialized row must not appear in tables_failed)."""
    output_dir = tmp_path / "extracts" / "keboola"
    output_dir.mkdir(parents=True)

    table_configs = [
        {
            "id": "orders",
            "name": "orders",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
        {
            "id": "orders_recent",
            "name": "orders_recent",
            "source_query": "SELECT 1",
            "query_mode": "materialized",
        },
    ]

    with caplog.at_level("INFO"):
        stats = kbe.run(
            str(output_dir), table_configs,
            keboola_url="https://invalid.example/",
            keboola_token="bogus",
        )

    # The materialized row must not be in failures (it was skipped, not failed).
    failed_names = {e["table"] for e in stats.get("errors", [])}
    assert "orders_recent" not in failed_names, (
        "materialized row appeared in failures — should have been skipped"
    )
    # Skip log line for ops visibility.
    assert "Skipping" in caplog.text and "materialized" in caplog.text
