# Development guide

## Logging

All processes (FastAPI app, scheduler, telegram_bot, ws_gateway, corporate_memory,
session_collector, verification_detector, CLI scripts) use
`app.logging_config.setup_logging` to configure the root logger. Each entrypoint
calls it once:

```python
from app.logging_config import setup_logging
setup_logging(__name__)
```

Library modules just do `logger = logging.getLogger(__name__)` — they NEVER
configure root.

| Env | Handler | Format |
|-----|---------|--------|
| `DEBUG=1` | `rich.logging.RichHandler` | colored, clickable file:line, pretty tracebacks |
| (default) | stdlib `StreamHandler` | JSON to stderr (`ts`, `lvl`, `logger`, `service`, `msg`, optional `request_id`) |

`LOG_LEVEL` overrides the level (default `DEBUG` when `DEBUG=1`, else `INFO`).

`DEBUG` and `LOG_LEVEL` are read at process start by `app/main.py` to decide
whether to mount the toolbar middleware and configure logging handlers. The
DuckDB connection wrapper in `src/db.py` reads `DEBUG` at call time, so tests
can toggle it via `monkeypatch.setenv` — but the toolbar itself only mounts
on initial app construction.

## Request correlation

`RequestIdMiddleware` is mounted unconditionally on the FastAPI app. It assigns
or propagates `X-Request-ID` and exposes it via the `request_id_var`
ContextVar so the JSON formatter and the debug-toolbar logging panel see the
same id.

## Debug toolbar

### What it is

Per-request HTML overlay that surfaces what the server did to produce the
page in front of you — headers, routes matched, every DuckDB query, log
records, timing — without leaving the browser. Powered by
[`fastapi-debug-toolbar`](https://github.com/mongkok/fastapi-debug-toolbar)
plus a custom `DuckDBPanel` (see `app/debug_panels/duckdb_panel.py`) that
intercepts every `con.execute(sql, params)` from `src/db.py`.

The toolbar is mounted innermost so it sees raw HTML before
`_SelectiveGZipMiddleware` compresses the body, and gated by `DEBUG=1` —
**never imported in production**. The dev dependency group
(`uv pip install ".[dev]"`) is the only place `fastapi-debug-toolbar` lives.

### Enabling it

```bash
DEBUG=1 uv run uvicorn app.main:app --reload --port 8011
```

Or persist in `.env` at repo root (auto-loaded by uvicorn):

```env
DEBUG=1
LOG_LEVEL=DEBUG
SESSION_SECRET=<32+ chars>
```

Visit any HTML page (`/setup`, `/login`, `/dashboard`, `/admin/access`) →
small collapsed handle on the right edge of the viewport → click to expand.

### Panels

| Panel | Shows |
|-------|-------|
| **Headers** | Request + response headers (incl. `x-request-id`) |
| **Routes** | All registered FastAPI routes; matched route highlighted |
| **Settings** | Pydantic settings, `instance_config` values |
| **Versions** | Installed package versions (Python, FastAPI, deps) |
| **Timer** | Wall-clock + CPU time for the request |
| **Logging** | Every `logger.*` call during the request, with rid prefix |
| **DuckDB** | Every SQL via `src/db.py` — DB tag (`system`/`analytics`/`analytics_ro`), parameters, duration, row count |

Profiling panel (pyinstrument) intentionally omitted — clashes with
uvicorn's async task context. Re-enable in `app/main.py` if you set
`PROFILER_OPTIONS={"async_mode": "disabled"}` or swap profilers.

JSON-only endpoints (Swagger UI at `/docs`) replay the most recent
request's panels via a cookie mechanism — open `/docs`, fire a request,
then navigate to any HTML page to inspect it.

### When to reach for it

| Symptom | Panel |
|---------|-------|
| "Why is this page slow?" | Timer + DuckDB (look for N+1 or unindexed scans) |
| "Which route handler ran?" | Routes |
| "Which user / session did the server see?" | Headers + Logging |
| "Why is this query returning N rows?" | DuckDB (full SQL + params + tag) |
| "Did this log line fire?" | Logging |
| "Is rid propagating end-to-end?" | Headers (`x-request-id`) + Logging (rid prefix on every line) |

### Forcing an error page (for testing)

Two dev-only routes (mounted only when `DEBUG=1`, otherwise 404):

| URL | Behavior |
|-----|----------|
| `/_debug/throw/http/{code}` | Raises `HTTPException(code)` → goes through `StarletteHTTPException` handler → renders `error.html` for any code (`/_debug/throw/http/404`, `/_debug/throw/http/418`, `/_debug/throw/http/500`, …). Matched route, so the toolbar mounts. |
| `/_debug/throw/exc` | Raises unhandled `KeyError` → goes through `_unhandled_exception_handler` → renders the **5xx path**, including the `<details>Traceback</details>` block (DEBUG-only). **Toolbar NOT injected on this page** — see note below. |

Both echo the active `x-request-id` in response header and `Reference: <rid>`
on the rendered error page.

**Toolbar gap on unhandled exceptions.** `fastapi-debug-toolbar` uses
`BaseHTTPMiddleware`, which composes poorly with Starlette's
`ServerErrorMiddleware`: when the route raises a bare `Exception` (not
`HTTPException`), the exception propagates past the toolbar's
`call_next` boundary before any response is sent, so the toolbar dispatch
never sees a response body to inject into. The 500 page is produced
*outside* the toolbar. Use `/_debug/throw/http/500` instead to eyeball
the 500 chrome WITH toolbar panels. Use `/_debug/throw/exc` only to
verify the unhandled-exception code path itself (traceback `<details>`
block, JSON 500 body).

### Source

- Mount + show-callback: `app/main.py` (search for `DebugToolbarMiddleware`)
- DuckDB panel: `app/debug_panels/duckdb_panel.py`
- Dev throw routes: `app/web/router.py` (`/_debug/throw/...`)

## Running locally

```bash
uv pip install ".[dev]"
DEBUG=1 LOCAL_DEV_MODE=1 uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/dashboard`.

## Bot logs

The telegram bot writes to stdout (captured by Docker). Read its logs with:

```bash
docker compose logs -f notify-bot
```

(Previously bots wrote to `/data/notifications/bot.log` via a FileHandler. That
file is no longer produced; use `docker logs` for runtime tail.)
