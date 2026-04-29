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


_request_store: contextvars.ContextVar[list[Query] | None] = contextvars.ContextVar("duckdb_panel_store", default=None)


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
