# Three-Plane Wave 1 — Process Roles, Startup Guards, Readiness (WS A + harness smoke)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `AGNES_ROLE` process roles with hard startup guards, `/healthz` + `/readyz` probes (hysteresis + background write-canary), lease-guarded seeds, and an m-tier compose smoke harness — the foundation every later workstream (jobs, coordination, gateway, DuckLake) builds on.

**Architecture:** One image, one entrypoint; a new `app/roles.py` resolves the active role set (env > instance.yaml > default `all`), `app/startup_guards.py` refuses unsafe topologies at boot (multi-process ⇒ Postgres + explicit secrets), lifecycle components in `app/main.py` become role-gated, and a new unauthenticated probe router serves liveness/readiness for load balancers. Spec: `docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md` §3.1, §3.2 (guards only), §3.7.

**Tech Stack:** Python 3.12/FastAPI/uvicorn, DuckDB + Postgres (repo factory), pytest (`.venv/bin/pytest`), Docker Compose.

## Global Constraints

- Default behavior MUST be unchanged: no env/config set ⇒ role set = `all`, no new requirements, existing E2E and unit suites pass unchanged (spec §5.4.1).
- Dual-backend discipline: anything touching state goes through the repo factory; PG sibling in the same task (CLAUDE.md).
- Every task ends with the full suite relevant to it green; full suite (`.venv/bin/pytest tests/ --tb=short -n auto -q`) before push.
- CHANGELOG bullet under `## [Unreleased]` ships in this branch (folded into Task 8).
- Vendor-agnostic wording in all docs/comments (no customer names, no cloud project ids).
- `/api/health` behavior MUST NOT change (existing healthchecks/watchdogs depend on it).

---

### Task 1: Role resolution module

**Files:**
- Create: `app/roles.py`
- Test: `tests/test_roles.py`

**Interfaces:**
- Produces: `Role` (StrEnum: `API="api"`, `GATEWAY="gateway"`, `WORKER="worker"`), `active_roles() -> frozenset[Role]`, `role_enabled(role: Role) -> bool`, `is_all_in_one() -> bool`, `reset_roles_cache() -> None` (for tests). Resolution order: `AGNES_ROLE` env > `instance.yaml::deployment.role` (via `app.instance_config.get_value("deployment", "role", default=None)`) > `"all"`. Value is comma-separable (`"api,gateway"`); `"all"` expands to the full set; unknown token ⇒ `ValueError` listing valid tokens.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roles.py
import pytest

from app.roles import Role, active_roles, is_all_in_one, reset_roles_cache, role_enabled


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_default_is_all_roles():
    assert active_roles() == frozenset({Role.API, Role.GATEWAY, Role.WORKER})
    assert is_all_in_one() is True
    assert role_enabled(Role.API) and role_enabled(Role.WORKER)


def test_env_single_role(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.API})
    assert is_all_in_one() is False
    assert role_enabled(Role.GATEWAY) is False


def test_env_comma_list_and_whitespace(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", " api, worker ")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.API, Role.WORKER})


def test_all_token(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "all")
    reset_roles_cache()
    assert is_all_in_one() is True


def test_unknown_token_raises(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "apii")
    reset_roles_cache()
    with pytest.raises(ValueError, match="apii"):
        active_roles()


def test_instance_yaml_fallback(monkeypatch):
    monkeypatch.setattr("app.roles._config_role", lambda: "worker")
    reset_roles_cache()
    assert active_roles() == frozenset({Role.WORKER})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_roles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.roles'`

- [ ] **Step 3: Implement `app/roles.py`**

```python
"""Process-role resolution for the three-plane deployment model.

One image, one entrypoint: ``AGNES_ROLE`` (env, comma-separable) or
``instance.yaml::deployment.role`` selects which planes this process
serves. Default ``all`` keeps today's single-process behavior.
Spec: docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md §3.1.
"""
import os
from enum import StrEnum
from functools import lru_cache


class Role(StrEnum):
    API = "api"
    GATEWAY = "gateway"
    WORKER = "worker"


_ALL = frozenset({Role.API, Role.GATEWAY, Role.WORKER})


def _config_role() -> str | None:
    from app.instance_config import get_value

    return get_value("deployment", "role", default=None)


@lru_cache(maxsize=1)
def active_roles() -> frozenset[Role]:
    raw = os.environ.get("AGNES_ROLE") or _config_role() or "all"
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if "all" in tokens:
        return _ALL
    roles: set[Role] = set()
    for tok in tokens:
        try:
            roles.add(Role(tok))
        except ValueError:
            valid = ", ".join([r.value for r in Role] + ["all"])
            raise ValueError(
                f"Invalid AGNES_ROLE token {tok!r} — valid tokens: {valid}"
            ) from None
    return frozenset(roles)


def role_enabled(role: Role) -> bool:
    return role in active_roles()


def is_all_in_one() -> bool:
    return active_roles() == _ALL


def reset_roles_cache() -> None:
    active_roles.cache_clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_roles.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/roles.py tests/test_roles.py
git commit -m "feat(roles): AGNES_ROLE process-role resolution"
```

---

### Task 2: Deployment startup guards

**Files:**
- Create: `app/startup_guards.py`
- Test: `tests/test_startup_guards.py`

**Interfaces:**
- Consumes: `app.roles.is_all_in_one`, `src.repositories.use_pg`.
- Produces: `DeploymentConfigError(RuntimeError)`; `validate_deployment() -> None` — raises `DeploymentConfigError` when the topology is multi-process (`not is_all_in_one()` or `UVICORN_WORKERS > 1`) and any of: app-state backend is not Postgres; `JWT_SECRET_KEY` env unset; `SESSION_SECRET` env unset; `coordination.backend` config is not `"redis"`. Error message names every missing item and links `docs/DEPLOYMENT.md`. `AGNES_VAULT_KEY` is NOT checked (spec §3.7: required only with vault-backed features).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_startup_guards.py
import pytest

from app.roles import reset_roles_cache
from app.startup_guards import DeploymentConfigError, validate_deployment


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("AGNES_ROLE", "UVICORN_WORKERS", "JWT_SECRET_KEY", "SESSION_SECRET"):
        monkeypatch.delenv(var, raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_all_in_one_passes_with_no_config():
    validate_deployment()  # must not raise — spec §5.4.1 default unchanged


def test_split_role_without_pg_refuses(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError, match="Postgres"):
        validate_deployment()


def test_multi_worker_is_multi_process(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError):
        validate_deployment()


def test_split_role_names_missing_secrets(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    assert "JWT_SECRET_KEY" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)
    assert "docs/DEPLOYMENT.md" in str(exc.value)


def test_split_role_requires_redis_coordination(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    with pytest.raises(DeploymentConfigError, match="coordination"):
        validate_deployment()


def test_split_role_fully_configured_passes(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    validate_deployment()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_startup_guards.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.startup_guards'`

- [ ] **Step 3: Implement `app/startup_guards.py`**

```python
"""Refuse unsafe multi-process topologies at boot.

Multi-process = role split (AGNES_ROLE != all) OR UVICORN_WORKERS > 1.
Such a deployment requires: Postgres app-state, explicit shared secrets,
and a Redis coordination backend. Single-process ``all`` mode has no new
requirements. Spec §3.2/§3.7.
"""
import os

from app.roles import is_all_in_one


class DeploymentConfigError(RuntimeError):
    pass


def _use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


def _coordination_backend() -> str:
    from app.instance_config import get_value

    return (get_value("coordination", "backend", default="memory") or "memory").lower()


def _workers() -> int:
    try:
        return int(os.environ.get("UVICORN_WORKERS", "1"))
    except ValueError:
        return 1


def is_multi_process() -> bool:
    return (not is_all_in_one()) or _workers() > 1


def validate_deployment() -> None:
    if not is_multi_process():
        return
    problems: list[str] = []
    if not _use_pg():
        problems.append(
            "app-state backend must be Postgres (set DATABASE_URL or "
            "instance.yaml::database.backend)"
        )
    for var in ("JWT_SECRET_KEY", "SESSION_SECRET"):
        if not os.environ.get(var):
            problems.append(f"{var} must be set explicitly (no per-node autogeneration)")
    if _coordination_backend() != "redis":
        problems.append(
            "coordination.backend must be 'redis' (instance.yaml::coordination.backend)"
        )
    if problems:
        raise DeploymentConfigError(
            "Multi-process deployment (AGNES_ROLE split or UVICORN_WORKERS>1) "
            "is not safely configured:\n  - "
            + "\n  - ".join(problems)
            + "\nSee docs/DEPLOYMENT.md#multi-process."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_startup_guards.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/startup_guards.py tests/test_startup_guards.py
git commit -m "feat(roles): startup guards for multi-process topologies"
```

---

### Task 3: Liveness/readiness probes with hysteresis + write-canary

**Files:**
- Create: `app/api/health_probes.py`
- Test: `tests/test_health_probes.py`

**Interfaces:**
- Produces: `router` (APIRouter, **no auth dependencies**) with `GET /healthz` (always `{"status": "alive"}`, 200) and `GET /readyz` (200 `{"status":"ready", ...}` / 503 `{"status":"not_ready", ...}`); `ReadinessState` class — `record_canary(ok: bool)` (M-of-N hysteresis: 3 consecutive failures ⇒ not ready, 2 consecutive successes ⇒ ready again; starts ready), `is_ready() -> bool`, `snapshot() -> dict`; module singleton `readiness = ReadinessState()`; `async def canary_loop(interval_s: float = 30.0)` — background loop calling `_write_canary()` (upsert of a timestamp row through the active backend via `system_config_repo` pattern: `sync_state_repo` is wrong here — use `users_repo`-independent key/value: implement `_write_canary()` as `metadata_repo().set("readiness_canary", <iso-now>)` if a metadata/kv repo exists, otherwise execute through `src.repositories.system_config_repo()` — **the implementer MUST grep `src/repositories/__init__.py` for an existing small KV repo (e.g. `system_config_repo` / `app_metadata_repo`) and reuse it; do not add a new table**), recording the result. Role hooks: `register_readiness_check(name: str, fn: Callable[[], bool])` — extra checks ANDed into `/readyz` (used by later workstreams for queue/redis checks).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_health_probes.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health_probes import ReadinessState, readiness, register_readiness_check, router


def make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_healthz_always_alive():
    assert make_client().get("/healthz").json() == {"status": "alive"}


def test_hysteresis_three_fails_two_recoveries():
    st = ReadinessState()
    assert st.is_ready()
    st.record_canary(False); st.record_canary(False)
    assert st.is_ready(), "two failures must not flip (hysteresis)"
    st.record_canary(False)
    assert not st.is_ready(), "third consecutive failure flips to not-ready"
    st.record_canary(True)
    assert not st.is_ready(), "one success must not recover"
    st.record_canary(True)
    assert st.is_ready(), "two consecutive successes recover"


def test_readyz_reflects_singleton(monkeypatch):
    client = make_client()
    for _ in range(3):
        readiness.record_canary(False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    for _ in range(2):
        readiness.record_canary(True)
    assert client.get("/readyz").status_code == 200


def test_extra_check_gates_readyz():
    client = make_client()
    flag = {"ok": True}
    register_readiness_check("t_extra", lambda: flag["ok"])
    try:
        assert client.get("/readyz").status_code == 200
        flag["ok"] = False
        r = client.get("/readyz")
        assert r.status_code == 503
        assert "t_extra" in str(r.json()["failed_checks"])
    finally:
        from app.api import health_probes
        health_probes._extra_checks.pop("t_extra", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_health_probes.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `app/api/health_probes.py`**

```python
"""LB probes: /healthz (liveness) and /readyz (readiness).

Readiness = background write-canary result with M-of-N hysteresis
(3 consecutive failures -> not ready, 2 consecutive successes -> ready)
plus any registered role-specific checks. The canary runs on a timer,
NOT per probe request — N replicas probing a slow DB must not amplify
load or flap together. /api/health is unchanged and stays the
compatibility alias. Spec §3.7.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["probes"])

_FAILS_TO_TRIP = 3
_OKS_TO_RECOVER = 2


class ReadinessState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = True
        self._consec_fail = 0
        self._consec_ok = 0
        self._last_canary_at: str | None = None

    def record_canary(self, ok: bool) -> None:
        with self._lock:
            self._last_canary_at = datetime.now(timezone.utc).isoformat()
            if ok:
                self._consec_ok += 1
                self._consec_fail = 0
                if not self._ready and self._consec_ok >= _OKS_TO_RECOVER:
                    self._ready = True
                    logger.info("readiness: recovered")
            else:
                self._consec_fail += 1
                self._consec_ok = 0
                if self._ready and self._consec_fail >= _FAILS_TO_TRIP:
                    self._ready = False
                    logger.error("readiness: tripped after %d canary failures", self._consec_fail)

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "canary_ready": self._ready,
                "consecutive_failures": self._consec_fail,
                "last_canary_at": self._last_canary_at,
            }


readiness = ReadinessState()
_extra_checks: dict[str, Callable[[], bool]] = {}


def register_readiness_check(name: str, fn: Callable[[], bool]) -> None:
    _extra_checks[name] = fn


def _write_canary() -> bool:
    try:
        # Reuse the existing small KV surface; see Task interface note —
        # implementer greps src/repositories/__init__.py for the KV repo.
        from src.repositories import system_config_repo

        system_config_repo().set("readiness_canary", datetime.now(timezone.utc).isoformat())
        return True
    except Exception:
        logger.exception("readiness write-canary failed")
        return False


async def canary_loop(interval_s: float = 30.0) -> None:
    while True:
        ok = await asyncio.to_thread(_write_canary)
        readiness.record_canary(ok)
        await asyncio.sleep(interval_s)


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "alive"}


@router.get("/readyz")
def readyz():
    failed = [name for name, fn in _extra_checks.items() if not _safe(fn)]
    ok = readiness.is_ready() and not failed
    body = {"status": "ready" if ok else "not_ready", "failed_checks": failed, **readiness.snapshot()}
    return JSONResponse(status_code=200 if ok else 503, content=body)


def _safe(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        logger.exception("readiness extra check crashed")
        return False
```

**Note for implementer:** if `system_config_repo` does not exist under that name, find the equivalent KV repo in `src/repositories/__init__.py` (grep for `config` / `metadata` factory functions) and adapt `_write_canary` — the write must go through the factory so it exercises the ACTIVE backend (that is the whole point: catching "reads OK / writes 500"). Adjust the import in the test-free `_write_canary` only; tests do not touch it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_health_probes.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/api/health_probes.py tests/test_health_probes.py
git commit -m "feat(probes): /healthz + /readyz with write-canary hysteresis"
```

---

### Task 4: Wire guards + probes into the app lifespan

**Files:**
- Modify: `app/main.py` (lifespan function start; router registration block — grep anchor `include_router` cluster; canary task next to the checkpoint-loop task creation — grep anchor `_state_checkpoint_loop`)
- Test: `tests/test_app_wiring_probes.py`

**Interfaces:**
- Consumes: `validate_deployment` (Task 2), `health_probes.router` + `canary_loop` (Task 3).
- Produces: app serves `/healthz`, `/readyz` unauthenticated; boot aborts with `DeploymentConfigError` on unsafe topology.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_wiring_probes.py
from fastapi.testclient import TestClient


def test_probes_served_unauthenticated(app_client):
    # app_client: use the repo's existing app TestClient fixture — grep
    # tests/conftest.py for the fixture that builds the FastAPI app
    # (pattern used by existing route tests). No Authorization header.
    assert app_client.get("/healthz").status_code == 200
    assert app_client.get("/readyz").status_code in (200, 503)
```

(If the shared fixture has a different name, match the neighboring route tests' convention — do not build a new app factory.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_app_wiring_probes.py -q`
Expected: FAIL — 404 on /healthz

- [ ] **Step 3: Wire in `app/main.py`**

Three small edits:

1. First statement inside the lifespan async context (before any DB work):

```python
    from app.startup_guards import validate_deployment

    validate_deployment()  # refuses unsafe multi-process topologies (spec §3.2)
```

2. Next to the `_state_checkpoint_loop` task creation, start the canary:

```python
    from app.api.health_probes import canary_loop

    _canary_task = asyncio.create_task(canary_loop())
```

and cancel `_canary_task` in the shutdown path next to where the checkpoint task is cancelled (same pattern: `cancel()` + `contextlib.suppress(asyncio.CancelledError)` await).

3. In the router-registration block:

```python
    from app.api import health_probes

    app.include_router(health_probes.router)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_app_wiring_probes.py tests/test_health_probes.py -q` then the app-route neighborhood: `.venv/bin/pytest tests/ -k "health" -q`
Expected: all pass; existing `/api/health` tests untouched and green.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_app_wiring_probes.py
git commit -m "feat(probes): wire startup guard, probe router and canary loop into lifespan"
```

---

### Task 5: Role-gate lifecycle components

**Files:**
- Modify: `app/main.py` — four sites, all located by grep anchor, not line number:
  1. chat manager init — anchor: `UVICORN_WORKERS > 1` chat gate (`elif int(os.environ.get("UVICORN_WORKERS", "1")) > 1:`)
  2. Slack socket-mode dispatcher setup — anchor: `socket_mode_preflight(`
  3. startup warmup — anchor: `maybe_schedule_startup_warmup`
  4. rebuild-on-boot — anchor: `AGNES_REBUILD_ON_BOOT`
- Test: `tests/test_role_gating.py`

**Interfaces:**
- Consumes: `role_enabled(Role.GATEWAY)`, `role_enabled(Role.WORKER)` (Task 1).
- Produces: chat manager + Slack socket run only when `role_enabled(Role.GATEWAY)`; warmup + rebuild-on-boot only when `role_enabled(Role.WORKER)`. In default `all` mode every gate passes ⇒ behavior identical to today. The `UVICORN_WORKERS > 1` chat gate stays (belt and braces until WS C/D remove the in-memory state).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_role_gating.py
"""Role gates: api-only process must not own chat/warmup; all-mode unchanged."""
import pytest

from app.roles import Role, reset_roles_cache, role_enabled


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_gateway_gate_helper(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    assert role_enabled(Role.GATEWAY) is False


def test_main_uses_role_gates():
    # Structural guard: the four lifecycle sites in app/main.py must consult
    # role_enabled — cheap regression net until the E2E harness (Task 7)
    # exercises real processes.
    src = open("app/main.py").read()
    assert src.count("role_enabled(Role.GATEWAY)") >= 2, "chat + slack socket must be gateway-gated"
    assert src.count("role_enabled(Role.WORKER)") >= 2, "warmup + rebuild-on-boot must be worker-gated"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_role_gating.py -q`
Expected: `test_main_uses_role_gates` FAILS (count 0)

- [ ] **Step 3: Apply the four gates in `app/main.py`**

Add once near the top of the lifespan: `from app.roles import Role, role_enabled`.

1. Chat manager block — extend the existing decision chain with a role condition **before** the provider/worker checks:

```python
            if not role_enabled(Role.GATEWAY):
                logger.info("chat: disabled in this process (role split; gateway role owns chat)")
                app.state.chat_manager = None
            elif app.state.chat_config.provider != "e2b":
                ...  # existing chain unchanged
```

2. Slack socket-mode setup function — first lines:

```python
    if not role_enabled(Role.GATEWAY):
        logger.info("slack socket mode: skipped (not a gateway-role process)")
        return
```

3. Warmup call site:

```python
    if role_enabled(Role.WORKER):
        maybe_schedule_startup_warmup(...)
```

(keep the existing call expression/arguments exactly; only wrap it)

4. Rebuild-on-boot block: wrap the existing `AGNES_REBUILD_ON_BOOT` branch body in `if role_enabled(Role.WORKER):` the same way.

- [ ] **Step 4: Run tests — role tests plus the full unit suite for regressions**

Run: `.venv/bin/pytest tests/test_role_gating.py -q && .venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: role tests pass; full suite green (default mode = all gates open).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_role_gating.py
git commit -m "feat(roles): role-gate chat, slack socket, warmup and rebuild-on-boot"
```

---

### Task 6: Lease-guarded seeds (concurrent cold boot)

**Files:**
- Modify: `src/db_pg.py` (add lease helper), `app/main.py` (wrap the seed block — grep anchors `seed_default_connections` through `seed_builtin_marketplace`)
- Test: `tests/db_pg/test_seed_lease_contract.py`

**Interfaces:**
- Consumes: `src.db_pg` engine (`get_pg_engine()` or the module's existing engine accessor — grep `create_engine` in `src/db_pg.py` and reuse its accessor).
- Produces: `seed_lease()` context manager in `src/db_pg.py` — takes PG advisory lock id `0x41474E53` (`"AGNS"`), blocking, released on exit; on DuckDB backend it is a no-op context (single process is guaranteed by Task 2's guard). `app/main.py` seed block runs inside `with seed_lease():`.

- [ ] **Step 1: Write the failing test**

```python
# tests/db_pg/test_seed_lease_contract.py
"""Two processes running the seed block concurrently must serialize.

Uses the repo's existing PG test fixture — grep tests/db_pg/ for the
fixture that yields a live engine/DSN (same one the other contract tests
use) and follow that pattern.
"""
import threading
import time

from src.db_pg import seed_lease


def test_seed_lease_serializes(pg_engine):  # match the local fixture name
    order: list[str] = []

    def hold():
        with seed_lease():
            order.append("first-in")
            time.sleep(0.5)
            order.append("first-out")

    def contend():
        time.sleep(0.1)
        with seed_lease():
            order.append("second-in")

    t1, t2 = threading.Thread(target=hold), threading.Thread(target=contend)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert order == ["first-in", "first-out", "second-in"]


def test_seed_lease_noop_on_duckdb(monkeypatch):
    monkeypatch.setattr("src.db_pg._lease_use_pg", lambda: False)
    with seed_lease():
        pass  # must not require a PG connection
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/db_pg/test_seed_lease_contract.py -q`
Expected: FAIL — `ImportError: cannot import name 'seed_lease'`

- [ ] **Step 3: Implement `seed_lease` in `src/db_pg.py`**

```python
from contextlib import contextmanager

_SEED_LEASE_ID = 0x41474E53  # "AGNS" — cross-replica seed serialization


def _lease_use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


@contextmanager
def seed_lease():
    """Serialize startup seeds across replicas via a PG advisory lock.

    No-op on the DuckDB backend — the startup guard already restricts
    DuckDB app-state to a single process. Blocking by design: replicas
    wait for the winner, then re-run the (idempotent) seeds.
    """
    if not _lease_use_pg():
        yield
        return
    engine = get_pg_engine()  # reuse the module's existing engine accessor
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT pg_advisory_lock(%s)", (_SEED_LEASE_ID,))
        try:
            yield
        finally:
            conn.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (_SEED_LEASE_ID,))
```

Then in `app/main.py`, wrap the contiguous seed block (from `seed_default_connections` through the seed-admin section) in:

```python
    from src.db_pg import seed_lease

    with seed_lease():
        ...  # existing seed statements, unchanged, re-indented
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/db_pg/test_seed_lease_contract.py -q && .venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: contract test green (needs the PG test infra; runs where other tests/db_pg tests run); full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/db_pg.py app/main.py tests/db_pg/test_seed_lease_contract.py
git commit -m "feat(roles): lease-guarded startup seeds for concurrent cold boot"
```

---

### Task 7: m-tier compose profile + smoke script (harness skeleton, WS H)

**Files:**
- Create: `docker-compose.mtier.yml`, `deploy/caddy/Caddyfile.mtier`, `scripts/dev/mtier-smoke.sh`
- Test: the smoke script itself (env-gated; not part of unit CI)

**Interfaces:**
- Consumes: image built from this branch; Task 1–6 role behavior.
- Produces: `docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.mtier.yml --profile mtier up` boots `api1, api2, gateway, worker, caddy-mtier` (+ postgres); smoke script asserts probe behavior and single-replica-kill continuity.

- [ ] **Step 1: Write `docker-compose.mtier.yml`**

```yaml
# M-tier role-split overlay (spec §3.8). Compose profile: mtier.
# Layer over docker-compose.yml + docker-compose.postgres.yml. Redis is a
# placeholder for WS C — guards demand its config, coordination lands next wave.
services:
  api1: &api_role
    extends: { file: docker-compose.yml, service: app }
    ports: !reset []            # proxy-only exposure — no host ports
    environment:
      AGNES_ROLE: api
    profiles: [mtier]
  api2:
    <<: *api_role
    profiles: [mtier]
  gateway:
    extends: { file: docker-compose.yml, service: app }
    ports: !reset []
    environment:
      AGNES_ROLE: gateway
    profiles: [mtier]
  worker:
    extends: { file: docker-compose.yml, service: app }
    ports: !reset []
    environment:
      AGNES_ROLE: worker
    profiles: [mtier]
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    profiles: [mtier]
  caddy-mtier:
    image: caddy:2-alpine
    ports: ["8080:8080"]
    volumes:
      - ./deploy/caddy/Caddyfile.mtier:/etc/caddy/Caddyfile:ro
    depends_on: [api1, api2]
    profiles: [mtier]
```

**Implementer note:** `extends` + `!reset` needs Compose v2.24+; if the pinned compose version chokes on `!reset`, copy the `app` service stanza inline instead of `extends` (same env/volumes, no `ports:`). Verify with `docker compose config`.

- [ ] **Step 2: Write `deploy/caddy/Caddyfile.mtier`**

```
:8080 {
	reverse_proxy api1:8000 api2:8000 {
		health_uri /readyz
		health_interval 5s
		fail_duration 10s
		lb_policy round_robin
	}
}
```

- [ ] **Step 3: Write `scripts/dev/mtier-smoke.sh`**

```bash
#!/usr/bin/env bash
# M-tier smoke: boots the role-split profile, asserts probes + kill-one-api
# continuity. Local/dev harness — needs docker; not run in unit CI.
set -euo pipefail
cd "$(dirname "$0")/../.."

export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$(openssl rand -hex 32)}"
export SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.mtier.yml --profile mtier)

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${COMPOSE[@]}" up -d --build
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS localhost:8080/healthz | grep -q alive || { echo "FAIL healthz"; exit 1; }
curl -fsS localhost:8080/readyz | grep -q ready || { echo "FAIL readyz"; exit 1; }

echo "killing api1 under traffic..."
"${COMPOSE[@]}" kill api1
fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || fails=$((fails+1))
  sleep 0.5
done
[ "$fails" -le 2 ] || { echo "FAIL: $fails/20 requests failed after killing api1"; exit 1; }
echo "MTIER SMOKE OK (failures after kill: $fails/20)"
```

- [ ] **Step 4: Run it**

Run: `chmod +x scripts/dev/mtier-smoke.sh && ./scripts/dev/mtier-smoke.sh`
Expected: `MTIER SMOKE OK`. (Requires the WS A guard to accept the topology: the script exports both secrets, compose uses the postgres overlay, and `coordination.backend` — until WS C — must be set `redis` in the test instance config the compose mounts; if the guard refuses, mount a minimal `config/instance.mtier.yaml` with `coordination: {backend: redis}` into the role services and set `AGNES_CONFIG` accordingly — follow how docker-compose.yml mounts `./config`.)

- [ ] **Step 5: Commit**

```bash
git add docker-compose.mtier.yml deploy/caddy/Caddyfile.mtier scripts/dev/mtier-smoke.sh
git commit -m "feat(harness): m-tier role-split compose profile + smoke script"
```

---

### Task 8: Docs + CHANGELOG

**Files:**
- Modify: `docs/DEPLOYMENT.md` (new section `## Multi-process (role split)` — anchor for the guard's error link `docs/DEPLOYMENT.md#multi-process`), `CHANGELOG.md` (`## [Unreleased]`)

- [ ] **Step 1: Write the DEPLOYMENT section**

Content (adapt heading level to the file's existing structure):

```markdown
## Multi-process (role split) {#multi-process}

`AGNES_ROLE` (env or `instance.yaml::deployment.role`) selects which planes a
process serves: `api`, `gateway`, `worker`, or `all` (default — today's
single-process behavior, no new requirements).

Any multi-process topology (role split, or `UVICORN_WORKERS > 1`) must set:

- `DATABASE_URL` (or `database.backend`) — Postgres app-state,
- `JWT_SECRET_KEY` and `SESSION_SECRET` — explicit shared secrets,
- `coordination.backend: redis` — shared coordination (see the m-tier profile).

The app refuses to start otherwise, naming what is missing. Probes:
`/healthz` (liveness), `/readyz` (readiness — background write-canary with
hysteresis; point LB health checks here). `/api/health` is unchanged.
Try it: `./scripts/dev/mtier-smoke.sh`.
```

- [ ] **Step 2: CHANGELOG bullet under `## [Unreleased]` → Added**

```markdown
- Process roles (`AGNES_ROLE=api|gateway|worker|all`) with startup guards for
  multi-process topologies, `/healthz` + `/readyz` LB probes (write-canary with
  hysteresis), lease-guarded startup seeds, and an experimental m-tier
  role-split compose profile (`docker-compose.mtier.yml`).
```

- [ ] **Step 3: Full suite + commit**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green.

```bash
git add docs/DEPLOYMENT.md CHANGELOG.md
git commit -m "docs: multi-process role split deployment section + changelog"
```

---

## Self-review notes

- Spec coverage (wave-1 slice of §3.1/§3.2/§3.7/§3.8): role resolution (T1), guards incl. UVICORN_WORKERS reframe (T2), probes + canary + hysteresis + alias preservation (T3/T4), role-gated lifecycle (T5), seed lease (T6), m-tier profile + proxy multi-upstream + smoke (T7), docs anchor referenced by the guard error (T8). Deliberately deferred to later waves: Redis coordination implementation (WS C — guard demands its config already), job queue + worker runtime (WS B), per-role mem-limit knobs and Prometheus (WS G), auto-upgrade/watchdog rewrites (WS I plan).
- Known judgment calls for the implementer: exact KV repo name for the canary write (T3 note), compose `extends`/`!reset` support (T7 note), shared test fixture names (T4/T6 notes) — each carries an explicit discovery instruction instead of a guess.
