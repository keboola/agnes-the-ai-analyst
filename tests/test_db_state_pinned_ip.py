"""B1-NEW — start_migration pins the resolved IP into the job JSON
at validation time, so a DNS rebind between queue and connect cannot
flip the target."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _setup_state(tmp_path: Path, monkeypatch, backend_yaml: str):
    """Common fixture helper: wire tmp_path as DATA_DIR, write instance.yaml,
    and monkeypatch the state-machine module's overlay + lock paths."""
    import src.db_state_machine as _sm
    from app.api import db_state

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jobs_dir = state_dir / "db-jobs"
    jobs_dir.mkdir()
    instance_yaml = state_dir / "instance.yaml"
    instance_yaml.write_text(backend_yaml)
    lock_path = state_dir / "db-migration.lock"

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(_sm, "_OVERLAY_PATH", instance_yaml)
    monkeypatch.setattr(_sm, "_LOCK_PATH", lock_path)

    return jobs_dir


def test_target_url_pinned_ip_recorded_in_job_json(tmp_path: Path, monkeypatch) -> None:
    """When the user POSTs a cloud target whose hostname resolves to a
    public IP, the queued job JSON must carry both the hostname URL
    AND a pinned-IP URL for the applier to use at connect time."""
    from app.api import db_state

    jobs_dir = _setup_state(
        tmp_path,
        monkeypatch,
        "database:\n  backend: duckdb\n",
    )

    with patch("app.api.db_state._resolve_host",
               lambda h: {"8.8.8.8"} if h == "cloud.example.com" else set()):
        out = db_state.start_migration(
            payload=db_state.MigrateRequest(
                target="cloud",
                cloud_url="postgresql+psycopg://u:p@cloud.example.com:5432/agnes",
            )
        )

    job_id = out["job_id"]
    raw = json.loads((jobs_dir / f"{job_id}.json").read_text())

    # The display URL keeps the hostname so operators see what they posted.
    assert "cloud.example.com" in raw["target_url"]
    # The pinned URL substitutes the resolved IP so the migrator dials
    # the host that was validated, not a re-resolved one.
    assert "target_url_pinned_ip" in raw, raw
    assert "8.8.8.8" in raw["target_url_pinned_ip"]
    assert "cloud.example.com" not in raw["target_url_pinned_ip"]


def test_pg_source_url_also_pinned(tmp_path: Path, monkeypatch) -> None:
    """For side_car → cloud migrations, the source PG URL also gets
    pinned (the side-car compose hostname resolves locally; pinning
    locks the migrator to that resolution)."""
    from app.api import db_state

    jobs_dir = _setup_state(
        tmp_path,
        monkeypatch,
        "database:\n  backend: side_car\n"
        "  url: postgresql+psycopg://u:p@postgres:5432/agnes\n",
    )

    def _fake_resolve(h: str) -> set[str]:
        if h == "postgres":
            return {"172.18.0.2"}
        if h == "cloud.example.com":
            return {"8.8.8.8"}
        return set()

    with patch("app.api.db_state._resolve_host", _fake_resolve):
        out = db_state.start_migration(
            payload=db_state.MigrateRequest(
                target="cloud",
                cloud_url="postgresql+psycopg://u:p@cloud.example.com:5432/agnes",
            )
        )

    raw = json.loads((jobs_dir / f"{out['job_id']}.json").read_text())
    assert "172.18.0.2" in raw["source_url_pinned_ip"]
    assert "8.8.8.8" in raw["target_url_pinned_ip"]


def test_ip_literal_target_pins_same_ip(tmp_path: Path, monkeypatch) -> None:
    """When the user posts an IP literal as the cloud_url host, the
    pinned URL is the same literal (no resolution needed)."""
    from app.api import db_state

    jobs_dir = _setup_state(
        tmp_path,
        monkeypatch,
        "database:\n  backend: duckdb\n",
    )
    # AGNES_ALLOW_RESERVED_CLOUD_URL bypasses the reserved-range check
    # so the IP-literal path in _pin_resolved_ip can be exercised cheaply.
    monkeypatch.setenv("AGNES_ALLOW_RESERVED_CLOUD_URL", "1")

    out = db_state.start_migration(
        payload=db_state.MigrateRequest(
            target="cloud",
            cloud_url="postgresql+psycopg://u:p@8.8.8.8:5432/agnes",
        )
    )

    raw = json.loads((jobs_dir / f"{out['job_id']}.json").read_text())
    assert "8.8.8.8" in raw["target_url_pinned_ip"]
