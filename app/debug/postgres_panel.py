"""Per-request Postgres query capture for the dev debug toolbar.

The Postgres equivalent of ``app/debug/duckdb_panel.py``. Where DuckDB is
captured by wrapping the connection (``InstrumentedConnection``), Postgres goes
through SQLAlchemy, so we hook the engine's ``before_cursor_execute`` /
``after_cursor_execute`` / ``handle_error`` events and record each statement
into a contextvar-scoped store. The toolbar's ``PostgresPanel`` reads that store
at response time.

When the contextvar is unset (outside a debug request, or in prod), recording
is a no-op — the event listeners return immediately, so there is no measurable
overhead on the normal request path. ``instrument_engine()`` is called once from
``src/db_pg.py::get_engine()`` and is idempotent.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Query:
    db: str
    sql: str
    params: Any
    ms: float
    rows: int | None
    error: str | None = None


# Per-request query buffer. Mirrors duckdb_panel: request-scoped, unbounded
# (dev-only), garbage-collected with the asyncio Task that owns the contextvar.
_request_store: contextvars.ContextVar[list[Query] | None] = contextvars.ContextVar(
    "postgres_panel_store", default=None
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


def instrument_engine(engine: Any) -> None:
    """Attach query-capture listeners to a SQLAlchemy engine (idempotent).

    Listeners short-circuit when no request store is active, so this is safe to
    call unconditionally — it is a no-op on every non-debug request and in prod.
    """
    if getattr(engine, "_agnes_pg_instrumented", False):
        return
    try:
        from sqlalchemy import event
    except Exception:
        return

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if _request_store.get() is None:
            return
        context._agnes_pg_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if _request_store.get() is None:
            return
        started = getattr(context, "_agnes_pg_start", None)
        if started is None:
            return
        rows: int | None = None
        try:
            rows = cursor.rowcount
        except Exception:
            pass
        record_query("postgres", statement, parameters, started, rows)

    @event.listens_for(engine, "handle_error")
    def _error(exc_ctx):  # noqa: ANN001
        if _request_store.get() is None:
            return
        ec = exc_ctx.execution_context
        started = getattr(ec, "_agnes_pg_start", None) if ec is not None else None
        if started is None:
            started = time.perf_counter()
        record_query(
            "postgres",
            exc_ctx.statement or "",
            exc_ctx.parameters,
            started,
            None,
            repr(exc_ctx.original_exception),
        )

    engine._agnes_pg_instrumented = True


# Toolbar Panel — only available when fastapi-debug-toolbar is installed.
# The try/except keeps this module import-safe everywhere; the listeners above
# do not depend on the toolbar.
try:
    from fastapi import Request, Response
    from debug_toolbar.panels import Panel

    class PostgresPanel(Panel):
        """fastapi-debug-toolbar panel rendering captured Postgres queries."""

        title = "Postgres"
        template = "panels/postgres.html"

        @property
        def nav_subtitle(self) -> str:
            stats = self.get_stats()
            queries = stats.get("queries", []) if stats else []
            total_ms = stats.get("total_ms", 0.0) if stats else 0.0
            return f"{len(queries)} queries · {total_ms:.1f} ms"

        async def process_request(self, request: Request) -> Response:
            _request_store.set([])
            return await super().process_request(request)

        async def generate_stats(self, request: Request, response: Response) -> dict:
            store = _request_store.get() or []
            queries = [q.__dict__ for q in store]
            total_ms = sum(q.ms for q in store)
            return {
                "queries": queries,
                "total_ms": total_ms,
            }

        async def generate_server_timing(
            self, request: Request, response: Response
        ) -> list[tuple[str, str, float]]:
            store = _request_store.get() or []
            return [("Postgres", "Postgres queries", sum(q.ms for q in store))]

except ImportError:
    PostgresPanel = None  # type: ignore[assignment, misc]
