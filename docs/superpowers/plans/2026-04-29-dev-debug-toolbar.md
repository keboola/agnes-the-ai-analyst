# Dev Debug Toolbar + Centralized Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `fastapi-debug-toolbar` (gated by `DEBUG=1`) with a custom DuckDBPanel, and replace 23 scattered `logging.basicConfig` calls with a single centralized `setup_logging()` helper that uses `RichHandler` in dev and JSON in prod.

**Architecture:** Phase 1–4 introduce `app/logging_config.py` and migrate every entrypoint (services, scripts, app, connectors) to call `setup_logging(__name__)`. Phase 5 mounts `DebugToolbarMiddleware` behind a `DEBUG=1` env gate so production never imports the toolbar. Phase 6 adds a custom `DuckDBPanel` with an `InstrumentedConnection` wrapper that hooks `src/db.py`'s three `duckdb.connect()` call sites. Phase 7 documents the `DEBUG=1` flag in `.env.template`, `CHANGELOG.md`, and `docs/development.md`.

**Tech Stack:** Python 3.11+, FastAPI, Starlette, DuckDB, `rich>=13` (already a dep), `fastapi-debug-toolbar>=0.6.3` (new dev-only dep, pulls Jinja2 + pyinstrument), pytest with coverage.

**Working directory for execution:** `/Users/vrysanek/foundry-ai/agnes-the-ai-analyst/.worktrees/main-dev-logging/` on branch `vr/dev-logging` (tracks `origin/main`). All paths below are relative to this worktree root.

---

## File Structure

### New files

| Path | Responsibility |
|------|----------------|
| `app/logging_config.py` | `setup_logging()`, `_JSONFormatter`, `_derive_slug()`, `request_id_var` ContextVar |
| `app/middleware/__init__.py` | empty package marker |
| `app/middleware/request_id.py` | `RequestIdMiddleware` (Starlette `BaseHTTPMiddleware`) |
| `app/debug/__init__.py` | empty package marker |
| `app/debug/duckdb_panel.py` | `DuckDBPanel`, `InstrumentedConnection`, `record_query`, `get_request_store` |
| `app/debug/templates/panels/duckdb.html` | Jinja template rendering query table |
| `tests/test_logging_config.py` | unit tests for `setup_logging` and slug derivation |
| `tests/test_request_id_middleware.py` | unit tests for request-id middleware |
| `tests/test_duckdb_panel.py` | unit tests for `InstrumentedConnection` |
| `tests/test_toolbar_integration.py` | integration tests for toolbar + DuckDBPanel |
| `docs/development.md` | brief usage doc covering `DEBUG=1` and toolbar |

### Modified files

| Path | Change |
|------|--------|
| `app/main.py` | call `setup_logging("app")`, mount `RequestIdMiddleware` always, mount `DebugToolbarMiddleware` only if `DEBUG=1` |
| `app/api/sync.py:105` | delete rogue module-level `logging.basicConfig` |
| `src/db.py` | add `_maybe_instrument()` helper; wrap returns of `get_system_db()`, `get_analytics_db()`, `get_analytics_db_readonly()` |
| `src/catalog_export.py` | replace module-level `basicConfig` with `setup_logging(__name__)` (only when run as script) |
| `src/profiler.py` | replace module-level `basicConfig` with `setup_logging(__name__)` |
| `services/scheduler/__main__.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `services/ws_gateway/gateway.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `services/telegram_bot/bot.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `services/corporate_memory/collector.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `services/session_collector/collector.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `services/verification_detector/__main__.py` | replace `basicConfig` with `setup_logging(__name__)` |
| `connectors/keboola/extractor.py` | remove module-level `_logging.basicConfig` (only run via CLI block at bottom) |
| `connectors/jira/transform.py` | remove module-level `basicConfig` |
| `connectors/jira/incremental_transform.py` | remove module-level `basicConfig` |
| `connectors/jira/extract_init.py` | replace `basicConfig` with `setup_logging(__name__)` (only inside `__main__` block) |
| `connectors/jira/scripts/poll_sla.py` | replace with `setup_logging(__name__)` |
| `connectors/jira/scripts/backfill.py` | replace with `setup_logging(__name__)` |
| `connectors/jira/scripts/backfill_sla.py` | replace with `setup_logging(__name__)` |
| `connectors/jira/scripts/backfill_remote_links.py` | replace with `setup_logging(__name__)` |
| `connectors/jira/scripts/consistency_check.py` | replace with `setup_logging(__name__)` |
| `scripts/migrate_json_to_duckdb.py` | replace with `setup_logging(__name__)` |
| `scripts/migrate_metrics_to_duckdb.py` | replace with `setup_logging(__name__)` |
| `scripts/migrate_parquets_to_extracts.py` | replace with `setup_logging(__name__)` |
| `scripts/migrate_registry_to_duckdb.py` | replace with `setup_logging(__name__)` |
| `scripts/generate_sample_data.py` | replace with `setup_logging(__name__)` |
| `pyproject.toml` | add `fastapi-debug-toolbar>=0.6.3` to `[project.optional-dependencies].dev` |
| `config/.env.template` | document `DEBUG=1` toggle |
| `CHANGELOG.md` | new `### Added` entry under `## [Unreleased]` |

### Discovered facts (verified during plan writing)

- `src/db.py` has three `duckdb.connect()` call sites: lines 413, 423, 581/587. Three public helpers: `get_system_db()` (line 394, returns `_system_db_conn.cursor()`), `get_analytics_db()` (line 419), `get_analytics_db_readonly()` (line 573).
- `services/scheduler/__main__.py:31`, `services/verification_detector/__main__.py:49`, `services/corporate_memory/collector.py:52`, `services/session_collector/collector.py:36`, `services/telegram_bot/bot.py:55`, `services/ws_gateway/gateway.py:214` — all have multi-line `basicConfig` calls.
- `app/api/sync.py:105` has a **module-level** `basicConfig` inside an api file — bug, will be removed (root logger is configured by the app entrypoint).
- 23 total `basicConfig` call sites enumerated.

---

## Task 1: Centralized logging module (`app/logging_config.py`) — Phase P1

**Files:**
- Create: `app/logging_config.py`
- Test: `tests/test_logging_config.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_logging_config.py`:

```python
import json
import logging
import os
from io import StringIO

import pytest

from app.logging_config import (
    _CONFIGURED,
    _derive_slug,
    _JSONFormatter,
    request_id_var,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch):
    """Reset global logging state between tests."""
    import app.logging_config as lc
    lc._CONFIGURED = False
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    yield
    lc._CONFIGURED = False
    logging.getLogger().handlers.clear()


def test_dev_uses_rich_handler(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    from rich.logging import RichHandler
    assert isinstance(handlers[0], RichHandler)


def test_prod_uses_json_formatter():
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert isinstance(handlers[0].formatter, _JSONFormatter)


def test_idempotent():
    setup_logging("app")
    setup_logging("app")
    setup_logging("app")
    assert len(logging.getLogger().handlers) == 1


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_log_level_default_prod():
    setup_logging("app")
    assert logging.getLogger().level == logging.INFO


def test_log_level_default_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_slug_explicit_short_name():
    assert _derive_slug("scheduler") == "scheduler"


def test_slug_strips_services_prefix():
    assert _derive_slug("services.scheduler.__main__") == "scheduler"


def test_slug_keeps_nested_module():
    assert _derive_slug("services.corporate_memory.collector") == "corporate_memory.collector"


def test_slug_strips_app_prefix():
    assert _derive_slug("app.main") == "app"


def test_slug_strips_connectors_prefix():
    assert _derive_slug("connectors.jira.transform") == "jira.transform"


def test_slug_explicit_app():
    assert _derive_slug("app") == "app"


def test_json_formatter_includes_service_field():
    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello world", args=(), exc_info=None,
    )
    fmt = _JSONFormatter(service="myservice")
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert parsed["service"] == "myservice"
    assert parsed["msg"] == "hello world"
    assert parsed["lvl"] == "INFO"
    assert parsed["logger"] == "test"
    assert "ts" in parsed


def test_json_formatter_includes_request_id_when_set():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    token = request_id_var.set("abc123")
    try:
        line = fmt.format(rec)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)
    assert parsed["request_id"] == "abc123"


def test_json_formatter_omits_request_id_when_unset():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "request_id" not in parsed


def test_json_formatter_includes_exception():
    fmt = _JSONFormatter(service="app")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="oops", args=(), exc_info=sys.exc_info(),
        )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "exc" in parsed
    assert "ValueError: boom" in parsed["exc"]


def test_setup_logging_emits_parsable_json_in_prod(capsys):
    setup_logging("app")
    logging.getLogger("test").info("hello %s", "world")
    out = capsys.readouterr().err
    parsed = json.loads(out.strip().splitlines()[-1])
    assert parsed["msg"] == "hello world"
    assert parsed["service"] == "app"


def test_setup_logging_silences_uvicorn_access_in_prod():
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_setup_logging_keeps_uvicorn_access_in_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.INFO
```

- [ ] **Step 1.2: Run tests, verify they fail**

```bash
uv run pytest tests/test_logging_config.py -q
```

Expected: collection error or `ImportError: cannot import name 'setup_logging'` because the module does not exist yet.

- [ ] **Step 1.3: Implement `app/logging_config.py`**

Create the file:

```python
"""Centralized logging configuration for FastAPI app and background services.

Each entrypoint (app/main.py, services/*/__main__.py or top-level script)
calls setup_logging(__name__) once. Library modules just do
`logger = logging.getLogger(__name__)` — they NEVER call setup_logging.

Dev (DEBUG=1): rich.logging.RichHandler with color, tracebacks, links.
Prod: stdlib StreamHandler with JSON formatter to stderr.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_CONFIGURED = False


def setup_logging(service: str | None = None, level: str | None = None) -> None:
    """Configure root logger. Idempotent.

    Pass ``__name__`` (preferred) or an explicit short slug like ``"app"``.
    Multiple calls are no-ops.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    lvl = (
        level
        or os.environ.get("LOG_LEVEL")
        or ("DEBUG" if debug else "INFO")
    ).upper()
    slug = _derive_slug(service)

    if debug:
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_time=True,
            show_path=True,
            markup=False,
            force_terminal=True,
        )
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter(service=slug))

    logging.basicConfig(level=lvl, handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if debug else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def _derive_slug(service: str | None) -> str:
    """Turn module name (``__name__``) or override into readable service slug.

    Examples:
        _derive_slug("app")                                  -> "app"
        _derive_slug("services.scheduler.__main__")          -> "scheduler"
        _derive_slug("services.corporate_memory.collector")  -> "corporate_memory.collector"
        _derive_slug("connectors.jira.transform")            -> "jira.transform"
    """
    if service and not service.startswith("_") and service != "__main__":
        s = (
            service.removeprefix("services.")
            .removeprefix("connectors.")
            .removeprefix("app.")
        )
        s = s.removesuffix(".__main__").removesuffix(".main")
        return s or "app"

    try:
        frame = sys._getframe(2)
        path = frame.f_globals.get("__file__")
        if path:
            p = Path(path)
            for top in ("services", "connectors", "app"):
                if top in p.parts:
                    i = p.parts.index(top) + 1
                    rest = p.parts[i:]
                    name = ".".join([*rest[:-1], p.stem])
                    return name.removesuffix(".__main__").removesuffix(".main") or top
            return p.stem
    except Exception:
        pass
    return "app"


class _JSONFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "lvl": record.levelname,
            "logger": record.name,
            "service": self.service,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
```

- [ ] **Step 1.4: Run tests, verify they pass**

```bash
uv run pytest tests/test_logging_config.py -q
```

Expected: all tests pass. If any fail, fix before proceeding.

- [ ] **Step 1.5: Run quality checks**

```bash
ruff format app/logging_config.py tests/test_logging_config.py
ruff check app/logging_config.py tests/test_logging_config.py --fix
```

Expected: no warnings.

- [ ] **Step 1.6: Commit**

```bash
git add app/logging_config.py tests/test_logging_config.py
git commit -m "feat(logging): add centralized setup_logging with dev/prod handlers"
```

---

## Task 2: Request-ID middleware (`app/middleware/request_id.py`) — Phase P2

**Files:**
- Create: `app/middleware/__init__.py` (empty)
- Create: `app/middleware/request_id.py`
- Test: `tests/test_request_id_middleware.py`

- [ ] **Step 2.1: Create empty package marker**

```bash
mkdir -p app/middleware
: > app/middleware/__init__.py
```

- [ ] **Step 2.2: Write the failing tests**

Create `tests/test_request_id_middleware.py`:

```python
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.logging_config import request_id_var
from app.middleware.request_id import RequestIdMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo-rid")
    def echo_rid():
        return {"rid": request_id_var.get()}

    return app


def test_assigns_id_when_missing():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid")
    assert resp.status_code == 200
    rid = resp.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{12}", rid)
    assert resp.json()["rid"] == rid


def test_passes_through_provided_id():
    client = TestClient(_make_app())
    resp = client.get("/echo-rid", headers={"X-Request-ID": "given-1234"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "given-1234"
    assert resp.json()["rid"] == "given-1234"


def test_request_id_resets_after_request():
    client = TestClient(_make_app())
    client.get("/echo-rid")
    assert request_id_var.get() is None
```

- [ ] **Step 2.3: Run tests, verify they fail**

```bash
uv run pytest tests/test_request_id_middleware.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.middleware.request_id'`.

- [ ] **Step 2.4: Implement the middleware**

Create `app/middleware/request_id.py`:

```python
"""Request-ID middleware. Assigns or propagates X-Request-ID per request."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_config import request_id_var


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response
```

- [ ] **Step 2.5: Run tests, verify they pass**

```bash
uv run pytest tests/test_request_id_middleware.py -q
```

Expected: all 3 tests pass.

- [ ] **Step 2.6: Run quality checks**

```bash
ruff format app/middleware/ tests/test_request_id_middleware.py
ruff check app/middleware/ tests/test_request_id_middleware.py --fix
```

- [ ] **Step 2.7: Commit**

```bash
git add app/middleware/ tests/test_request_id_middleware.py
git commit -m "feat(http): add RequestIdMiddleware for cross-log correlation"
```

---

## Task 3: Wire `setup_logging` + `RequestIdMiddleware` into `app/main.py` — Phase P2 (continued)

**Files:**
- Modify: `app/main.py` (add `setup_logging` call at top of module before any other import that emits logs; add `RequestIdMiddleware`)
- Modify: `app/api/sync.py:105` (delete rogue `basicConfig`)

- [ ] **Step 3.1: Read `app/main.py` to find safe insertion points**

```bash
sed -n '1,40p' app/main.py
```

Expected: shows imports + module-level setup. Note the line where `app = FastAPI(...)` is created and where `app.include_router(...)` calls begin.

- [ ] **Step 3.2: Add `setup_logging("app")` near the top of `app/main.py`**

Locate the **first non-`from __future__` import** in `app/main.py`. Above the FastAPI import, add:

```python
from app.logging_config import setup_logging

setup_logging("app")
```

Keep it before any module that emits logs at import time.

- [ ] **Step 3.3: Mount `RequestIdMiddleware` immediately after `app = FastAPI(...)` is created**

Find the `app = FastAPI(...)` line. Immediately after it (before any `app.add_middleware(...)` or `app.include_router(...)`), add:

```python
from app.middleware.request_id import RequestIdMiddleware

app.add_middleware(RequestIdMiddleware)
```

- [ ] **Step 3.4: Delete rogue module-level `basicConfig` in `app/api/sync.py`**

Read `app/api/sync.py` lines 100–110 to find the exact line and any logger usage:

```bash
sed -n '100,115p' app/api/sync.py
```

Then delete the line:

```python
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
```

If there is no other `import logging` usage in the file beyond this and a `logger = logging.getLogger(__name__)`, leave the import alone (the logger reference still needs it).

- [ ] **Step 3.5: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all tests still pass. If any test was relying on the rogue `basicConfig` formatter from `sync.py`, it will surface here — fix by switching that test's expectations to use `caplog`.

- [ ] **Step 3.6: Sanity-check the app starts**

```bash
uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 3
curl -s -i http://localhost:8011/health 2>/dev/null | head -10
kill $APP_PID
```

Expected: response includes `x-request-id: <12 hex chars>` header.

- [ ] **Step 3.7: Commit**

```bash
git add app/main.py app/api/sync.py
git commit -m "feat(app): wire setup_logging + RequestIdMiddleware in app/main.py"
```

---

## Task 4: Migrate service entrypoints — Phase P3

Six service entrypoints currently call `logging.basicConfig(...)` at module top level. Replace each with `setup_logging(__name__)`.

**Files:**
- Modify: `services/scheduler/__main__.py`
- Modify: `services/ws_gateway/gateway.py`
- Modify: `services/telegram_bot/bot.py`
- Modify: `services/corporate_memory/collector.py`
- Modify: `services/session_collector/collector.py`
- Modify: `services/verification_detector/__main__.py`

- [ ] **Step 4.1: Migrate `services/scheduler/__main__.py`**

Line 31 onward currently is:

```python
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [scheduler] %(message)s",
)
logger = logging.getLogger(__name__)
```

Replace the `logging.basicConfig(...)` block (multi-line) with:

```python
from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)
```

Keep the existing `import logging` and the `logger = ...` line. Remove `import os` only if it is unused after the change (verify by running ruff after).

- [ ] **Step 4.2: Migrate `services/ws_gateway/gateway.py`**

Find the `logging.basicConfig(...)` block at line 214 (inside an `if __name__ == "__main__":` or similar startup function). Replace:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
```

with:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

- [ ] **Step 4.3: Migrate `services/telegram_bot/bot.py`**

Find the multi-line `logging.basicConfig(...)` block at line 55. Replace the entire block with:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
logger = logging.getLogger("notify-bot")  # keep existing logger name to preserve log filters
```

Preserve any pre-existing custom logger name.

- [ ] **Step 4.4: Migrate `services/corporate_memory/collector.py`**

Find the multi-line `logging.basicConfig(...)` block at line 52. Replace with:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

Keep any subsequent `logger = logging.getLogger(...)` line.

- [ ] **Step 4.5: Migrate `services/session_collector/collector.py`**

Find the multi-line `logging.basicConfig(...)` block at line 36. Replace with:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

- [ ] **Step 4.6: Migrate `services/verification_detector/__main__.py`**

Find the multi-line `logging.basicConfig(...)` block at line 49 (inside the `__main__` function, NOT module-level). Replace inline:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

- [ ] **Step 4.7: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all tests pass. Service modules' tests (if any) may need `monkeypatch.setattr("app.logging_config._CONFIGURED", False)` to reset state.

- [ ] **Step 4.8: Smoke-test each service starts**

```bash
# Scheduler should start, log a single line, then idle
timeout 3 uv run python -m services.scheduler 2>&1 | head -5 || true
# Ws gateway should start
timeout 3 uv run python -m services.ws_gateway 2>&1 | head -5 || true
```

Expected: each service emits a startup log line in JSON (no `DEBUG=1`) or rich color (`DEBUG=1 timeout 3 uv run …`).

- [ ] **Step 4.9: Run quality checks**

```bash
ruff format services/
ruff check services/ --fix
```

- [ ] **Step 4.10: Commit**

```bash
git add services/
git commit -m "refactor(services): migrate entrypoints to centralized setup_logging"
```

---

## Task 5: Migrate `connectors/`, `src/`, `scripts/` — Phase P4

These 14 files have module-level or `__main__`-block `basicConfig` calls. The migration rules differ by location:

- **Module-level `basicConfig` in a library file** (e.g. `connectors/jira/transform.py`): just **remove** it — library modules should never configure root logger; the entrypoint that imports them is responsible.
- **`basicConfig` inside `if __name__ == "__main__":` block**: **replace** with `setup_logging(__name__)`.

**Files:**
- Modify: `src/catalog_export.py`, `src/profiler.py`
- Modify: `connectors/keboola/extractor.py`
- Modify: `connectors/jira/transform.py`, `incremental_transform.py`, `extract_init.py`
- Modify: `connectors/jira/scripts/poll_sla.py`, `backfill.py`, `backfill_sla.py`, `backfill_remote_links.py`, `consistency_check.py`
- Modify: `scripts/migrate_json_to_duckdb.py`, `migrate_metrics_to_duckdb.py`, `migrate_parquets_to_extracts.py`, `migrate_registry_to_duckdb.py`, `generate_sample_data.py`

- [ ] **Step 5.1: Determine library vs entrypoint per file**

For each file, run:

```bash
grep -nE 'if __name__ == "__main__"|basicConfig' <file>
```

If `basicConfig` is **outside** any `if __name__` block AND the file is imported by other modules → it is a library file. Remove the `basicConfig` line entirely.

If `basicConfig` is **inside** `if __name__ == "__main__"` OR the file is exclusively run as a script (the `scripts/` dir, `connectors/jira/scripts/`) → replace with `setup_logging(__name__)`.

| File | Verdict | Action |
|------|---------|--------|
| `src/catalog_export.py` | library + has `if __name__` block | check: if `basicConfig` is inside `__main__`, replace; else remove |
| `src/profiler.py` | library | remove |
| `connectors/keboola/extractor.py:289` | library, has CLI block at bottom | the `_logging.basicConfig` is inside the CLI block — replace with `setup_logging(__name__)` |
| `connectors/jira/transform.py:20` | library | **remove** (module-level) |
| `connectors/jira/incremental_transform.py:38` | library | **remove** (module-level) |
| `connectors/jira/extract_init.py:124` | inside `if __name__ == "__main__"` | replace |
| `connectors/jira/scripts/*.py` (5 files) | scripts | replace |
| `scripts/migrate_*.py` (4 files) | scripts | replace |
| `scripts/generate_sample_data.py:1035` | inside `if __name__ == "__main__"` | replace |

- [ ] **Step 5.2: Apply removals (library files)**

For each library file with module-level `basicConfig`:

`connectors/jira/transform.py:20`:
- delete the line `logging.basicConfig(level=logging.INFO)`.

`connectors/jira/incremental_transform.py:38`:
- delete the line `logging.basicConfig(level=logging.INFO)`.

`src/profiler.py:28`:
- delete the multi-line `logging.basicConfig(...)` call.

`src/catalog_export.py:43`:
- inspect: if outside any `__main__` block, delete the multi-line `basicConfig`; if inside, do step 5.3 instead.

```bash
sed -n '40,55p' src/catalog_export.py
sed -n '25,35p' src/profiler.py
```

- [ ] **Step 5.3: Apply replacements (entrypoint files)**

For each entrypoint file, replace the `basicConfig(...)` block with:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

Files: `connectors/keboola/extractor.py:289` (note: uses `_logging` alias — replace the call inside the same scope), `connectors/jira/extract_init.py:124`, all 5 `connectors/jira/scripts/*.py`, all 4 `scripts/migrate_*.py`, `scripts/generate_sample_data.py:1035`.

For `connectors/keboola/extractor.py`:
```python
# was:
#     _logging.basicConfig(level=_logging.INFO, format="%(levelname)s: %(message)s")
from app.logging_config import setup_logging
setup_logging(__name__)
```

- [ ] **Step 5.4: Verify no `basicConfig` left in production code**

```bash
grep -rEn 'logging\.basicConfig|_logging\.basicConfig' \
    src/ app/ services/ connectors/ scripts/ \
    --include="*.py" | grep -v "tests/" || echo "CLEAN"
```

Expected: prints `CLEAN`. If anything matches, decide library vs entrypoint and fix.

- [ ] **Step 5.5: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 5.6: Smoke-test one CLI script**

```bash
DATA_DIR=/tmp/agnes-smoke uv run python -m scripts.migrate_metrics_to_duckdb --help 2>&1 | head -10 || true
```

Expected: usage message appears with no double-config warnings.

- [ ] **Step 5.7: Run quality checks**

```bash
ruff format src/ connectors/ scripts/
ruff check src/ connectors/ scripts/ --fix
```

- [ ] **Step 5.8: Commit**

```bash
git add src/ connectors/ scripts/
git commit -m "refactor(logging): remove scattered basicConfig calls across libs and scripts"
```

---

## Task 6: Add `fastapi-debug-toolbar` dev dep + wire middleware behind `DEBUG=1` — Phase P5

**Files:**
- Modify: `pyproject.toml` (add dev dep)
- Modify: `app/main.py` (mount `DebugToolbarMiddleware` only if `DEBUG=1`)

- [ ] **Step 6.1: Add the dev dependency**

Edit `pyproject.toml` `[project.optional-dependencies].dev` block:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.0.0",
    "pytest-timeout>=2.0.0",
    "pytest-xdist>=3.0.0",
    "faker>=24.0.0",
    "anthropic>=0.30.0",
    "openai>=1.30.0",
    "jsonschema>=4.0.0",
    "fastapi-debug-toolbar>=0.6.3",
]
```

Also mirror under `[tool.uv].dev-dependencies` if present:

```toml
[tool.uv]
dev-dependencies = [
    "pytest>=9.0.0",
    "pytest-timeout>=2.0.0",
    "pytest-xdist>=3.0.0",
    "faker>=24.0.0",
    "anthropic>=0.30.0",
    "openai>=1.30.0",
    "fastapi-debug-toolbar>=0.6.3",
]
```

- [ ] **Step 6.2: Install the new dep**

```bash
uv pip install ".[dev]"
```

Expected: installs `fastapi-debug-toolbar`, `Jinja2` (already a dep), `pyinstrument`.

- [ ] **Step 6.3: Wire `DebugToolbarMiddleware` in `app/main.py`**

Locate the `app.add_middleware(RequestIdMiddleware)` line added in Task 3. Immediately after it, add:

```python
import os

DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

if DEBUG:
    try:
        from debug_toolbar.middleware import DebugToolbarMiddleware
        app.add_middleware(
            DebugToolbarMiddleware,
            panels=[
                "debug_toolbar.panels.headers.HeadersPanel",
                "debug_toolbar.panels.routes.RoutesPanel",
                "debug_toolbar.panels.settings.SettingsPanel",
                "debug_toolbar.panels.versions.VersionsPanel",
                "debug_toolbar.panels.timer.TimerPanel",
                "debug_toolbar.panels.logging.LoggingPanel",
                "debug_toolbar.panels.profiling.ProfilingPanel",
            ],
            settings={"REFRESH_INTERVAL": 5000},
        )
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "DEBUG=1 but fastapi-debug-toolbar not installed; toolbar disabled"
        )
```

The `DuckDBPanel` is added in Task 8, not here.

Also adjust the `app = FastAPI(...)` call to honor `DEBUG`:

```python
app = FastAPI(debug=DEBUG, ...existing kwargs...)
```

- [ ] **Step 6.4: Manual browser verification**

```bash
DEBUG=1 uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 3
```

Use `playwright-cli` for verification (per `browser-automation.md`):

```bash
playwright-cli -s=$PILOT_SESSION_ID open http://localhost:8011/dashboard
playwright-cli -s=$PILOT_SESSION_ID snapshot
# Expect to see a "djdt" or toolbar tab in the accessibility tree
playwright-cli -s=$PILOT_SESSION_ID screenshot /tmp/toolbar.png
playwright-cli -s=$PILOT_SESSION_ID close

kill $APP_PID
```

Expected: screenshot shows toolbar tab on right edge of the page. If the route requires auth, set `LOCAL_DEV_MODE=1` too.

If you don't have playwright-cli, use this curl + grep verification:

```bash
DEBUG=1 uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 3
LOCAL_DEV_MODE=1 curl -s http://localhost:8011/dashboard 2>/dev/null | grep -i "fastDebug\|debug.*toolbar" | head -3
kill $APP_PID
```

Expected: at least one match showing the toolbar HTML was injected.

- [ ] **Step 6.5: Verify toolbar is OFF without DEBUG**

```bash
uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 3
curl -s http://localhost:8011/dashboard 2>/dev/null | grep -ci "fastDebug" || echo "no toolbar (correct)"
kill $APP_PID
```

Expected: `no toolbar (correct)`.

- [ ] **Step 6.6: Run the full test suite**

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 6.7: Commit**

```bash
git add pyproject.toml app/main.py uv.lock
git commit -m "feat(dev): mount fastapi-debug-toolbar behind DEBUG=1"
```

---

## Task 7: `InstrumentedConnection` — Phase P6 (part 1)

**Files:**
- Create: `app/debug/__init__.py` (empty)
- Create: `app/debug/duckdb_panel.py` (just `InstrumentedConnection` and helpers — `DuckDBPanel` itself comes in Task 8)
- Test: `tests/test_duckdb_panel.py`

- [ ] **Step 7.1: Create empty package marker**

```bash
mkdir -p app/debug/templates/panels
: > app/debug/__init__.py
```

- [ ] **Step 7.2: Write the failing tests**

Create `tests/test_duckdb_panel.py`:

```python
import duckdb
import pytest

from app.debug.duckdb_panel import (
    InstrumentedConnection,
    _request_store,
    get_request_store,
    record_query,
)


@pytest.fixture
def store_token():
    """Provide a fresh request-scoped store."""
    token = _request_store.set([])
    yield
    _request_store.reset(token)


@pytest.fixture
def conn():
    return duckdb.connect(":memory:")


def test_records_query(store_token, conn):
    inst = InstrumentedConnection(conn, "system")
    inst.execute("SELECT 1")
    store = get_request_store()
    assert len(store) == 1
    q = store[0]
    assert q.db == "system"
    assert q.sql == "SELECT 1"
    assert q.error is None
    assert q.ms >= 0


def test_records_query_with_params(store_token, conn):
    inst = InstrumentedConnection(conn, "analytics")
    inst.execute("SELECT $1::INT", [42])
    store = get_request_store()
    assert len(store) == 1
    assert store[0].params == [42]


def test_records_error(store_token, conn):
    inst = InstrumentedConnection(conn, "system")
    with pytest.raises(duckdb.Error):
        inst.execute("SELECT FROM bogus_syntax")
    store = get_request_store()
    assert len(store) == 1
    assert store[0].error is not None
    assert "bogus_syntax" in store[0].error or "syntax" in store[0].error.lower()


def test_db_tag_preserved(store_token):
    a = duckdb.connect(":memory:")
    b = duckdb.connect(":memory:")
    InstrumentedConnection(a, "system").execute("SELECT 1")
    InstrumentedConnection(b, "analytics").execute("SELECT 2")
    store = get_request_store()
    assert {q.db for q in store} == {"system", "analytics"}


def test_no_op_outside_request(conn):
    """When _request_store is None (outside a debug request), do not raise."""
    assert get_request_store() is None
    inst = InstrumentedConnection(conn, "system")
    inst.execute("SELECT 1")  # must not raise
    assert get_request_store() is None


def test_passthrough_attributes(conn):
    """Wrapper must delegate non-execute methods to the real connection."""
    inst = InstrumentedConnection(conn, "system")
    inst.execute("CREATE TABLE t (x INT)")
    inst.execute("INSERT INTO t VALUES (1), (2), (3)")
    rows = inst.execute("SELECT x FROM t ORDER BY x").fetchall()
    assert rows == [(1,), (2,), (3,)]


def test_cursor_returns_instrumented(store_token, conn):
    """A cursor() call returns an InstrumentedConnection wrapping the real cursor."""
    inst = InstrumentedConnection(conn, "system")
    cur = inst.cursor()
    assert isinstance(cur, InstrumentedConnection)
    cur.execute("SELECT 99")
    store = get_request_store()
    assert len(store) == 1
    assert store[0].db == "system"


def test_record_query_no_op_when_store_none():
    """record_query is safe to call when no store is set."""
    record_query("system", "SELECT 1", None, 0.0, None)  # must not raise
```

- [ ] **Step 7.3: Run tests, verify they fail**

```bash
uv run pytest tests/test_duckdb_panel.py -q
```

Expected: `ImportError: cannot import name 'InstrumentedConnection'`.

- [ ] **Step 7.4: Implement `InstrumentedConnection` and helpers**

Create `app/debug/duckdb_panel.py` (panel class will be added in Task 8):

```python
"""Per-request DuckDB query capture for the dev debug toolbar.

`InstrumentedConnection` wraps a `duckdb.DuckDBPyConnection` and records
every `.execute()` call into a contextvar-scoped store. The toolbar's
`DuckDBPanel` reads that store at response time.

When the contextvar is unset (outside a debug request, or in prod), all
recording is a no-op — `_maybe_instrument()` in `src/db.py` returns the
raw connection, so the wrapper isn't even constructed in prod paths.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
from typing import Any

import duckdb


@dataclass
class Query:
    db: str
    sql: str
    params: Any
    ms: float
    rows: int | None
    error: str | None = None


_request_store: contextvars.ContextVar[list[Query] | None] = contextvars.ContextVar(
    "duckdb_panel_store", default=None
)


def get_request_store() -> list[Query] | None:
    """Return the current request's query buffer, or None outside a debug request."""
    return _request_store.get()


def record_query(
    db: str,
    sql: str,
    params: Any,
    started: float,
    rows: int | None,
    error: str | None = None,
) -> None:
    store = _request_store.get()
    if store is None:
        return
    store.append(
        Query(
            db=db,
            sql=sql,
            params=params,
            ms=(time.perf_counter() - started) * 1000.0,
            rows=rows,
            error=error,
        )
    )


class InstrumentedConnection:
    """`duckdb.DuckDBPyConnection`-compatible wrapper that records queries."""

    def __init__(self, real: duckdb.DuckDBPyConnection, db_tag: str) -> None:
        self._real = real
        self._db = db_tag

    def execute(self, sql: str, params: Any = None, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        err: str | None = None
        result = None
        try:
            if params is not None:
                result = self._real.execute(sql, params, *args, **kwargs)
            else:
                result = self._real.execute(sql, *args, **kwargs)
            return result
        except Exception as e:
            err = repr(e)
            raise
        finally:
            rows: int | None = None
            try:
                if result is not None and hasattr(result, "rowcount"):
                    rows = result.rowcount
            except Exception:
                pass
            record_query(self._db, sql, params, started, rows, err)

    def cursor(self) -> "InstrumentedConnection":
        return InstrumentedConnection(self._real.cursor(), self._db)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)
```

- [ ] **Step 7.5: Run tests, verify they pass**

```bash
uv run pytest tests/test_duckdb_panel.py -q
```

Expected: all 8 tests pass.

- [ ] **Step 7.6: Run quality checks**

```bash
ruff format app/debug/ tests/test_duckdb_panel.py
ruff check app/debug/ tests/test_duckdb_panel.py --fix
```

- [ ] **Step 7.7: Commit**

```bash
git add app/debug/__init__.py app/debug/duckdb_panel.py tests/test_duckdb_panel.py
git commit -m "feat(debug): add InstrumentedConnection for per-request DuckDB query capture"
```

---

## Task 8: `DuckDBPanel` + Jinja template + integration in `src/db.py` — Phase P6 (part 2)

**Files:**
- Modify: `app/debug/duckdb_panel.py` (append `DuckDBPanel` class)
- Create: `app/debug/templates/panels/duckdb.html`
- Modify: `src/db.py` (add `_maybe_instrument`; wrap returns)
- Modify: `app/main.py` (add `app.debug.duckdb_panel.DuckDBPanel` to the panels list)
- Test: `tests/test_toolbar_integration.py`

- [ ] **Step 8.1: Append `DuckDBPanel` to `app/debug/duckdb_panel.py`**

Add at the bottom of the file:

```python
# Toolbar Panel (only imported when DEBUG=1, so import inside the class block)
try:
    from debug_toolbar.panels import Panel
    from debug_toolbar.types import ServerTiming, Stats

    class DuckDBPanel(Panel):
        """fastapi-debug-toolbar panel rendering captured DuckDB queries."""

        title = "DuckDB"
        template = "panels/duckdb.html"

        @property
        def nav_subtitle(self) -> str:
            store = get_request_store() or []
            total_ms = sum(q.ms for q in store)
            return f"{len(store)} queries · {total_ms:.1f} ms"

        async def process_request(self, request):
            _request_store.set([])
            return await super().process_request(request)

        async def generate_stats(self, request, response) -> Stats | None:
            store = get_request_store() or []
            return {
                "queries": [q.__dict__ for q in store],
                "total_ms": sum(q.ms for q in store),
                "by_db": {
                    db: sum(q.ms for q in store if q.db == db)
                    for db in {q.db for q in store}
                },
            }

        async def generate_server_timing(self, request, response) -> ServerTiming:
            store = get_request_store() or []
            return [("DuckDB", "DuckDB queries", sum(q.ms for q in store))]
except ImportError:
    # fastapi-debug-toolbar not installed in this environment (e.g. prod).
    # The class is only referenced when DEBUG=1, where the dep is required.
    DuckDBPanel = None  # type: ignore[assignment, misc]
```

- [ ] **Step 8.2: Create the Jinja template**

Create `app/debug/templates/panels/duckdb.html`:

```jinja
<h4>DuckDB queries — {{ stats.queries|length }} ({{ "%.1f"|format(stats.total_ms) }} ms total)</h4>
{% if stats.by_db %}
<p>By DB:
  {% for db, ms in stats.by_db.items() %}<code>{{ db }}</code>: {{ "%.1f"|format(ms) }} ms{% if not loop.last %} · {% endif %}{% endfor %}
</p>
{% endif %}
<table class="djdt-table">
  <thead>
    <tr><th>#</th><th>DB</th><th>ms</th><th>rows</th><th>SQL</th><th>params</th></tr>
  </thead>
  <tbody>
  {% for q in stats.queries %}
    <tr class="{{ 'djdt-error' if q.error else '' }}">
      <td>{{ loop.index }}</td>
      <td>{{ q.db }}</td>
      <td>{{ "%.2f"|format(q.ms) }}</td>
      <td>{{ q.rows if q.rows is not none else '—' }}</td>
      <td>
        <pre>{{ q.sql }}</pre>
        {% if q.error %}<div class="djdt-error">{{ q.error }}</div>{% endif %}
      </td>
      <td><code>{{ q.params }}</code></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
```

- [ ] **Step 8.3: Wrap connections in `src/db.py`**

The file has three `duckdb.connect()` call sites: lines 413, 423, 581, 587. The three public helpers return:
- `get_system_db()` — returns `_system_db_conn.cursor()` (line 416). Wrap the cursor.
- `get_analytics_db()` — returns the connection directly (line 423). Wrap it.
- `get_analytics_db_readonly()` — two return paths (lines 586, 587 onward). Wrap both.

Add this helper near the top of `src/db.py` (just after the imports):

```python
import os as _os_for_debug

_DEBUG = _os_for_debug.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def _maybe_instrument(con, db_tag: str):
    """Wrap a duckdb connection with InstrumentedConnection when DEBUG=1, else return as-is."""
    if not _DEBUG:
        return con
    from app.debug.duckdb_panel import InstrumentedConnection
    return InstrumentedConnection(con, db_tag)
```

Then modify each helper:

**`get_system_db()`** at line 394 — the `return _system_db_conn.cursor()` becomes:

```python
return _maybe_instrument(_system_db_conn.cursor(), "system")
```

**`get_analytics_db()`** at line 419 — the `return duckdb.connect(str(db_path))` becomes:

```python
return _maybe_instrument(duckdb.connect(str(db_path)), "analytics")
```

**`get_analytics_db_readonly()`** at line 573 — both return statements (line 586 with `return conn` and line 587 with `conn = duckdb.connect(str(db_path), read_only=True)` followed by ATTACH + final `return conn`) get the wrap applied at the **final** return only:

The function ends with something like `return conn` after ATTACH loop. Replace that final `return conn` with `return _maybe_instrument(conn, "analytics_ro")`.

For the early-return path (lines 581–586) where the file doesn't exist: replace the early `return conn` with `return _maybe_instrument(conn, "analytics_ro")`.

```bash
sed -n '580,605p' src/db.py  # verify exact return sites before editing
```

- [ ] **Step 8.4: Mount `DuckDBPanel` in `app/main.py`**

Edit the panel list created in Task 6.3. Add the DuckDBPanel string to the list:

```python
panels=[
    "debug_toolbar.panels.headers.HeadersPanel",
    "debug_toolbar.panels.routes.RoutesPanel",
    "debug_toolbar.panels.settings.SettingsPanel",
    "debug_toolbar.panels.versions.VersionsPanel",
    "debug_toolbar.panels.timer.TimerPanel",
    "debug_toolbar.panels.logging.LoggingPanel",
    "debug_toolbar.panels.profiling.ProfilingPanel",
    "app.debug.duckdb_panel.DuckDBPanel",
],
```

The toolbar resolves panels by import string. Confirm `app/debug/duckdb_panel.py` exposes `DuckDBPanel` at module level (it does — see Step 8.1).

- [ ] **Step 8.5: Configure Jinja template lookup**

The toolbar needs to find `panels/duckdb.html`. Wire the template path by adding a conftest-style env or by passing the path to the middleware via `settings`. The simplest path: copy/symlink the template into the toolbar's expected lookup. Verify by reading `fastapi-debug-toolbar` docs:

```bash
uv run python -c "import debug_toolbar.middleware as m; import inspect; print(inspect.getfile(m))"
```

Then check how panels with custom templates register lookup paths. If the toolbar uses Jinja `PackageLoader`, set `template = "panels/duckdb.html"` AND ensure `app/debug/templates/` is on its lookup path. The standard way (per upstream) is to set the `template` attribute and rely on the panel's own package; since `DuckDBPanel` lives in `app.debug.duckdb_panel`, the toolbar should resolve `app/debug/templates/panels/duckdb.html` automatically via package introspection.

If the toolbar fails to find the template, add this class attribute on `DuckDBPanel`:

```python
template_dirs = ["app/debug/templates"]
```

Verify by hitting an HTML route with `DEBUG=1` and inspecting the rendered toolbar HTML.

- [ ] **Step 8.6: Write the integration tests**

Create `tests/test_toolbar_integration.py`:

```python
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def reset_logging_state():
    import app.logging_config as lc
    lc._CONFIGURED = False
    yield
    lc._CONFIGURED = False
    import logging
    logging.getLogger().handlers.clear()


@pytest.fixture
def app_with_toolbar(monkeypatch, reset_logging_state):
    monkeypatch.setenv("DEBUG", "1")
    # Reload app.main fresh so DEBUG gate is honored
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)
    return main_mod.app


@pytest.fixture
def app_no_toolbar(monkeypatch, reset_logging_state):
    monkeypatch.delenv("DEBUG", raising=False)
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)
    return main_mod.app


@pytest.mark.integration
def test_toolbar_html_present_when_debug(app_with_toolbar):
    client = TestClient(app_with_toolbar)
    # Hit any HTML response endpoint (the dashboard or login page)
    resp = client.get("/dashboard", follow_redirects=False)
    if resp.status_code in (302, 401):
        pytest.skip("Dashboard requires auth in this env; visual verify covered separately")
    assert resp.status_code == 200
    body = resp.text.lower()
    assert "djdt" in body or "fastdebug" in body, "Toolbar markup not injected"


@pytest.mark.integration
def test_no_toolbar_when_debug_off(app_no_toolbar):
    client = TestClient(app_no_toolbar)
    resp = client.get("/dashboard", follow_redirects=False)
    if resp.status_code in (302, 401):
        return
    body = resp.text.lower()
    assert "djdt" not in body
    assert "fastdebug" not in body


@pytest.mark.integration
def test_request_id_header_always_present(app_no_toolbar):
    client = TestClient(app_no_toolbar)
    resp = client.get("/health")
    assert "x-request-id" in resp.headers
```

- [ ] **Step 8.7: Run integration tests**

```bash
uv run pytest tests/test_toolbar_integration.py -q
```

Expected: tests pass (or skip when auth required). If `dashboard` is hidden behind login, test against another HTML route (`/login` or `/setup`) — adjust the fixture to a route known to render HTML without auth.

- [ ] **Step 8.8: Manual browser verification with playwright-cli**

```bash
DEBUG=1 LOCAL_DEV_MODE=1 uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 4

playwright-cli -s=$PILOT_SESSION_ID open http://localhost:8011/admin/access
playwright-cli -s=$PILOT_SESSION_ID snapshot
playwright-cli -s=$PILOT_SESSION_ID screenshot /tmp/toolbar-duckdb.png
playwright-cli -s=$PILOT_SESSION_ID close

kill $APP_PID
ls -la /tmp/toolbar-duckdb.png
```

Open `/tmp/toolbar-duckdb.png` (or read with the Read tool — it accepts images). Confirm:
- Toolbar tab visible on right edge
- Clicking opens panels list including **DuckDB**
- DuckDB panel shows queries with `system` / `analytics_ro` tags

If toolbar tab not visible: check uvicorn logs for `fastapi-debug-toolbar not installed` warning or template-loader errors.

- [ ] **Step 8.9: Run the full test suite**

```bash
uv run pytest -q --cov=app.logging_config --cov=app.middleware --cov=app.debug --cov-fail-under=80
```

Expected: tests pass and coverage on the new modules ≥ 80%.

- [ ] **Step 8.10: Run quality checks**

```bash
ruff format app/ src/db.py tests/test_toolbar_integration.py
ruff check app/ src/db.py tests/test_toolbar_integration.py --fix
```

- [ ] **Step 8.11: Commit**

```bash
git add app/debug/ src/db.py app/main.py tests/test_toolbar_integration.py
git commit -m "feat(debug): add DuckDBPanel rendering per-request queries with db tag"
```

---

## Task 9: Documentation, `.env.template`, CHANGELOG — Phase P7

**Files:**
- Modify: `config/.env.template` (or `.env.template` at repo root — verify location)
- Create: `docs/development.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 9.1: Locate the env template**

```bash
ls -la config/.env.template .env.template 2>/dev/null
```

The CLAUDE.md references `config/.env.template`. Verify path before editing.

- [ ] **Step 9.2: Document `DEBUG=1` in env template**

Append to `config/.env.template` (or wherever it lives):

```bash

# === Local development ===
# DEBUG=1 enables:
#   - FastAPI debug=True (richer 500 pages)
#   - rich.logging.RichHandler (colored, with tracebacks)
#   - fastapi-debug-toolbar mounted at right edge of HTML pages
#   - DuckDB query capture in the toolbar
# Never set in production. Keep separate from LOCAL_DEV_MODE (auth bypass).
# DEBUG=1
```

- [ ] **Step 9.3: Create developer docs**

Create `docs/development.md`:

```markdown
# Development guide

## Logging

All processes (FastAPI app, scheduler, telegram_bot, ws_gateway, corporate_memory,
session_collector, CLI scripts) use `app.logging_config.setup_logging` to configure
the root logger. Each entrypoint calls it once:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

Library modules just do `logger = logging.getLogger(__name__)` — they never
configure root.

| Env | Handler | Format |
|-----|---------|--------|
| `DEBUG=1` | `rich.logging.RichHandler` | colored, clickable file:line, pretty tracebacks |
| (default) | stdlib `StreamHandler` | JSON to stderr (`ts`, `lvl`, `logger`, `service`, `msg`, optional `request_id`) |

`LOG_LEVEL` overrides the level (default `DEBUG` when `DEBUG=1`, else `INFO`).

## Request correlation

`RequestIdMiddleware` is mounted unconditionally on the FastAPI app. It assigns
or propagates `X-Request-ID` and exposes it via the `request_id_var`
ContextVar so the JSON formatter and the debug-toolbar logging panel see the
same id.

## Debug toolbar

Set `DEBUG=1` to mount `fastapi-debug-toolbar`. Visit any HTML page (e.g.
`/admin/access`, `/dashboard`, `/login`) and click the small toolbar tab on
the right edge.

Panels:

- **Headers** — request/response headers
- **Routes** — registered FastAPI routes
- **Settings** — Pydantic / instance-config values
- **Versions** — installed package versions
- **Timer** — request duration
- **Logging** — log records emitted during the request
- **Profiling** — `pyinstrument` flame graph
- **DuckDB** — every `con.execute(sql, params)` from `src/db.py`, tagged
  by `system` / `analytics` / `analytics_ro`, with timing and row count

JSON-only endpoints (Swagger UI at `/docs`) replay the most recent request's
panels via a cookie-based mechanism.

The toolbar is **never imported in production**: the import is gated by
`if DEBUG: ...` in `app/main.py`, and `fastapi-debug-toolbar` lives only in
the dev optional-dependency group.

## Running locally

```bash
uv pip install ".[dev]"
DEBUG=1 LOCAL_DEV_MODE=1 uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/dashboard`.
```

- [ ] **Step 9.4: Update CHANGELOG.md**

Read the top of `CHANGELOG.md`:

```bash
sed -n '1,30p' CHANGELOG.md
```

If a `## [Unreleased]` heading exists at the top, add bullets under it. If not, create one above the topmost released version.

Add under `### Added`:

```markdown
## [Unreleased]

### Added
- Dev debug toolbar gated by `DEBUG=1`. Mounts `fastapi-debug-toolbar` with panels
  for headers, routes, settings, versions, timer, logging, profiling, and a custom
  DuckDB panel that captures every `con.execute()` from `src/db.py` (tagged by
  `system` / `analytics` / `analytics_ro`). See `docs/development.md`.
- `X-Request-ID` request header / response header on every FastAPI response, plus
  a `request_id` field in JSON logs for cross-process correlation.
- Centralized `app.logging_config.setup_logging()` — replaces 23 scattered
  `logging.basicConfig(...)` calls. Uses `rich.logging.RichHandler` in dev
  (`DEBUG=1`) and JSON to stderr in prod.

### Changed
- All service entrypoints (`services/scheduler/__main__.py`, `ws_gateway`,
  `telegram_bot`, `corporate_memory`, `session_collector`, `verification_detector`)
  and CLI scripts under `scripts/` and `connectors/jira/scripts/` now call
  `setup_logging(__name__)` instead of inline `basicConfig`. Library modules no
  longer configure root logger at import time.

### Fixed
- Removed rogue module-level `logging.basicConfig` from `app/api/sync.py` that
  was reconfiguring root logger every time the api module was imported.
```

- [ ] **Step 9.5: Run the full test suite one more time**

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 9.6: Final verification**

Confirm no remaining `basicConfig` outside tests:

```bash
grep -rEn 'logging\.basicConfig|_logging\.basicConfig' \
    src/ app/ services/ connectors/ scripts/ \
    --include="*.py" | grep -v "tests/" || echo "CLEAN"
```

Expected: `CLEAN`.

Confirm `DEBUG=1` smoke test:

```bash
DEBUG=1 LOCAL_DEV_MODE=1 uv run uvicorn app.main:app --port 8011 &
APP_PID=$!
sleep 4
curl -s http://localhost:8011/health -i 2>/dev/null | head -5
kill $APP_PID
```

Expected: response with `x-request-id` header. Server log line is rich-colored.

- [ ] **Step 9.7: Commit**

```bash
git add config/.env.template docs/development.md CHANGELOG.md
git commit -m "docs: document DEBUG=1 toggle and centralized logging"
```

- [ ] **Step 9.8: Push branch**

```bash
git push -u origin vr/dev-logging
```

Open a PR via `gh pr create` (out of scope for this plan — the user runs PR creation separately).

---

## Self-Review Checklist (already applied during plan writing)

- [x] **Spec coverage:** every section in the spec maps to one of Tasks 1–9.
  - logging_config + RichHandler → Task 1
  - RequestIdMiddleware → Tasks 2–3
  - migration of 23 basicConfig calls → Tasks 3, 4, 5
  - DuckDBPanel + InstrumentedConnection → Tasks 7–8
  - production safeguards (DEBUG gate, dev-only dep) → Task 6
  - docs / CHANGELOG / .env.template → Task 9
- [x] **Placeholder scan:** no TBD, TODO, or "implement later" left in the plan.
- [x] **Type consistency:** `Query` dataclass used in tests and in `DuckDBPanel.generate_stats`; `InstrumentedConnection.cursor()` returns `InstrumentedConnection` (matches test).
- [x] **Files-not-to-modify rule:** plan does NOT touch `connectors/jira/file_lock.py`, `connectors/jira/transform.py` is library-level — only the `basicConfig` line is removed (plan respects "core logic" stability), `services/ws_gateway/` is touched only at the entrypoint `gateway.py:214` for the `basicConfig` swap (no logic change).

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-29-dev-debug-toolbar.md`.**
