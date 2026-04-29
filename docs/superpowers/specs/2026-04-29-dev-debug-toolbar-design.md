# Dev Debug Toolbar + Centralized Logging — Design

**Date:** 2026-04-29
**Status:** Draft (awaiting user review)
**Owner:** vrysanek

## Goal

Give developers a Flask-DebugToolbar-style panel UI in the FastAPI app for local dev, plus consistent log output across the FastAPI app and 5 background services. Make it cheap to leave on in dev, free to ship in production.

Two scopes, one design:

1. **Debug toolbar** in the FastAPI app — visible only in dev, exposing per-request panels: timer, headers, routes, settings, versions, **logging records**, **pyinstrument profiler**, and a custom **DuckDB queries panel**.
2. **Centralized logging** — replace 20+ scattered `logging.basicConfig(...)` calls across `app/`, `services/`, `connectors/`, `src/`, `scripts/` with a single `setup_logging()` helper that uses `rich.logging.RichHandler` in dev and JSON to stdout in prod.

Out of scope: production observability, log shipping, distributed tracing, structured logging refactor of every `logger.info("foo")` call site.

---

## Non-goals / explicit rejections

- **Not** building an in-house toolbar (rejected: 1–2 days build time when `fastapi-debug-toolbar` covers 80% of the need).
- **Not** introducing `loguru` or `structlog` (rejected: requires rewriting 40+ files using stdlib `logging`).
- **Not** auto-deriving service slug via `sys._getframe(1)` magic (rejected after Gemini adversarial review: brittle on PyPy, decorators, indirect calls). Use explicit `setup_logging(__name__)`.
- **Not** instrumenting DuckDB queries in production (rejected: per-query overhead, only needed for dev panel).

---

## Activation

One environment variable: `DEBUG`.

| `DEBUG` | FastAPI `debug=` | Toolbar middleware | Logging handler | DuckDB instrumentation |
|---------|------------------|--------------------|-----------------|------------------------|
| unset / `0` | `False` | not mounted, not imported | `StreamHandler` + JSON formatter | no-op (raw `duckdb.Connection`) |
| `1` | `True` | mounted | `RichHandler` (color, tracebacks, links) | `InstrumentedConnection` records queries per request |

Orthogonal to `LOCAL_DEV_MODE` (auth bypass) — they do different things, both can be set independently.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│ FastAPI app process                                           │
│                                                               │
│  app/main.py                                                  │
│    ├── setup_logging("app")                                   │
│    ├── FastAPI(debug=DEBUG)                                   │
│    ├── app.add_middleware(RequestIdMiddleware)                │
│    └── if DEBUG:                                              │
│          app.add_middleware(DebugToolbarMiddleware,           │
│            panels=[Headers, Routes, Settings, Versions,       │
│                    Timer, Logging, Profiling,                 │
│                    ★ DuckDBPanel ★])                          │
│                                                               │
│  app/logging_config.py        ← setup_logging, _JSONFormatter │
│  app/middleware/request_id.py ← RequestIdMiddleware           │
│  app/debug/duckdb_panel.py    ← DuckDBPanel,                  │
│                                  InstrumentedConnection       │
│  src/db.py (modified)         ← _maybe_instrument helper      │
└───────────────────────────────────────────────────────────────┘

Background services (scheduler, telegram_bot, ws_gateway,
                    corporate_memory, session_collector):
  └── each entrypoint: setup_logging(__name__)
      → kills 20+ scattered basicConfig calls
      → unified format (Rich in dev, JSON in prod)
```

### Component boundaries

| Component | Public interface | Depends on |
|-----------|------------------|------------|
| `app/logging_config.py` | `setup_logging(service=None, level=None)`, `request_id_var: ContextVar[str\|None]` | stdlib `logging`, `rich`, env vars |
| `app/middleware/request_id.py` | `RequestIdMiddleware` (Starlette `BaseHTTPMiddleware`) | `request_id_var` |
| `app/debug/duckdb_panel.py` | `DuckDBPanel` (toolbar Panel), `InstrumentedConnection` (`duckdb.DuckDBPyConnection`-compatible), `record_query()`, `get_request_store()` | `fastapi-debug-toolbar`, `duckdb`, `contextvars` |
| `src/db.py` (changed) | unchanged signatures; internal `_maybe_instrument(con, db_tag)` returns raw or wrapped connection based on `DEBUG` | `app.debug.duckdb_panel` (lazy import behind `DEBUG`) |

A library module never calls `setup_logging()`. Only entrypoints (`__main__.py`, `main.py`, top-level CLI scripts).

---

## Detailed design

### `app/logging_config.py`

```python
import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_CONFIGURED = False


def setup_logging(service: str | None = None, level: str | None = None) -> None:
    """Configure root logger. Idempotent. Call once per process at entrypoint.

    Pass `__name__` (preferred) or an explicit short service slug.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    lvl = (level or os.environ.get("LOG_LEVEL") or ("DEBUG" if debug else "INFO")).upper()
    slug = _derive_slug(service)

    if debug:
        from rich.logging import RichHandler
        handler = RichHandler(
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_time=True,
            show_path=True,
            markup=False,
            force_terminal=True,        # docker compose: stdout not a TTY but still want color
        )
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter(service=slug))

    logging.basicConfig(level=lvl, handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO if debug else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def _derive_slug(service: str | None) -> str:
    """Turn module name (`__name__`) or override into readable service slug.

    setup_logging("app")                           -> "app"
    setup_logging("services.corporate_memory.collector")
                                                   -> "corporate_memory.collector"
    setup_logging("services.scheduler.__main__")   -> "scheduler"
    setup_logging("__main__") (direct script run)  -> derived from caller's __file__
    """
    if service and not service.startswith("_") and service != "__main__":
        s = service.removeprefix("services.").removeprefix("connectors.").removeprefix("app.")
        s = s.removesuffix(".__main__").removesuffix(".main")
        return s or "app"

    # Fallback: parse caller's __file__
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
        payload = {
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

Adversarial flaws addressed (per Gemini review 2026-04-29):

| Flaw | Mitigation |
|------|------------|
| `sys._getframe` brittleness | Used only as a fallback when caller can't supply `__name__`; primary path is explicit `setup_logging(__name__)` |
| `root.handlers.clear()` wipes uvicorn handlers | Replaced with `logging.basicConfig(force=True)` — semantically equivalent but well-defined |
| RichHandler color-detection in docker | `force_terminal=True` — color is intentional in dev, dev never goes to log shippers |
| Path leakage via `show_path=True` | Dev only. Prod uses `_JSONFormatter`, no source paths |
| Non-idempotent | `_CONFIGURED` sentinel, repeated calls become no-ops |

### `app/middleware/request_id.py`

```python
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from app.logging_config import request_id_var


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response
```

Mounted unconditionally (cheap, useful in prod for correlation too). Sits **before** `DebugToolbarMiddleware` so request id is set when the toolbar captures.

### `app/debug/duckdb_panel.py`

```python
from __future__ import annotations
import contextvars
import time
from dataclasses import dataclass
from typing import Any

import duckdb
from debug_toolbar.panels import Panel
from debug_toolbar.types import ServerTiming, Stats


@dataclass
class _Query:
    db: str         # "system" | "analytics"
    sql: str
    params: Any
    ms: float
    rows: int | None
    error: str | None = None


_request_store: contextvars.ContextVar[list[_Query] | None] = contextvars.ContextVar(
    "duckdb_panel_store", default=None
)


def get_request_store() -> list[_Query] | None:
    return _request_store.get()


def record_query(db: str, sql: str, params: Any, started: float,
                 rows: int | None, error: str | None = None) -> None:
    store = _request_store.get()
    if store is None:                  # outside debug request: no-op
        return
    store.append(_Query(
        db=db, sql=sql, params=params,
        ms=(time.perf_counter() - started) * 1000.0,
        rows=rows, error=error,
    ))


class InstrumentedConnection:
    """duckdb.DuckDBPyConnection-compatible wrapper that records queries."""

    def __init__(self, real: duckdb.DuckDBPyConnection, db_tag: str) -> None:
        self._real = real
        self._db = db_tag

    def execute(self, sql: str, params: Any = None, *args, **kwargs):
        started = time.perf_counter()
        err: str | None = None
        result = None
        try:
            result = (self._real.execute(sql, params, *args, **kwargs)
                      if params is not None
                      else self._real.execute(sql, *args, **kwargs))
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

    def __getattr__(self, name):       # delegate everything else
        return getattr(self._real, name)


class DuckDBPanel(Panel):
    title = "DuckDB"
    template = "panels/duckdb.html"

    @property
    def nav_subtitle(self) -> str:
        store = get_request_store() or []
        return f"{len(store)} queries · {sum(q.ms for q in store):.1f} ms"

    async def process_request(self, request):
        _request_store.set([])
        return await super().process_request(request)

    async def generate_stats(self, request, response) -> Stats | None:
        store = get_request_store() or []
        return {
            "queries": [q.__dict__ for q in store],
            "total_ms": sum(q.ms for q in store),
            "by_db": {db: sum(q.ms for q in store if q.db == db)
                      for db in {q.db for q in store}},
        }

    async def generate_server_timing(self, request, response) -> ServerTiming:
        store = get_request_store() or []
        return [("DuckDB", "DuckDB queries", sum(q.ms for q in store))]
```

Template (`app/debug/templates/panels/duckdb.html`):

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

### `src/db.py` integration

```python
import os
from typing import TYPE_CHECKING

_DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def _maybe_instrument(con, db_tag: str):
    if not _DEBUG:
        return con
    from app.debug.duckdb_panel import InstrumentedConnection
    return InstrumentedConnection(con, db_tag)


# Each existing connect helper wraps its return:
#   def get_system_conn():    return _maybe_instrument(duckdb.connect(...), "system")
#   def get_analytics_conn(): return _maybe_instrument(duckdb.connect(...), "analytics")
```

**Implementation note:** the actual function names in `src/db.py` are not enumerated in this design — the planner reads the file during plan execution and wraps every public connection-returning helper. Wrapping happens at the single point where a fresh `duckdb.connect()` is exposed.

### Toolbar wiring in `app/main.py`

```python
from app.logging_config import setup_logging
setup_logging("app")

import os
from fastapi import FastAPI
from app.middleware.request_id import RequestIdMiddleware

DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

app = FastAPI(debug=DEBUG, ...)
app.add_middleware(RequestIdMiddleware)

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
                "app.debug.duckdb_panel.DuckDBPanel",
            ],
            settings={"REFRESH_INTERVAL": 5000},
        )
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "DEBUG=1 but fastapi-debug-toolbar not installed; toolbar disabled"
        )
```

`SQLAlchemyPanel` and `TortoisePanel` are intentionally **not** included — project uses DuckDB directly.

---

## File layout

```
app/
├── logging_config.py          (new)
├── middleware/
│   └── request_id.py          (new)
├── debug/                     (new)
│   ├── __init__.py
│   ├── duckdb_panel.py
│   └── templates/
│       └── panels/
│           └── duckdb.html
├── main.py                    (mod)  setup_logging + RequestIdMiddleware + debug-gated toolbar
└── api/sync.py                (mod)  remove rogue module-level basicConfig (line 104)

src/
├── db.py                      (mod)  _maybe_instrument helper
├── catalog_export.py          (mod)  remove basicConfig
└── profiler.py                (mod)  remove basicConfig

services/
├── scheduler/__main__.py      (mod)  setup_logging(__name__)
├── ws_gateway/gateway.py      (mod)  setup_logging(__name__)
├── telegram_bot/bot.py        (mod)  setup_logging(__name__)
├── corporate_memory/collector.py    (mod)  setup_logging(__name__)
└── session_collector/collector.py   (mod)  setup_logging(__name__)

connectors/
├── keboola/extractor.py       (mod)  remove basicConfig
└── jira/                      (mod)  remove basicConfig in 9 files

scripts/                       (mod)  setup_logging(__name__) in migrate_*.py and generate_sample_data.py

pyproject.toml                 (mod)  fastapi-debug-toolbar in [project.optional-dependencies].dev
.env.template                  (mod)  document DEBUG=1
docs/development.md            (new)  brief usage doc
CHANGELOG.md                   (mod)  Added: dev debug toolbar; centralized logging

tests/
├── test_logging_config.py     (new)
├── test_request_id.py         (new)
├── test_duckdb_panel.py       (new)
└── test_toolbar_integration.py (new)
```

---

## Testing

| Test | Type | Asserts |
|------|------|---------|
| `test_logging_config::test_dev_uses_rich_handler` | unit | `DEBUG=1` → root handler is `RichHandler` |
| `::test_prod_uses_json` | unit | no `DEBUG` → `_JSONFormatter`, output parses as JSON |
| `::test_idempotent` | unit | calling twice does not duplicate handlers |
| `::test_slug_from_dotted_name` | unit | `_derive_slug("services.scheduler.__main__") == "scheduler"` |
| `::test_slug_from_collector_module` | unit | `_derive_slug("services.corporate_memory.collector") == "corporate_memory.collector"` |
| `::test_slug_explicit_override` | unit | `_derive_slug("app") == "app"` |
| `::test_slug_fallback_via_file` | unit | `service is None`, frame has `__file__` → slug derived from path |
| `::test_request_id_in_json_output` | unit | with contextvar set → JSON line includes `"request_id"` |
| `test_request_id::test_assigns_id_when_missing` | unit | header missing → 12-char id assigned |
| `::test_passes_through_provided_id` | unit | `X-Request-ID` preserved |
| `::test_propagates_to_response_header` | unit | response has `X-Request-ID` |
| `test_duckdb_panel::test_instrumented_records_query` | unit | `con.execute("SELECT 1")` adds entry with timing |
| `::test_records_error` | unit | failing query records `error` field |
| `::test_db_tag_preserved` | unit | system vs analytics tagged correctly |
| `::test_no_op_outside_request` | unit | `record_query` when store is None doesn't raise |
| `::test_passthrough_attributes` | unit | `con.fetchall()` etc. delegated to wrapped connection |
| `test_toolbar_integration::test_html_response_has_toolbar_tab` | integration | `DEBUG=1` HTML response contains toolbar div |
| `::test_no_toolbar_when_debug_off` | integration | no DEBUG → no toolbar markup |
| `::test_duckdb_panel_renders_queries` | integration | DuckDB-touching endpoint → panel JSON has 1+ queries |
| `::test_logging_panel_captures_records` | integration | `logger.info("X")` during request → toolbar logging panel contains `"X"` |

Coverage target ≥ 80 % for `app.logging_config`, `app.debug.*`, `app.middleware.request_id`.

Mocking: use real in-memory DuckDB connections (`duckdb.connect(":memory:")`) — fast, catches API drift. `monkeypatch.setenv("DEBUG", "1")` per test.

---

## Rollout phases

| Phase | Deliverable | Verifies |
|-------|-------------|----------|
| **P1** | `app/logging_config.py` + tests | `pytest tests/test_logging_config.py` green |
| **P2** | `RequestIdMiddleware` + tests, wired in `app/main.py` | response has `X-Request-ID`; JSON logs include same id |
| **P3** | Migrate `app/main.py` + 5 service entrypoints | each service starts; no duplicate log lines; dev colors render |
| **P4** | Migrate `connectors/`, `src/`, `scripts/` (~14 files) | full suite passes; only test files keep `basicConfig` |
| **P5** | Add `fastapi-debug-toolbar` dev dep + wire middleware behind `DEBUG=1` (no `DuckDBPanel` yet) | with `DEBUG=1`, visit `/admin/access` → toolbar tab visible |
| **P6** | `DuckDBPanel` + `InstrumentedConnection` + integrate in `src/db.py` | DuckDB panel populated on any DB-touching request |
| **P7** | `docs/development.md` + CHANGELOG + `.env.template` `DEBUG=1` line | onboarding doc covers usage |

Each phase = one commit. P5 is opt-in (DEBUG flag) so prod is unaffected throughout.

---

## Production safeguards

- Toolbar code path **never imported** in prod: import is inside `if DEBUG:` block.
- `pyproject.toml` puts `fastapi-debug-toolbar` in `[project.optional-dependencies].dev` only — production image build (`uv pip install .` without `[dev]`) does not ship it.
- `_maybe_instrument(con, ...)` is a no-op in prod: returns the raw `duckdb.Connection` unchanged. Zero per-query overhead.
- JSON log format unchanged from existing prod expectations. New optional fields: `service`, `request_id`. Both omitted when not set.
- `DEBUG` env var documented in `.env.template`; default unset = production behavior.

---

## Failure-mode self-check

| Mode | Mitigation |
|------|------------|
| Hallucinated actions | Plan execution reads `src/db.py` to find actual connect helper names before wrapping — flagged in §"src/db.py integration" |
| Scope creep | Only entrypoint files + 1 broken module-level `basicConfig` in `app/api/sync.py:104`. No drive-by refactors. |
| Cascading errors | `InstrumentedConnection.execute` re-raises; `record_query` runs in `finally`, never swallows. |
| Context loss | `setup_logging` idempotent + `_CONFIGURED` sentinel; uvicorn loggers re-routed via `propagate=True` to root |
| Tool misuse | Native Edit/Write for code, pytest for tests, browser/playwright-cli for UI verification in P5/P6 |

---

## Open questions

None at design time. Implementation will resolve:

- Exact function names in `src/db.py` to wrap (read during P6).
- Whether `duckdb` `executemany` / `sql` / `query` helpers also need instrumentation (audit during P6).
- Whether the toolbar's built-in `LoggingPanel` plays nicely with our `RichHandler` (verify in P5; fall back to `StreamHandler` if conflict).

---

## References

- [fastapi-debug-toolbar docs](https://fastapi-debug-toolbar.domake.io/)
- [GitHub: mongkok/fastapi-debug-toolbar](https://github.com/mongkok/fastapi-debug-toolbar) — v0.6.3 (2024-05), 167 stars, low velocity but stable
- [PyPI: fastapi-debug-toolbar 0.6.3](https://pypi.org/project/fastapi-debug-toolbar/)
- [Rich logging docs](https://rich.readthedocs.io/en/stable/logging.html)
- Gemini 2.5 Flash adversarial review run 2026-04-29 (validated `setup_logging` design; recommended explicit `__name__` over frame-walk auto-derive)
