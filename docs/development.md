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

Set `DEBUG=1` to mount `fastapi-debug-toolbar`. Visit any HTML page (e.g.
`/setup`, `/login`, `/dashboard`, `/admin/access`) and click the small toolbar tab
on the right edge.

Panels:

- **Headers** — request/response headers
- **Routes** — registered FastAPI routes
- **Settings** — Pydantic / instance-config values
- **Versions** — installed package versions
- **Timer** — request duration
- **Logging** — log records emitted during the request
- **DuckDB** — every `con.execute(sql, params)` from `src/db.py`, tagged
  by `system` / `analytics` / `analytics_ro`, with timing and row count

Note: Profiling panel (pyinstrument) is intentionally omitted because it
clashes with uvicorn's async task context. Re-enable in `app/main.py` if
you set `PROFILER_OPTIONS={"async_mode": "disabled"}` or swap profilers.

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

## Bot logs

The telegram bot writes to stdout (captured by Docker). Read its logs with:

```bash
docker compose logs -f notify-bot
```

(Previously bots wrote to `/data/notifications/bot.log` via a FileHandler. That
file is no longer produced; use `docker logs` for runtime tail.)
