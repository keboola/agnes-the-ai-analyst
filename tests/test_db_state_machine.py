"""Unit tests for DB backend state machine."""
from __future__ import annotations
import pytest
from src.db_state_machine import (
    BackendState,
    InvalidTransitionError,
    MigrationInProgressError,
    MigrationLock,
    allowed_transitions,
    read_backend_state,
    validate_transition,
    write_backend_state,
)


def test_backend_state_values():
    """Five states defined; forward-only transitions enforced."""
    assert BackendState.DUCKDB.value == "duckdb"
    assert BackendState.SIDE_CAR.value == "side_car"
    assert BackendState.CLOUD.value == "cloud"
    assert BackendState.SIDE_CAR_IN_PROGRESS.value == "side_car_in_progress"
    assert BackendState.CLOUD_IN_PROGRESS.value == "cloud_in_progress"


def test_allowed_transitions_matrix():
    """DuckDB is start-only; PG↔PG is bidirectional.

      DUCKDB    → [SIDE_CAR, CLOUD]   (first cutover, either target)
      SIDE_CAR  → [CLOUD]              (graduate to managed)
      CLOUD     → [SIDE_CAR]           (DR / retire managed instance)

    No transition back to DuckDB once on PG.
    """
    assert allowed_transitions(BackendState.DUCKDB) == [
        BackendState.SIDE_CAR, BackendState.CLOUD,
    ]
    assert allowed_transitions(BackendState.SIDE_CAR) == [BackendState.CLOUD]
    assert allowed_transitions(BackendState.CLOUD) == [BackendState.SIDE_CAR]


def test_allowed_transitions_from_transient():
    """In-progress states allow ONLY the next stable state (retry)."""
    assert allowed_transitions(BackendState.SIDE_CAR_IN_PROGRESS) == [BackendState.SIDE_CAR]
    assert allowed_transitions(BackendState.CLOUD_IN_PROGRESS) == [BackendState.CLOUD]


def test_validate_transition_ok():
    """Valid transitions return None; invalid raises."""
    validate_transition(BackendState.DUCKDB, BackendState.SIDE_CAR)   # first cutover
    validate_transition(BackendState.DUCKDB, BackendState.CLOUD)      # skip side-car
    validate_transition(BackendState.SIDE_CAR, BackendState.CLOUD)    # graduate
    validate_transition(BackendState.CLOUD, BackendState.SIDE_CAR)    # DR back


def test_validate_transition_rejects_rollback_to_duckdb():
    """No transition lands on DuckDB once a PG cutover has happened.

    DuckDB is treated as immutable post-cutover (the backup file is
    the recovery artifact, not a writable target).
    """
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.SIDE_CAR, BackendState.DUCKDB)
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.CLOUD, BackendState.DUCKDB)


def test_validate_transition_rejects_self():
    """No-op transition (state → same state) raises."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.SIDE_CAR, BackendState.SIDE_CAR)


def test_write_then_read_backend_state(tmp_path, monkeypatch):
    """Round-trip: write side_car + URL, read same values."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(
        BackendState.SIDE_CAR,
        url="postgresql+psycopg://agnes:pw@postgres:5432/agnes",
    )
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == "postgresql+psycopg://agnes:pw@postgres:5432/agnes"


def test_read_returns_duckdb_when_overlay_absent(tmp_path, monkeypatch):
    """Fresh install defaults to duckdb."""
    overlay = tmp_path / "nonexistent.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    state, url = read_backend_state()
    assert state == BackendState.DUCKDB
    assert url is None


def test_write_is_atomic(tmp_path, monkeypatch):
    """Writes go through .tmp + os.replace; no .tmp left behind on success."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")
    assert overlay.exists()
    assert not (tmp_path / "instance.yaml.tmp").exists()


def test_lock_acquire_release(tmp_path, monkeypatch):
    """flock acquired and released cleanly."""
    lock_path = tmp_path / "db-migration.lock"
    monkeypatch.setattr("src.db_state_machine._LOCK_PATH", lock_path)

    with MigrationLock() as lock:
        assert lock.held
    assert lock_path.exists()  # file remains; lock released


def test_second_acquire_raises(tmp_path, monkeypatch):
    """Concurrent acquisition raises MigrationInProgressError."""
    lock_path = tmp_path / "db-migration.lock"
    monkeypatch.setattr("src.db_state_machine._LOCK_PATH", lock_path)

    with MigrationLock():
        with pytest.raises(MigrationInProgressError):
            with MigrationLock():
                pass


def test_get_database_config_reads_from_state_module(tmp_path, monkeypatch):
    """get_database_config delegates to state machine read."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import write_backend_state
    write_backend_state(BackendState.CLOUD, url="postgresql://cloud/agnes")

    from app.instance_config import get_database_config, reset_database_cache
    reset_database_cache()
    config = get_database_config()
    assert config["backend"] == "cloud"
    assert config["url"] == "postgresql://cloud/agnes"


def test_write_backend_state_preserves_url_when_url_kw_absent(tmp_path, monkeypatch):
    """When ``write_backend_state`` is called with no ``url`` argument
    it must KEEP the existing url key in instance.yaml. Previously it
    omitted url from the output → yaml.safe_dump erased the key →
    repository routing saw backend=*_in_progress (treated as PG) with
    no URL → 30s+ window where every authenticated request crashed
    with ``Postgres URL is unset``."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state

    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)   # no url= kwarg

    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR_IN_PROGRESS
    assert url == "postgresql+psycopg://x:y@h/d"


def test_write_backend_state_clears_url_when_url_none_explicit(tmp_path, monkeypatch):
    """Explicit url=None means CLEAR. Used for transitioning to a
    stateless backend like DuckDB."""
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    write_backend_state(BackendState.DUCKDB, url=None)
    state, url = read_backend_state()
    assert state == BackendState.DUCKDB
    assert url is None


def test_write_backend_state_preserves_other_top_level_keys(tmp_path, monkeypatch):
    """Non-database keys (logging, auth, feature flags) the operator
    may have set via /admin/server-config must NOT be lost when
    write_backend_state mutates the database subkey."""
    import yaml
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    # Pre-seed the overlay with operator-set keys.
    overlay.write_text(yaml.safe_dump({
        "logging": {"level": "debug"},
        "auth": {"providers": ["google", "magic_link"]},
        "database": {"backend": "duckdb"},
    }))
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://x:y@h/d")
    data = yaml.safe_load(overlay.read_text())
    assert data["logging"]["level"] == "debug"
    assert data["auth"]["providers"] == ["google", "magic_link"]
    assert data["database"]["backend"] == "side_car"
    assert data["database"]["url"] == "postgresql+psycopg://x:y@h/d"


def test_write_backend_state_sets_0600(tmp_path, monkeypatch):
    """H2 — instance.yaml carries plaintext PG credentials and must
    be owner-readable only. Catches the case where a non-app user on
    the same host could ``cat`` the overlay and exfiltrate the URL."""
    import os, stat
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql+psycopg://agnes:pw@host/agnes")
    mode = stat.S_IMODE(os.stat(overlay).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
