"""State machine for app-state DB backend (DuckDB / side-car PG / cloud PG).

Forward-only transitions enforced; transient *_in_progress states track
in-flight migrations so the API can reject concurrent attempts and the
app can detect crashed migrations on startup.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
from enum import StrEnum


class BackendState(StrEnum):
    """Backend states for the app-state DB layer.

    Values are persisted verbatim in ``instance.yaml::database.backend`` and
    in audit-log rows — do not rename without a migration that rewrites
    persisted state.
    """
    DUCKDB = "duckdb"
    SIDE_CAR = "side_car"
    CLOUD = "cloud"
    SIDE_CAR_IN_PROGRESS = "side_car_in_progress"
    CLOUD_IN_PROGRESS = "cloud_in_progress"


class InvalidTransitionError(ValueError):
    """Requested transition is not allowed from the current state."""


# Allowed forward transitions. In-progress states allow only the
# corresponding stable target (so a crashed migration can be retried).
_ALLOWED_TRANSITIONS: dict[BackendState, list[BackendState]] = {
    BackendState.DUCKDB: [BackendState.SIDE_CAR],
    BackendState.SIDE_CAR: [BackendState.CLOUD],
    BackendState.CLOUD: [],
    BackendState.SIDE_CAR_IN_PROGRESS: [BackendState.SIDE_CAR],
    BackendState.CLOUD_IN_PROGRESS: [BackendState.CLOUD],
}


def allowed_transitions(current: BackendState) -> list[BackendState]:
    """List of allowed target states from ``current``."""
    return _ALLOWED_TRANSITIONS[current]


def validate_transition(current: BackendState, target: BackendState) -> None:
    """Raise InvalidTransitionError if ``target`` is not reachable from ``current``."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"Transition {current.value} → {target.value} not allowed. "
            f"From {current.value}, allowed targets: "
            f"{[t.value for t in _ALLOWED_TRANSITIONS[current]] or 'none (terminal state)'}"
        )
