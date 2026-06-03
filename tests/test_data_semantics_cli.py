"""End-to-end tests for `agnes admin data-semantics generate`.

Seeds a real system DuckDB (via ``get_system_db()`` with ``DATA_DIR`` pointed at
a temp dir, which auto-initialises the schema) through the repositories, then
exercises the assembly grouping and the CLI command — proving the engine wires
to the live catalog, not just to hand-built fixtures.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _seed(conn) -> None:
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository
    from src.repositories.column_metadata import ColumnMetadataRepository
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.metrics import MetricRepository
    from src.repositories.table_registry import TableRegistryRepository

    pkgs = DataPackagesRepository(conn)
    pid = pkgs.create(
        name="Engagement", slug="engagement", description="UI events.",
        icon=None, color=None, created_by="tester",
    )

    reg = TableRegistryRepository(conn)
    reg.register(
        id="e1_events", name="E1 — Events", source_type="bigquery",
        bucket="analytics", source_table="E1_events",
        bq_fqn="proj.analytics.E1_events", partition_by="event_date",
    )
    reg.update_docs(
        "e1_events", grain="1 row per UI event",
        gotchas=[{"key": True, "body": "Always filter event_date."}],
    )
    pkgs.add_table(pid, "e1_events", added_by="tester")

    cols = ColumnMetadataRepository(conn)
    cols.save("e1_events", "event_id", basetype="STRING", description="Unique key")
    cols.save("e1_events", "event_date", basetype="DATE", description="Partition key")

    BqMetadataCacheRepository(conn).upsert_success(
        "e1_events", rows=1000, size_bytes=1, partition_by="event_date",
        clustered_by=["country_code", "platform"],
        known_columns=["event_id", "event_date"],
    )

    metrics = MetricRepository(conn)
    metrics.create(
        id="engagement/clicks", name="clicks", display_name="Clicks",
        category="engagement", sql="SELECT COUNT(*) FROM e1",
        description="Total clicks.", type="count", unit="count", grain="event",
        tables=["e1_events"], synonyms=["clicks", "click count"],
    )
    # An orphan metric whose table is in no package — must be dropped + warned.
    metrics.create(
        id="finance/revenue", name="revenue", display_name="Revenue",
        category="finance", sql="SELECT 1", table_name="ledger",
    )


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from src.db import get_system_db

    conn = get_system_db()
    _seed(conn)
    return tmp_path


def test_assemble_inputs_groups_tables_and_metrics(seeded):
    from cli.commands.admin_data_semantics import _assemble_inputs
    from src.db import get_system_db

    conn = get_system_db()
    try:
        inputs, notes = _assemble_inputs(conn)
    finally:
        conn.close()

    pkgs = {p["slug"]: p for p in inputs["packages"]}
    assert "engagement" in pkgs
    eng = pkgs["engagement"]
    assert [t["id"] for t in eng["tables"]] == ["e1_events"]
    table = eng["tables"][0]
    assert {c["column_name"] for c in table["columns"]} == {"event_id", "event_date"}
    assert table["bq_cache"]["clustered_by"] == ["country_code", "platform"]
    assert [m["name"] for m in eng["metrics"]] == ["clicks"]  # only the in-package metric
    # The orphan metric is reported, not silently dropped.
    assert any("belong to no data package" in n for n in notes)


def test_cli_generate_dry_run_then_write_then_check(seeded):
    from cli.commands.admin_data_semantics import admin_data_semantics_app

    out = seeded / "pack"
    runner = CliRunner()

    # --dry-run --json: prints the would-be files, writes nothing.
    r = runner.invoke(admin_data_semantics_app, ["generate", str(out), "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    files = json.loads(r.output)
    assert "engagement/tables/e1_events.yml" in files
    assert "engagement/metrics/clicks.yml" in files
    assert "engagement/_brief.md" in files
    assert not out.exists()

    # Write the pack.
    rw = runner.invoke(admin_data_semantics_app, ["generate", str(out)])
    assert rw.exit_code == 0, rw.output
    assert (out / "engagement" / "tables" / "e1_events.yml").is_file()
    assert (out / "engagement" / "metrics" / "clicks.yml").is_file()

    # A freshly-written pack is in sync → --check exits 0 (timestamp ignored).
    rc = runner.invoke(admin_data_semantics_app, ["generate", str(out), "--check"])
    assert rc.exit_code == 0, rc.output


def test_cli_check_detects_drift_before_write(seeded):
    from cli.commands.admin_data_semantics import admin_data_semantics_app

    out = seeded / "pack"
    # Nothing written yet → --check must fail (exit 1).
    r = CliRunner().invoke(admin_data_semantics_app, ["generate", str(out), "--check"])
    assert r.exit_code == 1
