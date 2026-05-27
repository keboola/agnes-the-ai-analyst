"""Unit tests for DB backend state machine."""
from __future__ import annotations
import pytest
from src.db_state_machine import (
    BackendState,
    InvalidTransitionError,
    allowed_transitions,
    validate_transition,
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
