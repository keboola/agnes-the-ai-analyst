"""H1-NEW — start_migration refuses to queue a migration FROM a PG
backend when the source URL is missing/None. Prevents a class of
silent rollbacks that left instance.yaml on backend=cloud with no url
(manual YAML repair required)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException


def test_start_migration_refuses_when_pg_source_has_no_url(
    tmp_path: Path, monkeypatch
) -> None:
    """When current backend is cloud/side_car but instance.yaml's
    database.url is missing or unparseable (post-B2-NEW: parse error
    → DUCKDB,None per read_backend_state) AND the operator tries to
    migrate away from that backend, the API must refuse with a clear
    409/400 — not queue a job that will fail mid-migrator with a
    cryptic '--source-url is required'."""
    from app.api import db_state
    from src import db_state_machine as _sm

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jobs_dir = state_dir / "db-jobs"
    jobs_dir.mkdir()
    instance_yaml = state_dir / "instance.yaml"
    # backend=cloud but no url key — the operator manually edited or
    # an earlier failure left the overlay incomplete.
    instance_yaml.write_text("database:\n  backend: cloud\n")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpw")
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(_sm, "_OVERLAY_PATH", instance_yaml)
    monkeypatch.setattr(_sm, "_LOCK_PATH", state_dir / "migration.lock")

    with pytest.raises(HTTPException) as exc:
        db_state.start_migration(
            payload=db_state.MigrateRequest(target="side_car"),
        )
    # Either 400 (bad-state in request) or 409 (state conflict) is
    # acceptable; the detail must mention source_url so the
    # operator knows what's wrong.
    assert exc.value.status_code in (400, 409)
    assert "source" in str(exc.value.detail).lower() and "url" in str(exc.value.detail).lower()

    # And no pending job file got written.
    assert list(jobs_dir.glob("*.json")) == []


def test_start_migration_allows_duckdb_source_without_url(
    tmp_path: Path, monkeypatch
) -> None:
    """DuckDB source legitimately has no url. Refusal must apply only
    to PG sources."""
    from app.api import db_state
    from src import db_state_machine as _sm

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jobs_dir = state_dir / "db-jobs"
    jobs_dir.mkdir()
    instance_yaml = state_dir / "instance.yaml"
    instance_yaml.write_text("database:\n  backend: duckdb\n")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(_sm, "_OVERLAY_PATH", instance_yaml)
    monkeypatch.setattr(_sm, "_LOCK_PATH", state_dir / "migration.lock")

    with patch("app.api.db_state._resolve_host",
               lambda h: {"8.8.8.8"} if h == "cloud.example" else set()):
        out = db_state.start_migration(
            payload=db_state.MigrateRequest(
                target="cloud",
                cloud_url="postgresql+psycopg://u:p@cloud.example:5432/agnes",
            ),
        )
    assert out["status"] == "pending"
