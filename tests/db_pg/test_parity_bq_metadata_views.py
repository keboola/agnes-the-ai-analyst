"""Parity test for the BigQuery VIEW-hint lookup across both backends.

``app/api/query._view_targets_in`` enriches the ``remote_scan_too_large`` error
with "this is a VIEW, LIMIT won't push" by joining ``bq_metadata_cache`` against
``table_registry``. It previously ran that join on the always-DuckDB system
connection, so on a Postgres instance both tables came back empty and the hint
silently never fired. The fix resolves it through the repo factory
(``table_registry_repo()`` + ``bq_metadata_cache_repo()``).

These tests seed a VIEW and a BASE TABLE through the factory and assert
``_view_targets_in`` returns only the view's id — on DuckDB AND Postgres.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    """DATA_DIR + (DuckDB) fresh system DB, for either backend."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()
    return state_backend


def _seed_remote_bq(table_id: str, bucket: str, source_table: str, entity_type: str):
    """Register a remote BQ table + its metadata-cache entity_type via factory."""
    from src.repositories import bq_metadata_cache_repo, table_registry_repo

    table_registry_repo().register(
        id=table_id,
        name=table_id,
        source_type="bigquery",
        bucket=bucket,
        source_table=source_table,
        query_mode="remote",
    )
    bq_metadata_cache_repo().upsert_success(
        table_id,
        rows=None,
        size_bytes=None,
        partition_by=None,
        clustered_by=None,
        entity_type=entity_type,
    )


def test_view_targets_in_resolves_views_on_both_backends(_env):
    from app.api.query import _view_targets_in

    _seed_remote_bq("the_view", "buck", "v_tbl", "VIEW")
    _seed_remote_bq("the_matview", "buck", "mv_tbl", "MATERIALIZED VIEW")
    _seed_remote_bq("the_base", "buck", "base_tbl", "BASE TABLE")

    # dry_run_set rows are (bucket, source_table, <ignored>) triples.
    dry_run_set = [
        ("buck", "v_tbl", "x"),
        ("buck", "mv_tbl", "x"),
        ("buck", "base_tbl", "x"),
    ]
    result = set(_view_targets_in(dry_run_set))

    assert result == {"the_view", "the_matview"}, (
        f"[{_env}] expected only the VIEW / MATERIALIZED VIEW ids; got {result}. "
        f"On Postgres a raw-conn join would return an empty set."
    )


def test_view_targets_in_empty_when_only_base_tables(_env):
    from app.api.query import _view_targets_in

    _seed_remote_bq("base_only", "buck", "bt", "BASE TABLE")
    assert _view_targets_in([("buck", "bt", "x")]) == []
