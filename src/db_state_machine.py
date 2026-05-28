"""State machine for app-state DB backend (DuckDB / side-car PG / cloud PG).

Forward-only transitions enforced; transient *_in_progress states track
in-flight migrations so the API can reject concurrent attempts and the
app can detect crashed migrations on startup.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import fcntl
import os
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml


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


# Allowed transitions.
#
#   DUCKDB → SIDE_CAR   (initial cutover to in-container Postgres)
#   DUCKDB → CLOUD      (cutover directly to managed Postgres,
#                        skipping the side-car container)
#   SIDE_CAR → CLOUD    (graduate from container PG to managed PG)
#   CLOUD → SIDE_CAR    (move back to container PG — DR, cost, or
#                        when the managed instance is being retired)
#
# DuckDB is **start-only**. No path back to DuckDB exists on purpose:
# once an instance is on Postgres, anything written there since the
# cutover has no DuckDB-readable form. Operators that genuinely need
# to re-test the cutover from scratch wipe the persistent volume
# and recreate the VM instead — re-running the state-machine on the
# same instance is not a supported workflow.
#
# In-progress states retain their old "revert to stable" reads so a
# crashed migration can be retried; the cancel API path uses those.
_ALLOWED_TRANSITIONS: dict[BackendState, list[BackendState]] = {
    BackendState.DUCKDB: [BackendState.SIDE_CAR, BackendState.CLOUD],
    BackendState.SIDE_CAR: [BackendState.CLOUD],
    BackendState.CLOUD: [BackendState.SIDE_CAR],
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


_OVERLAY_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "instance.yaml"
_LOCK_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-migration.lock"


def read_backend_state() -> tuple[BackendState, str | None]:
    """Read current backend + url from instance.yaml overlay.

    Returns (BackendState.DUCKDB, None) when overlay missing or
    ``database`` key absent — fresh-install default.
    """
    if not _OVERLAY_PATH.exists():
        return BackendState.DUCKDB, None
    try:
        data = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    except yaml.YAMLError:
        # Corrupt overlay; treat as duckdb to fail safe.
        return BackendState.DUCKDB, None
    db = data.get("database") or {}
    backend_str = db.get("backend", "duckdb")
    try:
        state = BackendState(backend_str)
    except ValueError:
        state = BackendState.DUCKDB
    return state, db.get("url")


def write_backend_state(target: BackendState, *, url: str | None = None) -> None:
    """Atomically update instance.yaml::database = {backend, url}.

    Uses tmp + os.replace for atomicity (same pattern as
    app/api/admin.py overlay writer). Caller is responsible for
    transition validation; this function performs no policy check.
    """
    _OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _OVERLAY_PATH.exists():
        data = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    else:
        data = {}

    data.setdefault("database", {})["backend"] = target.value
    if url is not None:
        data["database"]["url"] = url
    elif "url" in data["database"]:
        del data["database"]["url"]

    tmp = _OVERLAY_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=True))
    os.replace(tmp, _OVERLAY_PATH)


class MigrationInProgressError(RuntimeError):
    """A migration is already running; second concurrent attempt rejected."""


class MigrationLock:
    """Non-blocking flock at _LOCK_PATH.

    Usage:
        with MigrationLock():
            # exclusive section
            ...
    """

    def __init__(self) -> None:
        self.held = False
        self._fd: int | None = None

    def __enter__(self) -> Self:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(self._fd)
            self._fd = None
            raise MigrationInProgressError(
                f"Migration already in progress (lock held at {_LOCK_PATH})"
            ) from e
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self.held = False
