"""Mock classes for unit and integration tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock


class MockLLMProvider:
    """Mock LLM provider that returns pre-configured responses.

    Usage::

        provider = MockLLMProvider(responses=[{"key": "value"}, {"other": "result"}])
        result = provider.extract_json("some prompt")  # returns {"key": "value"}
        result = provider.extract_json("another prompt")  # returns {"other": "result"}
        # After exhausting responses, returns last item repeatedly.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses: list[Any] = responses if responses is not None else [{}]
        self._call_count = 0

    def extract_json(self, *args, **kwargs) -> Any:
        """Return the next configured response, cycling at the last one."""
        idx = min(self._call_count, len(self._responses) - 1)
        result = self._responses[idx]
        self._call_count += 1
        return result

    def complete(self, *args, **kwargs) -> str:
        """Return the next configured response as a JSON string."""
        return json.dumps(self.extract_json(*args, **kwargs))

    @property
    def call_count(self) -> int:
        """Number of times extract_json / complete was called."""
        return self._call_count

    def reset(self) -> None:
        """Reset the call counter."""
        self._call_count = 0


class MockHTTPResponse:
    """Mock httpx-compatible HTTP response.

    Mimics the interface used by httpx.Response / requests.Response so that
    code that calls `.json()`, `.text`, `.status_code`, and
    `.raise_for_status()` works without a real HTTP server.

    Usage::

        response = MockHTTPResponse(200, json_data={"id": 1}, text='{"id": 1}')
        response.json()           # {"id": 1}
        response.raise_for_status()  # no-op for 2xx
        response.status_code      # 200

        error = MockHTTPResponse(404, json_data={"detail": "not found"})
        error.raise_for_status()  # raises RuntimeError
    """

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> Any:
        """Return the configured JSON data."""
        if self._json_data is None:
            raise ValueError("No JSON data configured for this MockHTTPResponse")
        return self._json_data

    def raise_for_status(self) -> None:
        """Raise RuntimeError for 4xx/5xx status codes (mirrors httpx behaviour)."""
        if self.status_code >= 400:
            raise RuntimeError(
                f"HTTP error {self.status_code}: {self.text}"
            )


def mock_duckdb_connection(tables: dict[str, list[dict]] | None = None) -> MagicMock:
    """Return a MagicMock that mimics a DuckDB connection.

    Args:
        tables: Mapping of SQL pattern → list-of-tuples results that
            ``fetchall()`` should return when the executed SQL contains the
            key as a substring.  ``fetchone()`` returns the first tuple (or
            None).  If no key matches, fetchall returns [] and fetchone None.

    The returned mock exposes:
    - ``.execute(sql, params=None)`` — returns self (chainable)
    - ``.fetchall()`` — returns matching rows or []
    - ``.fetchone()`` — returns first matching row or None
    - ``.close()`` — no-op

    Example::

        conn = mock_duckdb_connection({"SELECT * FROM users": [("alice", "admin")]})
        conn.execute("SELECT * FROM users").fetchall()  # [("alice", "admin")]
    """
    tables = tables or {}

    class _MockConn:
        def __init__(self) -> None:
            self._last_sql: str = ""
            self._last_rows: list = []

        def execute(self, sql: str, params: Any = None) -> "_MockConn":
            self._last_sql = sql
            self._last_rows = []
            for pattern, rows in tables.items():
                if pattern in sql:
                    self._last_rows = list(rows)
                    break
            return self

        def fetchall(self) -> list:
            return self._last_rows

        def fetchone(self) -> Any:
            return self._last_rows[0] if self._last_rows else None

        def close(self) -> None:
            pass

    return _MockConn()
