"""DuckDB session facade for the Keboola Storage API extension.

Parallel of `connectors/bigquery/access.py:BqAccess`. The materialized
Keboola SQL path needs a one-shot DuckDB connection with the Keboola
extension installed, loaded, and ATTACHed; this facade encapsulates
that wiring so `_run_materialized_pass` doesn't need to know the
extension name, the ATTACH alias, or how the token gets quoted into
the URL literal.
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator

import duckdb


class KeboolaAccess:
    """Lazy DuckDB session manager for the Keboola Storage API extension.

    Single-use — call `.duckdb_session()` as a context manager once per
    materialized job.
    """

    def __init__(self, *, url: str, token: str) -> None:
        if not url or not token:
            raise ValueError("KeboolaAccess requires url and token")
        self._url = url
        self._token = token

    @contextmanager
    def duckdb_session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("INSTALL keboola FROM community")
            conn.execute("LOAD keboola")
            escaped_token = self._token.replace("'", "''")
            conn.execute(
                f"ATTACH '{self._url}' AS kbc "
                f"(TYPE keboola, TOKEN '{escaped_token}')"
            )
            yield conn
        finally:
            conn.close()
