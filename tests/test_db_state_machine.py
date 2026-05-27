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


def test_allowed_transitions_forward_only():
    """duckdb → side_car → cloud; no rollback."""
    assert allowed_transitions(BackendState.DUCKDB) == [BackendState.SIDE_CAR]
    assert allowed_transitions(BackendState.SIDE_CAR) == [BackendState.CLOUD]
    assert allowed_transitions(BackendState.CLOUD) == []


def test_allowed_transitions_from_transient():
    """In-progress states allow ONLY the next stable state (retry)."""
    assert allowed_transitions(BackendState.SIDE_CAR_IN_PROGRESS) == [BackendState.SIDE_CAR]
    assert allowed_transitions(BackendState.CLOUD_IN_PROGRESS) == [BackendState.CLOUD]


def test_validate_transition_ok():
    """Valid transition returns None; invalid raises."""
    validate_transition(BackendState.DUCKDB, BackendState.SIDE_CAR)  # no raise


def test_validate_transition_rejects_backward():
    """Backward transition (side_car → duckdb) raises InvalidTransitionError."""
    with pytest.raises(InvalidTransitionError) as exc:
        validate_transition(BackendState.SIDE_CAR, BackendState.DUCKDB)
    assert "not allowed" in str(exc.value).lower()


def test_validate_transition_rejects_skip():
    """No duckdb → cloud (skip side-car)."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.DUCKDB, BackendState.CLOUD)


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
