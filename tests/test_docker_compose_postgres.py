"""Static contract tests for docker-compose.postgres.yml.

Pins the fresh-volume boot path: ``app`` and ``scheduler`` gate on the
``data-migrate`` one-shot exiting 0, so on a brand-new ``data`` volume
(no ``system.duckdb`` yet) the one-shot MUST treat the missing source as
"nothing to migrate" — otherwise a fresh Postgres-backend deployment can
never boot compose from scratch. The CLI behavior itself is covered by
tests/test_migrate_cli_missing_source.py; these tests pin the wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def compose_pg() -> dict:
    root = Path(__file__).resolve().parent.parent
    return yaml.safe_load((root / "docker-compose.postgres.yml").read_text())


class TestDataMigrateFreshVolumeBoot:
    def test_data_migrate_tolerates_missing_source(self, compose_pg):
        cmd = compose_pg["services"]["data-migrate"]["command"]
        assert "--missing-source-ok" in cmd, (
            "data-migrate must pass --missing-source-ok: on a fresh data "
            "volume there is no system.duckdb yet, and without the flag the "
            "one-shot exits 2 and app/scheduler (gated on "
            "service_completed_successfully) never start"
        )

    def test_data_migrate_never_resets_target(self, compose_pg):
        cmd = compose_pg["services"]["data-migrate"]["command"]
        assert "--reset-target" not in cmd, (
            "data-migrate re-runs on every compose up — --reset-target would "
            "truncate live post-cutover data on every boot"
        )

    def test_app_and_scheduler_gate_on_data_migrate(self, compose_pg):
        """The dependency that makes the missing-source tolerance
        boot-critical: both runtime services block on data-migrate."""
        for svc in ("app", "scheduler"):
            dep = compose_pg["services"][svc]["depends_on"]["data-migrate"]
            assert dep["condition"] == "service_completed_successfully", (
                f"{svc} must gate on data-migrate completing successfully so "
                "the runtime never observes a partially-migrated PG"
            )
