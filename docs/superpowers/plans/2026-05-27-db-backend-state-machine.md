# Database Backend State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc `.env` editing with an admin-controlled state machine that migrates Agnes app-state from DuckDB → side-car Postgres → managed cloud Postgres, surfaced in `/admin/server-config` UI and `agnes admin db` CLI.

**Architecture:** Three backend states (`duckdb`, `side_car`, `cloud`) in `instance.yaml::database.backend`; forward-only transitions; app handles Python migration logic in a subprocess; host-side `agnes-state-applier.timer` reads `/data/state/db-state-target.flag` and applies docker compose lifecycle changes. App needs no docker socket access.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 + psycopg 3, Alembic 1.18, Click 8.x (CLI), Jinja2 + vanilla JS (UI), systemd timers (host), pgserver (tests).

**Precondition:** PR #454 merged to `main` (postgres app-state foundation + compose validation root-cause fix). Worktree created from fresh `origin/main`. The 28 existing PG repos, alembic 0001–0011, factory pattern, and docker-compose.postgres.yml + docker-compose.postgres-host-mount.yml are all in place.

**Spec:** `docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md` — read before starting.

---

## Scope Check

Single subsystem (DB backend admin management). Spec is focused enough for one plan. No decomposition needed.

---

## File Structure

| Path | Purpose | Phase |
|---|---|---|
| `src/db_state_machine.py` | NEW. State validation (allowed transitions), atomic state writes via instance.yaml, audit log emission, file lock | 1A |
| `app/instance_config.py` | EXTEND. `get_database_config()` helper; `reset_database_cache()` | 1A |
| `src/db_pg.py` | EXTEND. `_resolve_url()` reads instance.yaml::database.url first; `dispose_engine()` | 1B |
| `src/repositories/__init__.py` | EXTEND. `use_pg()` checks `instance.yaml::database.backend != "duckdb"` first | 1B |
| `scripts/db_state_migrator.py` | NEW. Subprocess orchestrator: alembic + data copy + verify + backup. Reuses `migrate_duckdb_to_pg/` | 2 |
| `app/api/db_state.py` | NEW. FastAPI router. 4 endpoints under `/api/admin/db/*` | 3 |
| `app/main.py` | EXTEND. Register `db_state.router` | 3 |
| `scripts/ops/agnes-state-applier.sh` | NEW. Host-side daemon; systemd timer-driven (every 30s) | 4 |
| `infra/modules/customer-instance/startup-script.sh.tpl` | EXTEND. Initial `instance.yaml::database = {backend: "duckdb"}` write; install applier.timer | 4 |
| `cli/commands/db.py` | NEW. Click subcommand group: state, migrate, job, cancel | 5 |
| `cli/main.py` | EXTEND. Register `db` group under `admin` | 5 |
| `app/web/static/js/admin/db_state.js` | NEW. Polling + progress bar + modal | 6 |
| `app/web/templates/admin_server_config.html` | EXTEND. "Database backend" section | 6 |
| `tests/test_db_state_machine.py` | NEW. Unit: state transitions, lock, audit | 1A |
| `tests/test_db_state_migrator.py` | NEW. Unit: each migrator step with mocked target | 2 |
| `tests/test_api_db_state.py` | NEW. Integration: HTTP cycle via pgserver | 3 |
| `tests/test_cli_db.py` | NEW. CLI smoke + JSON output | 5 |
| `tests/db_pg/test_db_state_e2e.py` | NEW. Full migration cycle: DuckDB → pgserver target | 7 |
| `docs/postgres-cutover-runbook.md` | EXTEND. Add UI/CLI section, manual smoke checklist | 8 |
| `CHANGELOG.md` | EXTEND. `[Unreleased]` bullet | 8 |

---

## Validation primitives

### Setup once per shell

```bash
cd <repo-root>
unset UV_PYTHON
source .venv/bin/activate
.venv/bin/python --version       # Expect: Python 3.12.x
```

### Run targeted tests

```bash
# Single unit file
.venv/bin/pytest tests/test_db_state_machine.py -v --tb=short --timeout=60

# PG integration suite
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=180

# CLI tests
.venv/bin/pytest tests/test_cli_db.py -v --tb=short --timeout=60
```

### Full PG suite (regression check)

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=180
# Expect: 322+ passed (existing PR #454 baseline) + new tests from this plan
```

---

## Phase 1A — State machine core + config wiring

### Task 1A.1: State machine module skeleton + transitions

**Files:**
- Create: `src/db_state_machine.py`
- Create: `tests/test_db_state_machine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_state_machine.py
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
    with pytest.raises(InvalidTransitionError) as exc:
        validate_transition(BackendState.SIDE_CAR, BackendState.DUCKDB)
    assert "not allowed" in str(exc.value).lower()


def test_validate_transition_rejects_skip():
    """No duckdb → cloud (skip side-car)."""
    with pytest.raises(InvalidTransitionError):
        validate_transition(BackendState.DUCKDB, BackendState.CLOUD)
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
unset UV_PYTHON
.venv/bin/pytest tests/test_db_state_machine.py -v --tb=short --timeout=60 2>&1 | tail -5
```

Expect: `ModuleNotFoundError: src.db_state_machine`.

- [ ] **Step 3: Write minimal module**

```python
# src/db_state_machine.py
"""State machine for app-state DB backend (DuckDB / side-car PG / cloud PG).

Forward-only transitions enforced; transient *_in_progress states track
in-flight migrations so the API can reject concurrent attempts and the
app can detect crashed migrations on startup.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
from enum import Enum


class BackendState(str, Enum):
    DUCKDB = "duckdb"
    SIDE_CAR = "side_car"
    CLOUD = "cloud"
    SIDE_CAR_IN_PROGRESS = "side_car_in_progress"
    CLOUD_IN_PROGRESS = "cloud_in_progress"


class InvalidTransitionError(ValueError):
    """Requested transition is not allowed from the current state."""


# Allowed forward transitions. In-progress states allow only the
# corresponding stable target (so a crashed migration can be retried).
_ALLOWED: dict[BackendState, list[BackendState]] = {
    BackendState.DUCKDB: [BackendState.SIDE_CAR],
    BackendState.SIDE_CAR: [BackendState.CLOUD],
    BackendState.CLOUD: [],
    BackendState.SIDE_CAR_IN_PROGRESS: [BackendState.SIDE_CAR],
    BackendState.CLOUD_IN_PROGRESS: [BackendState.CLOUD],
}


def allowed_transitions(current: BackendState) -> list[BackendState]:
    """List of allowed target states from ``current``."""
    return _ALLOWED[current]


def validate_transition(current: BackendState, target: BackendState) -> None:
    """Raise InvalidTransitionError if ``target`` is not reachable from ``current``."""
    if target not in _ALLOWED[current]:
        raise InvalidTransitionError(
            f"Transition {current.value} → {target.value} not allowed. "
            f"From {current.value}, allowed targets: "
            f"{[t.value for t in _ALLOWED[current]] or 'none (terminal state)'}"
        )
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
.venv/bin/pytest tests/test_db_state_machine.py -v --tb=short --timeout=60 2>&1 | tail -10
```

Expect: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/db_state_machine.py tests/test_db_state_machine.py
git commit -m "feat(db): state machine core — 5 states + forward-only transitions"
```

### Task 1A.2: Atomic state write to instance.yaml

**Files:**
- Modify: `src/db_state_machine.py`
- Modify: `tests/test_db_state_machine.py`

- [ ] **Step 1: Read existing instance.yaml overlay writer**

```bash
grep -n "instance.yaml\|os.replace\|atomic" app/api/admin.py | head -10
```

Note: existing code uses a temp file + `os.replace()` for atomic write. Match that pattern.

- [ ] **Step 2: Add test for atomic write + read-back**

Append to `tests/test_db_state_machine.py`:

```python
import json
from pathlib import Path
import yaml

from src.db_state_machine import (
    BackendState,
    read_backend_state,
    write_backend_state,
)


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
```

- [ ] **Step 3: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/test_db_state_machine.py::test_write_then_read_backend_state -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: `ImportError: cannot import name 'read_backend_state'`.

- [ ] **Step 4: Implement read/write**

Append to `src/db_state_machine.py`:

```python
import os
from pathlib import Path
import yaml

_OVERLAY_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "instance.yaml"


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
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
.venv/bin/pytest tests/test_db_state_machine.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: `9 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/db_state_machine.py tests/test_db_state_machine.py
git commit -m "feat(db): atomic read/write backend state in instance.yaml"
```

### Task 1A.3: File lock for migration in-progress

**Files:**
- Modify: `src/db_state_machine.py`
- Modify: `tests/test_db_state_machine.py`

- [ ] **Step 1: Write test for lock acquisition**

Append:

```python
from src.db_state_machine import MigrationLock, MigrationInProgressError


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
```

- [ ] **Step 2: Implement MigrationLock**

Append to `src/db_state_machine.py`:

```python
import fcntl
from typing import Self

_LOCK_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-migration.lock"


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
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_db_state_machine.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: `11 passed`.

- [ ] **Step 4: Commit**

```bash
git add src/db_state_machine.py tests/test_db_state_machine.py
git commit -m "feat(db): MigrationLock — flock-based concurrent-migration guard"
```

### Task 1A.4: instance_config helper + cache invalidation

**Files:**
- Modify: `app/instance_config.py`
- Modify: `tests/test_db_state_machine.py`

- [ ] **Step 1: Read existing reset_cache + get_value**

```bash
grep -n "def reset_cache\|def get_value\|_CACHE" app/instance_config.py | head -10
```

Note the cache key pattern.

- [ ] **Step 2: Add test for `get_database_config`**

Append:

```python
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
```

- [ ] **Step 3: Implement helper**

Append to `app/instance_config.py`:

```python
def get_database_config() -> dict:
    """Return ``{backend: "...", url: "..."}`` from the state machine.

    Centralised so future callers don't reach into src.db_state_machine
    directly. Cache invalidation via reset_database_cache() after
    /api/admin/db/migrate success.
    """
    from src.db_state_machine import read_backend_state
    state, url = read_backend_state()
    return {"backend": state.value, "url": url}


def reset_database_cache() -> None:
    """No-op for now — get_database_config reads fresh each call.

    Exposed as a public API so future caching (if added) has a single
    invalidation point. Called by app/api/db_state.py after a successful
    backend flip.
    """
    pass
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
.venv/bin/pytest tests/test_db_state_machine.py::test_get_database_config_reads_from_state_module -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/instance_config.py tests/test_db_state_machine.py
git commit -m "feat(config): get_database_config + reset_database_cache helpers"
```

---

## Phase 1B — Wire engine + factory to instance.yaml

### Task 1B.1: `src/db_pg.py::_resolve_url` reads instance.yaml first

**Files:**
- Modify: `src/db_pg.py`
- Modify: `tests/db_pg/test_db_pg_resolve.py` (new file)

- [ ] **Step 1: Read current `_resolve_url`**

```bash
sed -n '40,75p' src/db_pg.py
```

- [ ] **Step 2: Write test**

```python
# tests/db_pg/test_db_pg_resolve.py
"""URL resolution precedence: instance.yaml → DATABASE_URL → AGNES_DB_URL."""
from __future__ import annotations
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_resolve_prefers_instance_yaml(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://from-yaml/agnes")

    # Also set env to a different value — yaml must win
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://from-yaml/agnes"


def test_resolve_falls_back_to_database_url_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://from-env/agnes"


def test_resolve_falls_back_to_agnes_db_url_env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("AGNES_DB_URL", "postgresql://legacy/agnes")

    from src.db_pg import _resolve_url
    assert _resolve_url() == "postgresql://legacy/agnes"


def test_resolve_raises_when_all_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    from src.db_pg import _resolve_url
    with pytest.raises(RuntimeError, match="Postgres URL is unset"):
        _resolve_url()
```

- [ ] **Step 3: Run test, expect FAIL (yaml-prefer test fails — env still wins)**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_db_pg_resolve.py -v --tb=short --timeout=30 2>&1 | tail -10
```

- [ ] **Step 4: Modify `_resolve_url`**

In `src/db_pg.py`, replace `_resolve_url` body:

```python
def _resolve_url() -> str:
    """Return the Postgres URL using fallback chain:

      1. ``instance.yaml::database.url`` (admin-controlled, runtime-mutable).
      2. ``DATABASE_URL`` env var (12-factor convention).
      3. ``AGNES_DB_URL`` env var (deprecated alias — warning logged).

    Raises RuntimeError when no URL is configured.
    """
    # 1. instance.yaml
    try:
        from src.db_state_machine import read_backend_state
        _state, yaml_url = read_backend_state()
        if yaml_url:
            return yaml_url
    except Exception:
        # State module may be unavailable during early startup; fall
        # through to env vars.
        pass

    # 2. DATABASE_URL
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return db_url

    # 3. AGNES_DB_URL (legacy)
    legacy = os.environ.get("AGNES_DB_URL")
    if legacy:
        logger.warning(
            "AGNES_DB_URL is deprecated — rename to DATABASE_URL (12-factor convention)"
        )
        return legacy

    raise RuntimeError(
        "Postgres URL is unset: set instance.yaml::database.url via "
        "/api/admin/db/migrate, or set DATABASE_URL env var"
    )
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_db_pg_resolve.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: `4 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/db_pg.py tests/db_pg/test_db_pg_resolve.py
git commit -m "feat(db_pg): _resolve_url prefers instance.yaml::database.url"
```

### Task 1B.2: `dispose_engine()` for runtime URL switch

**Files:**
- Modify: `src/db_pg.py`
- Modify: `tests/db_pg/test_db_pg_resolve.py`

- [ ] **Step 1: Find current engine singleton + create dispose function**

```bash
grep -n "def get_engine\|_engine" src/db_pg.py | head -10
```

- [ ] **Step 2: Add test**

Append to `tests/db_pg/test_db_pg_resolve.py`:

```python
def test_dispose_engine_clears_singleton(pg_engine):
    """After dispose_engine(), next get_engine() re-resolves URL."""
    from src.db_pg import dispose_engine, get_engine
    # Pre-condition: engine already created by pg_engine fixture
    first = get_engine()
    dispose_engine()
    # Internal singleton should be None now; get_engine recreates
    second = get_engine()
    # Different engine instances (re-resolution happened)
    assert first is not second
```

- [ ] **Step 3: Implement dispose_engine**

In `src/db_pg.py`, after `get_engine()`:

```python
def dispose_engine() -> None:
    """Dispose the singleton engine + clear the cache.

    Next ``get_engine()`` call will re-resolve the URL and rebuild the
    engine. Called by ``POST /api/admin/db/migrate`` after a successful
    backend flip to make new repository operations land on the new
    backend without an app restart (though the app DOES restart on
    most migrations — this is a defence-in-depth runtime path).
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
```

- [ ] **Step 4: Run test**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_db_pg_resolve.py::test_dispose_engine_clears_singleton -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/db_pg.py tests/db_pg/test_db_pg_resolve.py
git commit -m "feat(db_pg): dispose_engine() — clears singleton for runtime URL swap"
```

### Task 1B.3: `use_pg()` checks instance.yaml first

**Files:**
- Modify: `src/repositories/__init__.py`
- Create: `tests/test_repositories_use_pg.py`

- [ ] **Step 1: Read current `use_pg`**

```bash
grep -n "def use_pg" src/repositories/__init__.py
```

- [ ] **Step 2: Add test**

```python
# tests/test_repositories_use_pg.py
"""use_pg() precedence: instance.yaml::database.backend → env var."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_use_pg_true_when_yaml_says_side_car(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR, url="postgresql://x")

    from src.repositories import use_pg
    assert use_pg() is True


def test_use_pg_false_when_yaml_says_duckdb(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.DUCKDB)

    from src.repositories import use_pg
    assert use_pg() is False


def test_use_pg_falls_back_to_env_when_yaml_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")

    from src.repositories import use_pg
    assert use_pg() is True


def test_use_pg_false_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "missing.yaml")

    from src.repositories import use_pg
    assert use_pg() is False
```

- [ ] **Step 3: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/test_repositories_use_pg.py -v --tb=short --timeout=30 2>&1 | tail -10
```

- [ ] **Step 4: Modify `use_pg()` in `src/repositories/__init__.py`**

Replace existing `use_pg` body:

```python
def use_pg() -> bool:
    """Return True when the active backend is Postgres (side-car or cloud).

    Precedence:
      1. ``instance.yaml::database.backend`` (admin-controlled).
      2. ``DATABASE_URL`` env var presence (12-factor convention).
      3. ``AGNES_DB_URL`` env var presence (legacy alias).
    """
    try:
        from src.db_state_machine import BackendState, read_backend_state
        state, _ = read_backend_state()
        if state in (
            BackendState.SIDE_CAR,
            BackendState.CLOUD,
            BackendState.SIDE_CAR_IN_PROGRESS,
            BackendState.CLOUD_IN_PROGRESS,
        ):
            return True
        if state == BackendState.DUCKDB:
            # Explicit duckdb wins over env vars (admin chose duckdb deliberately)
            return False
    except Exception:
        pass

    # Env-var fallback
    import os
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("AGNES_DB_URL"))
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
.venv/bin/pytest tests/test_repositories_use_pg.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: `4 passed`.

- [ ] **Step 6: Regression check — existing PG suite**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=180 2>&1 | tail -3
```

Expect: same pass count as before this plan started (322+ passed). Investigate any regression.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/__init__.py tests/test_repositories_use_pg.py
git commit -m "feat(repos): use_pg() prefers instance.yaml::database.backend"
```

---

## Phase 2 — Migration script

### Task 2.1: Job status writer + skeleton

**Files:**
- Create: `scripts/db_state_migrator.py`
- Create: `scripts/__init__.py` (if not present — namespace package marker)
- Create: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Write test for job status JSON write**

```python
# tests/test_db_state_migrator.py
"""Unit tests for db_state_migrator subprocess orchestrator."""
from __future__ import annotations
import json
from pathlib import Path
import pytest

from scripts.db_state_migrator import JobStatus, JobWriter


def test_job_writer_writes_initial_status(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()

    path = jobs_dir / "abc123.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["job_id"] == "abc123"
    assert data["status"] == "running"
    assert data["source_backend"] == "duckdb"
    assert data["target_backend"] == "side_car"
    assert data["progress_pct"] == 0
    assert data["started_at"] is not None
    assert data["completed_at"] is None


def test_job_writer_update_step(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.update_step("alembic", progress_pct=25)

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["current_step"] == "alembic"
    assert data["progress_pct"] == 25


def test_job_writer_mark_success(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.mark_success(summary={"tables_migrated": 28, "rows_total": 12345})

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["status"] == "success"
    assert data["completed_at"] is not None
    assert data["summary"]["tables_migrated"] == 28


def test_job_writer_mark_failed(tmp_path):
    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir()
    writer = JobWriter(job_id="abc123", jobs_dir=jobs_dir, source="duckdb", target="side_car")
    writer.write_initial()
    writer.mark_failed(step="data_copy", error_class="OperationalError", error_message="connection terminated")

    data = json.loads((jobs_dir / "abc123.json").read_text())
    assert data["status"] == "failed"
    assert data["error"]["step"] == "data_copy"
    assert data["error"]["class"] == "OperationalError"
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/test_db_state_migrator.py -v --tb=short --timeout=30 2>&1 | tail -5
```

- [ ] **Step 3: Implement JobWriter**

```python
# scripts/db_state_migrator.py
"""Migration subprocess orchestrator for DB backend state machine.

Invoked by app/api/db_state.py as a child subprocess; writes job
status to /data/state/db-jobs/<job_id>.json so the API endpoint can
poll. Steps for duckdb → side_car:

  1. validate connectivity
  2. alembic upgrade head on target
  3. data copy DuckDB → target (reuses scripts/migrate_duckdb_to_pg)
  4. verify row counts
  5. backup DuckDB snapshot
  6. flip instance.yaml::database

For side_car → cloud, source is the side-car PG; data step uses the
same migrator with a different source connection.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobWriter:
    """Writes /data/state/db-jobs/<job_id>.json on each step transition.

    Atomic write via tmp + os.replace. Schema versioned at 1.
    """

    job_id: str
    jobs_dir: Path
    source: str
    target: str
    _started_at: str = field(default_factory=lambda: _utcnow_iso())

    @property
    def _path(self) -> Path:
        return self.jobs_dir / f"{self.job_id}.json"

    def _write(self, data: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def _read(self) -> dict[str, Any]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def write_initial(self) -> None:
        data = {
            "schema_version": 1,
            "job_id": self.job_id,
            "status": JobStatus.RUNNING.value,
            "source_backend": self.source,
            "target_backend": self.target,
            "started_at": self._started_at,
            "completed_at": None,
            "current_step": "validate",
            "progress_pct": 0,
            "summary": None,
            "error": None,
        }
        self._write(data)

    def update_step(self, step: str, *, progress_pct: int) -> None:
        data = self._read()
        data["current_step"] = step
        data["progress_pct"] = progress_pct
        self._write(data)

    def update_table_progress(self, current_table: str, tables_done: int, tables_total: int) -> None:
        data = self._read()
        data["current_step"] = "data_copy"
        data["table_progress"] = {
            "current_table": current_table,
            "tables_done": tables_done,
            "tables_total": tables_total,
        }
        data["progress_pct"] = int(40 + (tables_done / tables_total) * 40)  # 40-80% range
        self._write(data)

    def mark_success(self, summary: dict[str, Any]) -> None:
        data = self._read()
        data["status"] = JobStatus.SUCCESS.value
        data["completed_at"] = _utcnow_iso()
        data["progress_pct"] = 100
        data["summary"] = summary
        self._write(data)

    def mark_failed(self, *, step: str, error_class: str, error_message: str) -> None:
        data = self._read()
        data["status"] = JobStatus.FAILED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {
            "step": step,
            "class": error_class,
            "message": error_message,
        }
        self._write(data)

    def mark_cancelled(self, *, step: str) -> None:
        data = self._read()
        data["status"] = JobStatus.CANCELLED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {"step": step, "class": "Cancelled", "message": "Admin cancelled migration"}
        self._write(data)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Ensure scripts/__init__.py exists**

```bash
test -f scripts/__init__.py || echo '"""Scripts package."""' > scripts/__init__.py
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
.venv/bin/pytest tests/test_db_state_migrator.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: `4 passed`.

- [ ] **Step 6: Commit**

```bash
git add scripts/db_state_migrator.py scripts/__init__.py tests/test_db_state_migrator.py
git commit -m "feat(db): JobWriter — atomic status writer for migration subprocess"
```

### Task 2.2: Alembic step

**Files:**
- Modify: `scripts/db_state_migrator.py`
- Modify: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Test alembic_upgrade_head against pgserver**

Append:

```python
def test_alembic_upgrade_head_runs(tmp_path, pg_engine):
    """alembic_upgrade_head brings target to current head revision."""
    from scripts.db_state_migrator import alembic_upgrade_head

    alembic_upgrade_head(str(pg_engine.url))

    # Verify alembic_version row exists with head revision
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    assert row is not None
    # Head should be the latest revision (validate non-empty rather than
    # hardcoding which can drift)
    assert len(row[0]) > 0
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py::test_alembic_upgrade_head_runs -v --tb=short --timeout=60 2>&1 | tail -10
```

- [ ] **Step 3: Implement alembic_upgrade_head**

Append to `scripts/db_state_migrator.py`:

```python
def alembic_upgrade_head(target_url: str) -> None:
    """Run ``alembic upgrade head`` against ``target_url``.

    Idempotent — alembic itself is a no-op when already at head.
    Raises subprocess.CalledProcessError on failure.
    """
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": target_url}
    result = subprocess.run(
        ["alembic", "-c", str(repo_root / "alembic.ini"), "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
```

- [ ] **Step 4: Run test, expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py::test_alembic_upgrade_head_runs -v --tb=short --timeout=120 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_db_state_migrator.py
git commit -m "feat(db): db_state_migrator alembic_upgrade_head step"
```

### Task 2.3: Data copy step (reuse migrate_duckdb_to_pg)

**Files:**
- Modify: `scripts/db_state_migrator.py`
- Modify: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Read existing migrate_duckdb_to_pg public API**

```bash
grep -n "^def \|^class " scripts/migrate_duckdb_to_pg/__init__.py | head -20
```

- [ ] **Step 2: Add test**

Append:

```python
def test_copy_duckdb_to_pg_full_cycle(tmp_path, pg_engine):
    """Seed DuckDB → copy to PG → verify rows present."""
    import duckdb
    from src.db import _ensure_schema

    # Seed source DuckDB
    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES ('u1', 'alice@example.com', 'Alice')"
    )
    conn.close()

    # Run target alembic first (data_copy assumes schema present)
    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg
    alembic_upgrade_head(str(pg_engine.url))

    # Copy
    summary = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert summary["rows_total"] >= 1

    # Verify
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row[0] == "alice@example.com"
```

- [ ] **Step 3: Run, expect FAIL**

- [ ] **Step 4: Implement copy_duckdb_to_pg**

Append:

```python
def copy_duckdb_to_pg(duckdb_path: Path, target_url: str) -> dict[str, int]:
    """Copy all PG-mapped tables from DuckDB to target PG.

    Wraps scripts/migrate_duckdb_to_pg/ — the same idempotent copy loop
    that the docker-compose data-migrate one-shot uses. Returns
    ``{rows_total, tables_migrated}``.
    """
    from scripts.migrate_duckdb_to_pg import run_migration

    result = run_migration(
        duckdb_path=str(duckdb_path),
        target_url=target_url,
    )
    return {
        "rows_total": sum(t["rows_inserted"] for t in result["tables"]),
        "tables_migrated": len(result["tables"]),
    }
```

NOTE: the existing `scripts/migrate_duckdb_to_pg/__init__.py` may expose a different function name. If `run_migration` doesn't exist, look for the equivalent entry point (e.g., `main()` or a CLI function); adapt the call. The contract: pass DuckDB path + target URL, get back a summary dict.

- [ ] **Step 5: Run test**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py::test_copy_duckdb_to_pg_full_cycle -v --tb=short --timeout=120 2>&1 | tail -10
```

Expect: PASS. If fails on `run_migration` import, adjust to existing API.

- [ ] **Step 6: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_db_state_migrator.py
git commit -m "feat(db): db_state_migrator copy_duckdb_to_pg step"
```

### Task 2.4: Row-count verify step

**Files:**
- Modify: `scripts/db_state_migrator.py`
- Modify: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Test row-count comparison**

Append:

```python
def test_verify_row_counts_match(tmp_path, pg_engine):
    """After copy, source and target row counts match."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import (
        alembic_upgrade_head,
        copy_duckdb_to_pg,
        verify_row_counts,
    )

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A'), ('u2', 'b@x', 'B')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    copy_duckdb_to_pg(duck_path, str(pg_engine.url))

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    # Empty diffs = all tables match
    assert diffs == [], f"Row count diffs: {diffs}"


def test_verify_row_counts_detects_mismatch(tmp_path, pg_engine):
    """When PG missing rows, verify returns table-level diff."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import alembic_upgrade_head, verify_row_counts

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    # Skip copy — leave PG empty

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    user_diff = next(d for d in diffs if d["table"] == "users")
    assert user_diff["source_rows"] == 1
    assert user_diff["target_rows"] == 0
```

- [ ] **Step 2: Implement verify_row_counts**

Append:

```python
def verify_row_counts(duckdb_path: Path, target_url: str) -> list[dict]:
    """Compare row counts per Base.metadata table between DuckDB and PG.

    Returns list of diffs ``[{table, source_rows, target_rows}]``.
    Empty list = all tables match. Tables present in only one side
    are also reported (the other side's count = 0).
    """
    import duckdb as _duckdb
    import sqlalchemy as sa
    from src.db_pg import Base

    diffs: list[dict] = []
    tables = [t.name for t in Base.metadata.sorted_tables]

    duck_conn = _duckdb.connect(str(duckdb_path))
    pg_engine = sa.create_engine(target_url)
    try:
        for table in tables:
            try:
                src_count = duck_conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            except _duckdb.CatalogException:
                src_count = 0
            try:
                with pg_engine.connect() as pg_conn:
                    tgt_count = pg_conn.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table}"')
                    ).fetchone()[0]
            except sa.exc.ProgrammingError:
                tgt_count = 0
            if src_count != tgt_count:
                diffs.append({
                    "table": table,
                    "source_rows": src_count,
                    "target_rows": tgt_count,
                })
    finally:
        duck_conn.close()
        pg_engine.dispose()
    return diffs
```

- [ ] **Step 3: Run, expect PASS**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py -v --tb=short --timeout=120 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_db_state_migrator.py
git commit -m "feat(db): db_state_migrator verify_row_counts step"
```

### Task 2.5: Backup step

**Files:**
- Modify: `scripts/db_state_migrator.py`
- Modify: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Test gzip backup of DuckDB**

Append:

```python
def test_backup_duckdb_creates_gzipped_copy(tmp_path):
    """Backup writes gzip'd DuckDB to backups dir; original untouched."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import backup_duckdb

    src = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(src))
    _ensure_schema(conn)
    conn.close()

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    out = backup_duckdb(src, backups_dir)
    assert out.exists()
    assert out.name.startswith("duckdb-pre-sidecar-")
    assert out.suffix == ".gz"
    assert src.exists()  # original preserved
```

- [ ] **Step 2: Implement backup_duckdb + backup_sidecar_pg**

Append:

```python
def backup_duckdb(duckdb_path: Path, backups_dir: Path) -> Path:
    """gzip the DuckDB file to backups dir with timestamp.

    Returns path to backup file. Used before duckdb → side_car cutover
    so the operator has a recovery point if the side-car PG ever
    diverges and needs to be re-built from the frozen DuckDB.
    """
    import gzip
    import shutil

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"duckdb-pre-sidecar-{ts}.duckdb.gz"
    with open(duckdb_path, "rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    return out


def backup_sidecar_pg(container_name: str, backups_dir: Path) -> Path:
    """pg_dump custom format of side-car PG, via docker exec.

    Returns path to .dump file. Used before side_car → cloud cutover.
    """
    import subprocess

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"sidecar-pre-cloud-{ts}.dump"
    with open(out, "wb") as fp:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_dump", "-U", "agnes", "-F", "c", "agnes"],
            stdout=fp,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
    return out
```

- [ ] **Step 3: Run test**

```bash
.venv/bin/pytest tests/test_db_state_migrator.py::test_backup_duckdb_creates_gzipped_copy -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_db_state_migrator.py
git commit -m "feat(db): db_state_migrator backup_duckdb + backup_sidecar_pg"
```

### Task 2.6: Migrator main() — orchestrate steps end-to-end

**Files:**
- Modify: `scripts/db_state_migrator.py`
- Modify: `tests/test_db_state_migrator.py`

- [ ] **Step 1: Test main() orchestration via CLI invocation**

Append:

```python
def test_main_duckdb_to_side_car_end_to_end(tmp_path, pg_engine, monkeypatch):
    """End-to-end: main(--to=side_car) drives all steps + writes success."""
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    from scripts.db_state_migrator import main
    rc = main(
        job_id="job-test-1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0

    job = json.loads((jobs_dir / "job-test-1.json").read_text())
    assert job["status"] == "success"
    assert job["summary"]["tables_migrated"] > 0

    # State machine flipped to stable side_car
    state, url = __import__("src.db_state_machine", fromlist=["read_backend_state"]).read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)
```

- [ ] **Step 2: Implement main()**

Append to `scripts/db_state_migrator.py`:

```python
def main(
    *,
    job_id: str,
    to: str,
    target_url: str,
    duckdb_path: Path,
    jobs_dir: Path,
    backups_dir: Path,
) -> int:
    """Run the migration job. Returns process exit code.

    Steps for ``to="side_car"``:
      1. write initial job status
      2. alembic upgrade head
      3. copy DuckDB → PG
      4. verify row counts
      5. backup DuckDB
      6. flip instance.yaml::database
      7. mark success
    """
    from src.db_state_machine import BackendState, write_backend_state

    writer = JobWriter(
        job_id=job_id,
        jobs_dir=jobs_dir,
        source="duckdb" if to == "side_car" else "side_car",
        target=to,
    )
    writer.write_initial()

    try:
        writer.update_step("alembic", progress_pct=20)
        alembic_upgrade_head(target_url)

        writer.update_step("data_copy", progress_pct=40)
        copy_summary = copy_duckdb_to_pg(duckdb_path, target_url)

        writer.update_step("verify", progress_pct=80)
        diffs = verify_row_counts(duckdb_path, target_url)
        if diffs:
            writer.mark_failed(
                step="verify",
                error_class="VerifyMismatchError",
                error_message=f"Row count mismatch: {diffs[:5]}",
            )
            return 1

        writer.update_step("backup", progress_pct=90)
        backup_duckdb(duckdb_path, backups_dir)

        writer.update_step("flip_backend", progress_pct=95)
        target_state = BackendState(to)
        write_backend_state(target_state, url=target_url)

        writer.mark_success(summary=copy_summary)
        return 0

    except Exception as e:
        # Revert state to previous stable (best-effort).
        try:
            write_backend_state(
                BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR,
            )
        except Exception:
            pass
        writer.mark_failed(
            step=writer._read().get("current_step", "unknown"),
            error_class=type(e).__name__,
            error_message=str(e),
        )
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--to", choices=["side_car", "cloud"], required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--duckdb-path", type=Path, default=Path("/data/state/system.duckdb"))
    parser.add_argument("--jobs-dir", type=Path, default=Path("/data/state/db-jobs"))
    parser.add_argument("--backups-dir", type=Path, default=Path("/data/state/backups"))
    args = parser.parse_args()

    sys.exit(main(
        job_id=args.job_id,
        to=args.to,
        target_url=args.target_url,
        duckdb_path=args.duckdb_path,
        jobs_dir=args.jobs_dir,
        backups_dir=args.backups_dir,
    ))
```

- [ ] **Step 3: Run e2e test**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py::test_main_duckdb_to_side_car_end_to_end -v --tb=short --timeout=180 2>&1 | tail -10
```

Expect: PASS.

- [ ] **Step 4: Run all migrator tests**

```bash
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/test_db_state_migrator.py -v --tb=short --timeout=180 2>&1 | tail -10
```

Expect: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/db_state_migrator.py tests/test_db_state_migrator.py
git commit -m "feat(db): db_state_migrator main() — end-to-end orchestration"
```

---

## Phase 3 — API endpoints

### Task 3.1: GET /api/admin/db/state

**Files:**
- Create: `app/api/db_state.py`
- Modify: `app/main.py`
- Create: `tests/test_api_db_state.py`

- [ ] **Step 1: Test GET endpoint returns shape**

```python
# tests/test_api_db_state.py
"""Integration tests for /api/admin/db/* endpoints."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Use existing admin fixture if codebase has one; otherwise:
    from app.main import app
    return TestClient(app)


def test_get_db_state_default_duckdb(admin_client, monkeypatch):
    # Bypass admin auth for test (use existing pattern from other admin tests)
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    r = admin_client.get("/api/admin/db/state")
    assert r.status_code == 200
    data = r.json()
    assert data["backend"] == "duckdb"
    assert data["url_redacted"] is None
    assert data["allowed_transitions"] == ["side_car"]
    assert data["current_job_id"] is None
```

NOTE: existing tests under `tests/` use various auth bypass patterns. Inspect `tests/test_admin_configure_api.py` for the canonical TestClient + admin pattern in this codebase, adapt accordingly.

- [ ] **Step 2: Run, expect FAIL**

```bash
.venv/bin/pytest tests/test_api_db_state.py::test_get_db_state_default_duckdb -v --tb=short --timeout=30 2>&1 | tail -10
```

- [ ] **Step 3: Create router**

```python
# app/api/db_state.py
"""Admin endpoints for DB backend state machine.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from src.db_state_machine import (
    BackendState,
    allowed_transitions,
    read_backend_state,
)

router = APIRouter(prefix="/api/admin/db", tags=["admin-db"])

_JOBS_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-jobs"


def _redact_url(url: str | None) -> str | None:
    """Replace password in postgresql://user:PASS@host with ****."""
    if not url:
        return None
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1****\2", url)


def _current_job_id() -> str | None:
    """Return job_id of any currently-running job, else None."""
    if not _JOBS_DIR.exists():
        return None
    for path in _JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("status") == "running":
                return data["job_id"]
        except (json.JSONDecodeError, KeyError):
            continue
    return None


@router.get("/state", dependencies=[Depends(require_admin)])
def get_db_state() -> dict:
    """Current backend + allowed transitions + in-progress job (if any)."""
    state, url = read_backend_state()
    return {
        "backend": state.value,
        "url_redacted": _redact_url(url),
        "allowed_transitions": [t.value for t in allowed_transitions(state)],
        "current_job_id": _current_job_id(),
    }
```

- [ ] **Step 4: Register router in app/main.py**

Find the existing router registration block (around line where other `app.include_router(...)` calls live) and add:

```python
from app.api import db_state as db_state_api
app.include_router(db_state_api.router)
```

- [ ] **Step 5: Run test**

```bash
.venv/bin/pytest tests/test_api_db_state.py::test_get_db_state_default_duckdb -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/db_state.py app/main.py tests/test_api_db_state.py
git commit -m "feat(api): GET /api/admin/db/state — current backend + transitions"
```

### Task 3.2: POST /api/admin/db/migrate

**Files:**
- Modify: `app/api/db_state.py`
- Modify: `tests/test_api_db_state.py`

- [ ] **Step 1: Test POST creates job + returns 202**

Append:

```python
def test_post_migrate_starts_job(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    r = admin_client.post("/api/admin/db/migrate", json={"target": "side_car"})
    assert r.status_code == 202
    body = r.json()
    assert "job_id" in body
    assert body["status"] == "running"


def test_post_migrate_rejects_invalid_transition(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # current state = duckdb, target = cloud (must go side_car first)
    r = admin_client.post("/api/admin/db/migrate", json={"target": "cloud"})
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"].lower()


def test_post_migrate_409_when_in_progress(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # First request starts job
    r1 = admin_client.post("/api/admin/db/migrate", json={"target": "side_car"})
    assert r1.status_code == 202

    # Second concurrent request rejected
    r2 = admin_client.post("/api/admin/db/migrate", json={"target": "side_car"})
    assert r2.status_code == 409
    assert "in_progress" in r2.json()["detail"].lower() or "already" in r2.json()["detail"].lower()
```

- [ ] **Step 2: Implement endpoint**

Append to `app/api/db_state.py`:

```python
import subprocess
import sys
import uuid


class MigrateRequest(BaseModel):
    target: str  # "side_car" or "cloud"
    cloud_url: str | None = None  # required when target=cloud


@router.post("/migrate", status_code=202, dependencies=[Depends(require_admin)])
def start_migration(payload: MigrateRequest) -> dict:
    """Start a backend migration job (async; poll /job/{id} for status)."""
    from src.db_state_machine import (
        BackendState,
        InvalidTransitionError,
        MigrationInProgressError,
        MigrationLock,
        validate_transition,
        write_backend_state,
    )

    current_state, _ = read_backend_state()
    try:
        target_state = BackendState(payload.target)
    except ValueError:
        raise HTTPException(400, detail=f"Unknown target: {payload.target}")

    try:
        validate_transition(current_state, target_state)
    except InvalidTransitionError as e:
        raise HTTPException(400, detail=str(e))

    if payload.target == "cloud" and not payload.cloud_url:
        raise HTTPException(400, detail="cloud_url required for target=cloud")

    # Resolve target URL
    if payload.target == "side_car":
        # Side-car URL is determined by env (compose POSTGRES_PASSWORD)
        password = os.environ.get("POSTGRES_PASSWORD", "agnes")
        target_url = f"postgresql+psycopg://agnes:{password}@postgres:5432/agnes"
    else:
        target_url = payload.cloud_url

    job_id = str(uuid.uuid4())

    # Acquire lock + write in-progress state
    try:
        lock = MigrationLock()
        lock.__enter__()
    except MigrationInProgressError as e:
        existing = _current_job_id()
        raise HTTPException(
            409, detail=f"Migration already in progress: job {existing}"
        )

    try:
        in_progress = (
            BackendState.SIDE_CAR_IN_PROGRESS if payload.target == "side_car"
            else BackendState.CLOUD_IN_PROGRESS
        )
        write_backend_state(in_progress)

        # Spawn subprocess (detached — child of init after exec; will outlive
        # this request thread but not the app container).
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        subprocess.Popen(
            [
                sys.executable, "-m", "scripts.db_state_migrator",
                "--job-id", job_id,
                "--to", payload.target,
                "--target-url", target_url,
                "--duckdb-path", str(data_dir / "state" / "system.duckdb"),
                "--jobs-dir", str(data_dir / "state" / "db-jobs"),
                "--backups-dir", str(data_dir / "state" / "backups"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # Release lock immediately; subprocess holds its own flock internally
        lock.__exit__(None, None, None)

    return {"job_id": job_id, "status": "running"}
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_api_db_state.py -v --tb=short --timeout=30 2>&1 | tail -15
```

Expect: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "feat(api): POST /api/admin/db/migrate — spawn migration subprocess"
```

### Task 3.3: GET /api/admin/db/job/{job_id}

**Files:**
- Modify: `app/api/db_state.py`
- Modify: `tests/test_api_db_state.py`

- [ ] **Step 1: Test GET job status**

Append:

```python
def test_get_job_returns_status(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Seed a job file
    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "success",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00", "completed_at": "2026-05-27T16:02:00+00:00",
        "current_step": "flip_backend", "progress_pct": 100,
        "summary": {"tables_migrated": 28, "rows_total": 1234},
        "error": None,
    }))

    r = admin_client.get("/api/admin/db/job/abc")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert data["summary"]["tables_migrated"] == 28


def test_get_job_404_unknown(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    r = admin_client.get("/api/admin/db/job/nonexistent")
    assert r.status_code == 404
```

(`import json` needed at top of test file if not already.)

- [ ] **Step 2: Implement endpoint**

Append to `app/api/db_state.py`:

```python
@router.get("/job/{job_id}", dependencies=[Depends(require_admin)])
def get_job(job_id: str) -> dict:
    """Return migration job status."""
    path = _JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")
    return json.loads(path.read_text())
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_api_db_state.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: 6 passed.

- [ ] **Step 4: Commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "feat(api): GET /api/admin/db/job/{id} — poll job status"
```

### Task 3.4: POST /api/admin/db/cancel/{job_id}

**Files:**
- Modify: `app/api/db_state.py`
- Modify: `tests/test_api_db_state.py`

- [ ] **Step 1: Test cancel updates job + revert state**

Append:

```python
def test_post_cancel_marks_job_cancelled(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "running",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00", "completed_at": None,
        "current_step": "data_copy", "progress_pct": 50,
        "summary": None, "error": None,
    }))

    r = admin_client.post("/api/admin/db/cancel/abc")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True

    data = json.loads((jobs_dir / "abc.json").read_text())
    assert data["status"] == "cancelled"


def test_post_cancel_409_after_flip_backend(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_TEST_BYPASS_AUTH", "admin@example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    jobs_dir = tmp_path / "state" / "db-jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "abc.json").write_text(json.dumps({
        "schema_version": 1, "job_id": "abc", "status": "running",
        "source_backend": "duckdb", "target_backend": "side_car",
        "started_at": "2026-05-27T16:00:00+00:00", "completed_at": None,
        "current_step": "flip_backend", "progress_pct": 95,
        "summary": None, "error": None,
    }))

    r = admin_client.post("/api/admin/db/cancel/abc")
    assert r.status_code == 409
```

- [ ] **Step 2: Implement endpoint**

Append:

```python
@router.post("/cancel/{job_id}", dependencies=[Depends(require_admin)])
def cancel_job(job_id: str) -> dict:
    """Cancel a running migration before point-of-no-return."""
    path = _JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(404, detail=f"Unknown job_id: {job_id}")

    data = json.loads(path.read_text())
    if data["status"] != "running":
        raise HTTPException(
            400, detail=f"Job is {data['status']}; cannot cancel non-running job"
        )
    if data["current_step"] in ("flip_backend", "app_restart", "verify_health"):
        raise HTTPException(
            409,
            detail="Past point-of-no-return (step >= flip_backend); manual recovery required"
        )

    from datetime import datetime, timezone
    data["status"] = "cancelled"
    data["completed_at"] = datetime.now(timezone.utc).isoformat()
    data["error"] = {
        "step": data["current_step"],
        "class": "Cancelled",
        "message": "Admin cancelled migration",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)

    # Revert state machine
    from src.db_state_machine import BackendState, write_backend_state
    revert = BackendState.DUCKDB if data["target_backend"] == "side_car" else BackendState.SIDE_CAR
    write_backend_state(revert)

    return {"cancelled": True}
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_api_db_state.py -v --tb=short --timeout=30 2>&1 | tail -10
```

Expect: 8 passed.

- [ ] **Step 4: Commit**

```bash
git add app/api/db_state.py tests/test_api_db_state.py
git commit -m "feat(api): POST /api/admin/db/cancel/{id} — abort running job"
```

---

## Phase 4 — Host-side applier

### Task 4.1: agnes-state-applier.sh

**Files:**
- Create: `scripts/ops/agnes-state-applier.sh`
- Create: `scripts/ops/agnes-state-applier.service`
- Create: `scripts/ops/agnes-state-applier.timer`

- [ ] **Step 1: Write the script**

```bash
# scripts/ops/agnes-state-applier.sh
#!/bin/bash
# Host-side daemon. Reads /data/state/db-state-target.flag and
# /data/state/instance.yaml to determine desired compose lifecycle;
# applies docker compose changes. Runs every 30s via systemd timer.
#
# Behavior:
#   - flag = "duckdb"            → ensure postgres container NOT in COMPOSE_FILE
#   - flag = "side-car-enabled"  → ensure postgres.yml + postgres-host-mount.yml
#                                   in COMPOSE_FILE; docker compose up -d postgres
#   - flag = "cloud-only"        → remove postgres.yml from COMPOSE_FILE;
#                                   docker compose stop postgres + docker rm postgres
#
# Idempotent: if current state matches desired, no-op.
set -euo pipefail

FLAG=/data/state/db-state-target.flag
COMPOSE_DIR=/opt/agnes

if [ ! -f "$FLAG" ]; then
    # No flag yet → default duckdb (no postgres overlay)
    exit 0
fi

TARGET="$(cat "$FLAG" | tr -d '[:space:]')"

cd "$COMPOSE_DIR"
# shellcheck disable=SC1091
set -a; . "$COMPOSE_DIR/.env"; set +a

case "$TARGET" in
    duckdb|cloud-only)
        # Strip postgres.yml + postgres-host-mount.yml from COMPOSE_FILE
        NEW_COMPOSE_FILE=$(echo "$COMPOSE_FILE" | tr ':' '\n' | \
            grep -vE 'docker-compose\.(postgres|postgres-host-mount)\.yml$' | \
            tr '\n' ':' | sed 's/:$//')

        if [ "$TARGET" = "cloud-only" ]; then
            docker compose stop postgres 2>/dev/null || true
            docker compose rm -f postgres 2>/dev/null || true
        fi
        ;;
    side-car-enabled)
        NEW_COMPOSE_FILE="$COMPOSE_FILE"
        if ! echo "$COMPOSE_FILE" | grep -q "docker-compose.postgres.yml"; then
            NEW_COMPOSE_FILE="${NEW_COMPOSE_FILE}:docker-compose.postgres.yml"
        fi
        if ! echo "$COMPOSE_FILE" | grep -q "docker-compose.postgres-host-mount.yml"; then
            NEW_COMPOSE_FILE="${NEW_COMPOSE_FILE}:docker-compose.postgres-host-mount.yml"
        fi

        # Ensure /data/postgres exists with uid 70 ownership
        mkdir -p /data/postgres
        chown 70:70 /data/postgres
        chmod 700 /data/postgres

        # Update .env COMPOSE_FILE line
        sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$NEW_COMPOSE_FILE|" "$COMPOSE_DIR/.env"

        export COMPOSE_FILE="$NEW_COMPOSE_FILE"
        docker compose up -d postgres
        ;;
    *)
        logger -t agnes-state-applier "Unknown target: $TARGET — ignoring"
        exit 0
        ;;
esac

# If COMPOSE_FILE changed, also recreate app + scheduler
if [ "${COMPOSE_FILE:-}" != "${NEW_COMPOSE_FILE:-}" ]; then
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$NEW_COMPOSE_FILE|" "$COMPOSE_DIR/.env"
    export COMPOSE_FILE="$NEW_COMPOSE_FILE"
    docker compose up -d --force-recreate app scheduler
fi
```

- [ ] **Step 2: Write systemd unit files**

```ini
# scripts/ops/agnes-state-applier.service
[Unit]
Description=Agnes DB backend state applier — apply target compose lifecycle
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/agnes-state-applier.sh
```

```ini
# scripts/ops/agnes-state-applier.timer
[Unit]
Description=Run Agnes DB backend state applier every 30s

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
Unit=agnes-state-applier.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Lint shell script**

```bash
bash -n scripts/ops/agnes-state-applier.sh
shellcheck scripts/ops/agnes-state-applier.sh 2>&1 | head -20 || true
```

Fix any SC errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/ops/agnes-state-applier.sh scripts/ops/agnes-state-applier.service scripts/ops/agnes-state-applier.timer
git commit -m "feat(ops): agnes-state-applier — host-side compose lifecycle daemon"
```

### Task 4.2: Install applier in startup-script.sh.tpl

**Files:**
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl`

- [ ] **Step 1: Read existing startup-script — find systemd install pattern**

```bash
grep -n "systemctl\|/usr/local/bin/agnes\|tee\|RAW_BASE" infra/modules/customer-instance/startup-script.sh.tpl | head -20
```

Note: existing pattern installs `agnes-auto-upgrade.sh` + its timer. Mirror that.

- [ ] **Step 2: Add applier install block**

After the existing `agnes-auto-upgrade` install, append:

```bash
# Install agnes-state-applier (DB backend state machine — applies
# compose lifecycle changes when /data/state/db-state-target.flag changes).
curl -fsSL "$RAW_BASE/scripts/ops/agnes-state-applier.sh" \
    -o /usr/local/bin/agnes-state-applier.sh
chmod 755 /usr/local/bin/agnes-state-applier.sh

curl -fsSL "$RAW_BASE/scripts/ops/agnes-state-applier.service" \
    -o /etc/systemd/system/agnes-state-applier.service
curl -fsSL "$RAW_BASE/scripts/ops/agnes-state-applier.timer" \
    -o /etc/systemd/system/agnes-state-applier.timer
systemctl daemon-reload
systemctl enable --now agnes-state-applier.timer

# Initial instance.yaml::database = {backend: "duckdb"} so the app
# starts in DuckDB mode even before any admin migration.
INSTANCE_YAML="$DATA_MNT/state/instance.yaml"
if [ ! -f "$INSTANCE_YAML" ]; then
    mkdir -p "$DATA_MNT/state"
    cat > "$INSTANCE_YAML" <<YAML
database:
  backend: duckdb
YAML
    chown 999:999 "$INSTANCE_YAML"
fi
```

- [ ] **Step 3: terraform validate**

```bash
cd infra/modules/customer-instance
terraform init -backend=false 2>&1 | tail -3
terraform validate
cd ../../..
```

Expect: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/modules/customer-instance/startup-script.sh.tpl
git commit -m "feat(infra): install agnes-state-applier.timer on VM provision"
```

---

## Phase 5 — CLI

### Task 5.1: `agnes admin db state` command

**Files:**
- Create: `cli/commands/db.py`
- Modify: `cli/main.py` (or wherever subgroups register; check structure)
- Create: `tests/test_cli_db.py`

- [ ] **Step 1: Inspect existing CLI patterns**

```bash
grep -n "^@click\|def admin\|admin_group\|@admin\.group" cli/commands/admin.py cli/main.py 2>/dev/null | head -10
ls cli/commands/ | head -10
```

- [ ] **Step 2: Test `agnes admin db state` invocation**

```python
# tests/test_cli_db.py
"""CLI smoke tests for `agnes admin db ...`."""
from __future__ import annotations
import json
import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


def test_db_state_json(runner, monkeypatch):
    """`agnes admin db state --json` returns valid JSON."""
    monkeypatch.setenv("AGNES_SERVER_URL", "https://test.example.com")
    monkeypatch.setenv("AGNES_TOKEN", "test-pat")

    # Mock HTTP layer
    def mock_get(*a, **kw):
        class R:
            status_code = 200
            def json(self): return {
                "backend": "duckdb",
                "url_redacted": None,
                "allowed_transitions": ["side_car"],
                "current_job_id": None,
            }
        return R()
    monkeypatch.setattr("requests.get", mock_get)

    from cli.commands.db import db
    result = runner.invoke(db, ["state", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["backend"] == "duckdb"
```

- [ ] **Step 3: Run, expect FAIL**

```bash
.venv/bin/pytest tests/test_cli_db.py::test_db_state_json -v --tb=short --timeout=30 2>&1 | tail -5
```

- [ ] **Step 4: Implement**

```python
# cli/commands/db.py
"""CLI: agnes admin db state | migrate | job | cancel.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json as _json
import os
import sys
import time

import click
import requests


def _credentials() -> tuple[str, str]:
    server = os.environ.get("AGNES_SERVER_URL") or _read_cred("server_url")
    token = os.environ.get("AGNES_TOKEN") or _read_cred("token")
    if not server or not token:
        click.echo("Error: AGNES_SERVER_URL + AGNES_TOKEN required (or ~/.agnes/credentials)", err=True)
        sys.exit(2)
    return server, token


def _read_cred(key: str) -> str | None:
    """Stub — match existing CLI credentials lookup."""
    return None  # Inspect cli/commands/admin.py for real impl; reuse


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@click.group()
def db():
    """Manage Agnes app-state DB backend (DuckDB / Postgres)."""


@db.command()
@click.option("--json", "as_json", is_flag=True, help="Output JSON for scripting")
def state(as_json: bool):
    """Show current DB backend state."""
    server, token = _credentials()
    r = requests.get(f"{server}/api/admin/db/state", headers=_headers(token), timeout=10)
    if r.status_code != 200:
        click.echo(f"Error {r.status_code}: {r.text}", err=True)
        sys.exit(1)
    data = r.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Backend:    {data['backend']}")
    click.echo(f"URL:        {data['url_redacted'] or '(none)'}")
    click.echo(f"Transitions: {', '.join(data['allowed_transitions']) or '(terminal)'}")
    if data.get("current_job_id"):
        click.echo(f"Active job: {data['current_job_id']}")
```

- [ ] **Step 5: Register `db` group**

Find `cli/main.py` (or `cli/commands/admin.py`) and add the registration. If `admin` is a Click group:

```python
# In the file that defines the admin group, after the group definition:
from cli.commands.db import db as _db
admin.add_command(_db, name="db")
```

- [ ] **Step 6: Run test**

```bash
.venv/bin/pytest tests/test_cli_db.py -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: PASS.

- [ ] **Step 7: Commit**

```bash
git add cli/commands/db.py cli/main.py tests/test_cli_db.py
git commit -m "feat(cli): agnes admin db state"
```

### Task 5.2: `agnes admin db migrate <target>` command

**Files:**
- Modify: `cli/commands/db.py`
- Modify: `tests/test_cli_db.py`

- [ ] **Step 1: Test migrate command**

Append:

```python
def test_db_migrate_starts_job(runner, monkeypatch):
    monkeypatch.setenv("AGNES_SERVER_URL", "https://test.example.com")
    monkeypatch.setenv("AGNES_TOKEN", "test-pat")

    def mock_post(*a, **kw):
        class R:
            status_code = 202
            def json(self): return {"job_id": "abc-123", "status": "running"}
        return R()
    monkeypatch.setattr("requests.post", mock_post)

    from cli.commands.db import db
    result = runner.invoke(db, ["migrate", "side_car", "--detach"])
    assert result.exit_code == 0
    assert "abc-123" in result.output
```

- [ ] **Step 2: Implement**

Append to `cli/commands/db.py`:

```python
@db.command()
@click.argument("target", type=click.Choice(["side_car", "cloud"]))
@click.option("--cloud-url", help="Cloud PG URL (required for target=cloud)")
@click.option("--detach", is_flag=True, help="Return immediately; don't poll progress")
@click.option("--json", "as_json", is_flag=True, help="Output JSON only")
@click.option("--timeout", default=600, help="Max seconds to wait for completion (default 600)")
def migrate(target: str, cloud_url: str | None, detach: bool, as_json: bool, timeout: int):
    """Migrate to next backend state (side_car or cloud)."""
    server, token = _credentials()

    if target == "cloud" and not cloud_url:
        cloud_url = click.prompt("Cloud PG connection string", hide_input=False)

    payload = {"target": target}
    if cloud_url:
        payload["cloud_url"] = cloud_url

    r = requests.post(
        f"{server}/api/admin/db/migrate",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    if r.status_code != 202:
        click.echo(f"Error {r.status_code}: {r.text}", err=True)
        sys.exit(1)

    body = r.json()
    job_id = body["job_id"]

    if detach or as_json:
        click.echo(_json.dumps(body) if as_json else f"Job started: {job_id}")
        return

    # Poll until done
    click.echo(f"Job started: {job_id} (polling, Ctrl-C to detach)")
    deadline = time.time() + timeout
    last_step = None
    while time.time() < deadline:
        time.sleep(2)
        jr = requests.get(
            f"{server}/api/admin/db/job/{job_id}",
            headers=_headers(token),
            timeout=10,
        )
        if jr.status_code != 200:
            click.echo(f"Poll error {jr.status_code}", err=True)
            continue
        job = jr.json()
        if job["current_step"] != last_step:
            click.echo(f"  [{job['progress_pct']:>3}%] {job['current_step']}")
            last_step = job["current_step"]
        if job["status"] in ("success", "failed", "cancelled"):
            click.echo(f"  Result: {job['status']}")
            if job["status"] == "failed":
                click.echo(f"  Error at {job['error']['step']}: {job['error']['message']}", err=True)
                sys.exit(1)
            return
    click.echo("Timeout — job still running. Run `agnes admin db job <id>` to check.", err=True)
    sys.exit(2)
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_cli_db.py -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add cli/commands/db.py tests/test_cli_db.py
git commit -m "feat(cli): agnes admin db migrate <target>"
```

### Task 5.3: `agnes admin db job` + `cancel` commands

**Files:**
- Modify: `cli/commands/db.py`
- Modify: `tests/test_cli_db.py`

- [ ] **Step 1: Test job + cancel**

Append:

```python
def test_db_job_outputs_status(runner, monkeypatch):
    monkeypatch.setenv("AGNES_SERVER_URL", "https://test.example.com")
    monkeypatch.setenv("AGNES_TOKEN", "test-pat")

    def mock_get(*a, **kw):
        class R:
            status_code = 200
            def json(self): return {
                "job_id": "abc-123", "status": "success",
                "current_step": "flip_backend", "progress_pct": 100,
                "summary": {"tables_migrated": 28},
            }
        return R()
    monkeypatch.setattr("requests.get", mock_get)

    from cli.commands.db import db
    result = runner.invoke(db, ["job", "abc-123", "--json"])
    assert result.exit_code == 0
    assert "success" in result.output


def test_db_cancel(runner, monkeypatch):
    monkeypatch.setenv("AGNES_SERVER_URL", "https://test.example.com")
    monkeypatch.setenv("AGNES_TOKEN", "test-pat")

    def mock_post(*a, **kw):
        class R:
            status_code = 200
            def json(self): return {"cancelled": True}
        return R()
    monkeypatch.setattr("requests.post", mock_post)

    from cli.commands.db import db
    result = runner.invoke(db, ["cancel", "abc-123"])
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
```

- [ ] **Step 2: Implement**

Append:

```python
@db.command()
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True)
def job(job_id: str, as_json: bool):
    """Show status of a migration job."""
    server, token = _credentials()
    r = requests.get(f"{server}/api/admin/db/job/{job_id}", headers=_headers(token), timeout=10)
    if r.status_code != 200:
        click.echo(f"Error {r.status_code}: {r.text}", err=True)
        sys.exit(1)
    data = r.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Job:    {data['job_id']}")
    click.echo(f"Status: {data['status']}")
    click.echo(f"Step:   {data['current_step']} ({data['progress_pct']}%)")
    if data.get("error"):
        click.echo(f"Error:  {data['error']['message']} (at {data['error']['step']})")
    if data.get("summary"):
        click.echo(f"Summary: {data['summary']}")


@db.command()
@click.argument("job_id")
def cancel(job_id: str):
    """Cancel a running migration job."""
    server, token = _credentials()
    r = requests.post(f"{server}/api/admin/db/cancel/{job_id}", headers=_headers(token), timeout=10)
    if r.status_code == 200:
        click.echo(f"Job {job_id} cancelled.")
    elif r.status_code == 409:
        click.echo(f"Cannot cancel: {r.json().get('detail')}", err=True)
        sys.exit(1)
    else:
        click.echo(f"Error {r.status_code}: {r.text}", err=True)
        sys.exit(1)
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_cli_db.py -v --tb=short --timeout=30 2>&1 | tail -5
```

Expect: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add cli/commands/db.py tests/test_cli_db.py
git commit -m "feat(cli): agnes admin db job + cancel commands"
```

---

## Phase 6 — Admin UI

### Task 6.1: db_state.js — polling + progress

**Files:**
- Create: `app/web/static/js/admin/db_state.js`

- [ ] **Step 1: Write JS module**

```javascript
// app/web/static/js/admin/db_state.js
// /admin/server-config "Database backend" section — current state card,
// transition buttons, modal for cloud URL, progress polling.
//
// Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md

const DBState = {
  async fetchState() {
    const r = await fetch('/api/admin/db/state');
    if (!r.ok) throw new Error(`state fetch ${r.status}`);
    return r.json();
  },

  async startMigration(target, cloudUrl) {
    const body = { target };
    if (cloudUrl) body.cloud_url = cloudUrl;
    const r = await fetch('/api/admin/db/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status === 409) {
      const e = await r.json();
      throw new Error(`Migration already running: ${e.detail}`);
    }
    if (!r.ok) {
      const e = await r.json();
      throw new Error(e.detail || `migrate fetch ${r.status}`);
    }
    return r.json();
  },

  async fetchJob(jobId) {
    const r = await fetch(`/api/admin/db/job/${jobId}`);
    if (!r.ok) throw new Error(`job fetch ${r.status}`);
    return r.json();
  },

  async cancelJob(jobId) {
    const r = await fetch(`/api/admin/db/cancel/${jobId}`, { method: 'POST' });
    if (!r.ok) {
      const e = await r.json();
      throw new Error(e.detail || `cancel ${r.status}`);
    }
    return r.json();
  },

  renderState(data) {
    const el = document.getElementById('db-state-card');
    if (!el) return;
    const backend = data.backend;
    const transitionBtns = data.allowed_transitions.map(t => {
      const label = t === 'side_car' ? 'Enable side-car Postgres' : 'Migrate to managed Postgres';
      return `<button class="btn btn-primary" data-target="${t}">${label}</button>`;
    }).join(' ');

    el.innerHTML = `
      <div class="card">
        <h3>Database backend</h3>
        <p><strong>Current:</strong> ${backend}</p>
        <p><strong>URL:</strong> ${data.url_redacted || '(none — DuckDB)'}</p>
        <div class="actions">${transitionBtns}</div>
      </div>
    `;

    el.querySelectorAll('button[data-target]').forEach(btn => {
      btn.addEventListener('click', () => this.handleTransitionClick(btn.dataset.target));
    });

    if (data.current_job_id) {
      this.startPolling(data.current_job_id);
    }
  },

  async handleTransitionClick(target) {
    let cloudUrl = null;
    if (target === 'cloud') {
      cloudUrl = prompt('Cloud PG connection string (postgresql+psycopg://user:pass@host:5432/db):');
      if (!cloudUrl) return;
    }
    try {
      const { job_id } = await this.startMigration(target, cloudUrl);
      this.startPolling(job_id);
    } catch (e) {
      alert(`Migration failed to start: ${e.message}`);
    }
  },

  startPolling(jobId) {
    const progress = document.getElementById('db-migration-progress');
    if (!progress) return;
    progress.style.display = 'block';

    const tick = async () => {
      try {
        const job = await this.fetchJob(jobId);
        progress.innerHTML = `
          <div class="job-status">
            <div>Job <code>${jobId}</code></div>
            <div>Step: <strong>${job.current_step}</strong> (${job.progress_pct}%)</div>
            <div class="progress-bar"><div style="width: ${job.progress_pct}%"></div></div>
            ${job.error ? `<div class="error">Error: ${job.error.message}</div>` : ''}
            <button class="btn btn-secondary" id="db-cancel-btn">Cancel</button>
          </div>
        `;
        document.getElementById('db-cancel-btn')?.addEventListener('click', async () => {
          try {
            await this.cancelJob(jobId);
            alert('Cancelled');
          } catch (e) {
            alert(`Cancel failed: ${e.message}`);
          }
        });
        if (['success', 'failed', 'cancelled'].includes(job.status)) {
          clearInterval(this._poll);
          setTimeout(() => location.reload(), 2000);
        }
      } catch (e) {
        // Silently swallow transient fetch errors; next tick retries.
      }
    };
    tick();
    this._poll = setInterval(tick, 2000);
  },

  async init() {
    try {
      const data = await this.fetchState();
      this.renderState(data);
    } catch (e) {
      console.error('DBState init failed', e);
    }
  },
};

document.addEventListener('DOMContentLoaded', () => DBState.init());
```

- [ ] **Step 2: Smoke-check syntax**

```bash
node -c app/web/static/js/admin/db_state.js 2>&1 | head -5
```

If node not installed, skip — Python-side template will catch it on render.

- [ ] **Step 3: Commit**

```bash
git add app/web/static/js/admin/db_state.js
git commit -m "feat(ui): db_state.js — admin /admin/server-config polling + modal"
```

### Task 6.2: admin_server_config.html — DB backend section

**Files:**
- Modify: `app/web/templates/admin_server_config.html`

- [ ] **Step 1: Find a clear insertion point (end of file, before closing `{% endblock %}`)**

```bash
grep -n "{% endblock\|{% block " app/web/templates/admin_server_config.html | tail -5
```

- [ ] **Step 2: Add section before final `{% endblock %}`**

Append (just before the last `{% endblock %}`):

```html
<!-- ==== Database backend (state machine) ==== -->
<section id="db-backend-section">
  <h2>Database backend</h2>
  <p>Switch app-state DB between DuckDB, side-car Postgres, and a managed cloud Postgres.
     See <a href="/docs/postgres-cutover-runbook">runbook</a> for operator details.</p>

  <div id="db-state-card"><!-- populated by db_state.js --></div>
  <div id="db-migration-progress" style="display: none;"></div>
</section>

<script src="{{ url_for('static', path='/js/admin/db_state.js') }}"></script>
```

- [ ] **Step 3: Smoke-render via existing admin page test**

```bash
.venv/bin/pytest tests/test_admin_server_config.py -q --timeout=30 2>&1 | tail -5
```

If the test file doesn't exist, just curl the page in dev:

```bash
.venv/bin/python -m http.server 8000 &  # NOPE — use uvicorn:
# (skip explicit dev-server step; the existing admin page tests cover render)
```

Expect: tests pass (template still valid Jinja).

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/admin_server_config.html
git commit -m "feat(ui): /admin/server-config — Database backend section"
```

---

## Phase 7 — E2E + integration

### Task 7.1: Full DuckDB → side-car E2E via pgserver

**Files:**
- Create: `tests/db_pg/test_db_state_e2e.py`

- [ ] **Step 1: Write end-to-end test**

```python
# tests/db_pg/test_db_state_e2e.py
"""End-to-end: full DuckDB → side-car migration via state machine.

Uses pgserver as the "side-car" target. Validates that:
  1. POST /migrate spawns subprocess
  2. Job moves through steps to success
  3. Row counts match between DuckDB and PG
  4. instance.yaml flipped to side_car
  5. use_pg() now returns True; factory routes to PG
"""
from __future__ import annotations
from pathlib import Path
import json
import time

import pytest


@pytest.mark.timeout(180)
def test_full_duckdb_to_side_car_migration(tmp_path, pg_engine, monkeypatch):
    # Setup
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Seed DuckDB with some rows
    import duckdb
    from src.db import _ensure_schema
    duck_path = tmp_path / "state" / "system.duckdb"
    duck_path.parent.mkdir(parents=True)
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'alice@x', 'Alice')")
    conn.close()

    # Run migrator directly (mimicking subprocess spawn)
    from scripts.db_state_migrator import main
    jobs_dir = tmp_path / "state" / "db-jobs"
    backups_dir = tmp_path / "state" / "backups"

    rc = main(
        job_id="e2e-1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, f"main() returned {rc}"

    # Job status = success
    job = json.loads((jobs_dir / "e2e-1.json").read_text())
    assert job["status"] == "success"

    # Backend flipped
    from src.db_state_machine import BackendState, read_backend_state
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR

    # Backup written
    backups = list(backups_dir.glob("duckdb-pre-sidecar-*.duckdb.gz"))
    assert len(backups) == 1

    # use_pg now True
    # Need to clear any cached state; instance.yaml-driven so no env needed
    from src.repositories import use_pg
    assert use_pg() is True

    # Rows visible in PG
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(sa.text("SELECT email FROM users WHERE id = 'u1'")).fetchone()
    assert row[0] == "alice@x"
```

- [ ] **Step 2: Run e2e**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/test_db_state_e2e.py -v --tb=short --timeout=180 2>&1 | tail -10
```

Expect: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/db_pg/test_db_state_e2e.py
git commit -m "test(db): full DuckDB → side-car E2E via state machine"
```

### Task 7.2: Full PG suite regression check

**Files:** (no changes; verification only)

- [ ] **Step 1: Run full PG suite**

```bash
unset UV_PYTHON
AGNES_TEST_PG_BACKEND=pgserver .venv/bin/pytest tests/db_pg/ -q --tb=line --timeout=300 2>&1 | tail -5
```

Expect: All previously-passing tests still pass + new e2e test. Investigate any regression.

- [ ] **Step 2: Run full DuckDB suite (sanity)**

```bash
.venv/bin/pytest tests/ --ignore=tests/db_pg -q --tb=line --timeout=300 2>&1 | tail -5
```

Expect: green or only pre-existing flakies.

- [ ] **Step 3: Commit (no code change, but record verification)**

```bash
# No commit needed unless you adjusted any tests during regression-fix
```

---

## Phase 8 — Docs + CHANGELOG + push

### Task 8.1: Update postgres-cutover-runbook.md

**Files:**
- Modify: `docs/postgres-cutover-runbook.md`

- [ ] **Step 1: Add UI/CLI section + manual smoke checklist**

Append (or insert near top after "What changed" section):

```markdown
## Admin UI / CLI

The state machine is admin-controlled. There are two entry points:

### Web UI

1. Navigate to `/admin/server-config`
2. Scroll to "Database backend" section
3. Card shows current backend + available transitions
4. Click "Enable side-car Postgres" → confirm → progress bar appears
5. Wait for "Migration complete" banner (2-3 min for typical instance)
6. Verify by reloading and seeing backend=`side_car`

### CLI

```bash
# Show current state
agnes admin db state

# Migrate to side-car PG (default password from .env)
agnes admin db migrate side_car

# Migrate to managed PG (will prompt for connection string)
agnes admin db migrate cloud
# Or non-interactive:
agnes admin db migrate cloud --cloud-url 'postgresql+psycopg://agnes:PASS@host:5432/agnes'

# Poll a job
agnes admin db job <job_id>

# Cancel a running job (pre-flip)
agnes admin db cancel <job_id>
```

## Manual smoke (agnes-dev)

After deploying this PR to agnes-dev:

1. SSH agnes-dev, check baseline:
   ```bash
   curl http://localhost:8000/api/admin/db/state -H "Authorization: Bearer $PAT"
   # Expect: {backend: "duckdb", ...}
   ```
2. Trigger migration from UI or CLI:
   ```bash
   agnes admin db migrate side_car
   ```
3. Watch:
   ```bash
   docker ps                     # postgres container should appear within 1 min
   docker logs agnes-postgres-1  # should show "ready to accept connections"
   ```
4. After job completes, verify:
   ```bash
   agnes admin db state
   # Expect: {backend: "side_car", ...}
   curl http://localhost:8000/api/health
   # Expect: 200
   ```
5. Inspect backup:
   ```bash
   ls /data/state/backups/
   # Expect: duckdb-pre-sidecar-*.duckdb.gz
   ```
```

- [ ] **Step 2: Commit**

```bash
git add docs/postgres-cutover-runbook.md
git commit -m "docs: postgres-cutover-runbook — UI/CLI + manual smoke"
```

### Task 8.2: CHANGELOG bullet

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add Added bullet under [Unreleased]**

Find `## [Unreleased]` and add at top:

```markdown
### Added
- **Admin-controlled DB backend state machine** (`/admin/server-config` "Database backend" section + `agnes admin db` CLI). Replaces manual `.env` editing with explicit forward-only transitions DuckDB → side-car Postgres → managed cloud Postgres. Async migration jobs with progress polling, idempotent retry, audit-logged transitions, gzip backups before every cutover. Subprocess-based orchestration (no docker socket needed in the app container); host-side `agnes-state-applier.timer` reads `/data/state/db-state-target.flag` and applies compose lifecycle changes. Spec: `docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md`. Runbook: `docs/postgres-cutover-runbook.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG — DB backend state machine"
```

### Task 8.3: Push branch + open PR

**Files:** (no changes)

- [ ] **Step 1: Push**

```bash
git push origin $(git branch --show-current)
```

- [ ] **Step 2: Open PR**

```bash
gh pr create \
  --base main --head $(git branch --show-current) \
  --title "feat(db): admin-controlled DB backend state machine" \
  --body-file - <<'EOF'
Implements the spec at `docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md`.

Replaces ad-hoc `.env` editing with `/admin/server-config` UI + `agnes admin db` CLI for migrating Agnes app-state between DuckDB, side-car Postgres, and managed cloud Postgres.

**Changes:**
- 6 new files: state machine module, migration subprocess, API router, host applier script, JS, CLI module
- 5 modified files: db_pg URL resolver, repositories factory, startup-script, admin_server_config template, CLI registration
- 6 new test files: unit (state machine, migrator), integration (API, CLI), E2E (full migration)

**Verification:**
- Full PG suite + DuckDB regression sample green
- Full DuckDB → side-car E2E via pgserver passes
- terraform validate green on infra/modules/customer-instance/

**Deploy plan:**
1. Merge to main
2. Bump infra-keboola pin
3. terraform apply on agnes-dev → smoke-test per runbook
4. Roll out to fleet once dev is green for 24h
EOF
```

- [ ] **Step 3: Watch CI**

```bash
sleep 10
gh pr checks $(gh pr view --json number --jq .number) 2>&1 | head -15
```

Wait for green. Fix any CI failures in additional commits on the branch (do NOT force-push during review).

---

## Self-review

Looking at the spec with fresh eyes:

### 1. Spec coverage

| Spec section | Plan task |
|---|---|
| State machine (5 states, forward-only) | 1A.1 |
| Atomic state read/write | 1A.2 |
| File lock (concurrent prevention) | 1A.3 |
| `get_database_config()` + cache | 1A.4 |
| `_resolve_url` instance.yaml first | 1B.1 |
| `dispose_engine()` | 1B.2 |
| `use_pg()` reads instance.yaml | 1B.3 |
| Migrator: JobWriter | 2.1 |
| Migrator: alembic step | 2.2 |
| Migrator: data copy | 2.3 |
| Migrator: verify | 2.4 |
| Migrator: backup | 2.5 |
| Migrator: main() orchestration | 2.6 |
| GET /state | 3.1 |
| POST /migrate | 3.2 |
| GET /job/{id} | 3.3 |
| POST /cancel/{id} | 3.4 |
| Host applier script | 4.1 |
| Applier install (startup-script) | 4.2 |
| CLI state | 5.1 |
| CLI migrate | 5.2 |
| CLI job + cancel | 5.3 |
| UI JS | 6.1 |
| UI template | 6.2 |
| Full E2E | 7.1 |
| Regression check | 7.2 |
| Runbook update | 8.1 |
| CHANGELOG | 8.2 |
| Push + PR | 8.3 |

Every spec requirement maps to a task. ✓

### 2. Placeholder scan

Searched for `TBD`, `TODO`, "Similar to", "implement later" — none found. Each step has complete code blocks or exact commands.

One soft spot: Task 5.1 step 4 has a `_read_cred` stub with note "Inspect cli/commands/admin.py for real impl; reuse". This is acceptable because the file content depends on existing codebase conventions; the engineer's task is to wire to the existing credentials lookup, which they'll find by reading the file referenced. Better than inventing code that won't match.

### 3. Type consistency

- `BackendState` enum used identically across all tasks (`.value` accessor consistent).
- `JobWriter` class name consistent in test + module + main()
- `MigrationLock` class + `MigrationInProgressError` consistent
- `_OVERLAY_PATH` monkeypatched in same pattern across all tests
- API endpoint URLs consistent: `/api/admin/db/{state,migrate,job/{id},cancel/{id}}`
- CLI command names consistent: `state`, `migrate`, `job`, `cancel`

All consistent. ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-27-db-backend-state-machine.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review (spec compliance + code quality) after each, fast iteration

**2. Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints

**Which approach?**
