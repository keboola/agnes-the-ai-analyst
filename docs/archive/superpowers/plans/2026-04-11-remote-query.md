# Remote Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix BigQuery extension re-attach so remote views work, then add a two-phase query engine that JOINs local Parquet data with on-demand BigQuery subquery results.

**Architecture:** Part 1 patches `get_analytics_db_readonly()` to re-load extensions from `_remote_attach` tables. Part 2 adds `RemoteQueryEngine` that wraps BQ client with safety limits (COUNT pre-check, memory estimation), registers Arrow results in DuckDB, then executes the final SQL. Exposed via `da query --register-bq` CLI and `POST /api/query/hybrid` API.

**Tech Stack:** DuckDB, google-cloud-bigquery, PyArrow, FastAPI, Typer

**Spec:** `docs/superpowers/specs/2026-04-11-remote-query-design.md`

---

### Task 1: Fix Extension Re-attach in `get_analytics_db_readonly()`

**Files:**
- Modify: `src/db.py:253-282` (get_analytics_db_readonly)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_db.py`:

```python
class TestExtensionReattach:
    def test_reads_remote_attach_table(self, tmp_path, monkeypatch):
        """Verify get_analytics_db_readonly() attempts to load extensions from _remote_attach."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb

        # Create analytics DB
        analytics_dir = tmp_path / "analytics"
        analytics_dir.mkdir()
        conn = duckdb.connect(str(analytics_dir / "server.duckdb"))
        conn.close()

        # Create an extract.duckdb with a _remote_attach table
        ext_dir = tmp_path / "extracts" / "testbq"
        ext_dir.mkdir(parents=True)
        ext_conn = duckdb.connect(str(ext_dir / "extract.duckdb"))
        ext_conn.execute("""
            CREATE TABLE _remote_attach (
                alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
            )
        """)
        ext_conn.execute(
            "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test', '')"
        )
        ext_conn.close()

        from src.db import get_analytics_db_readonly
        # This won't actually load bigquery (not installed in test env),
        # but should not crash — just log a warning
        analytics = get_analytics_db_readonly()
        try:
            # Connection should be usable even if extension load failed
            result = analytics.execute("SELECT 1").fetchone()
            assert result[0] == 1
        finally:
            analytics.close()

    def test_skips_missing_remote_attach(self, tmp_path, monkeypatch):
        """Extract without _remote_attach should not cause errors."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb

        analytics_dir = tmp_path / "analytics"
        analytics_dir.mkdir()
        conn = duckdb.connect(str(analytics_dir / "server.duckdb"))
        conn.close()

        ext_dir = tmp_path / "extracts" / "plain"
        ext_dir.mkdir(parents=True)
        ext_conn = duckdb.connect(str(ext_dir / "extract.duckdb"))
        ext_conn.execute("CREATE TABLE _meta (name VARCHAR)")
        ext_conn.close()

        from src.db import get_analytics_db_readonly
        analytics = get_analytics_db_readonly()
        try:
            result = analytics.execute("SELECT 1").fetchone()
            assert result[0] == 1
        finally:
            analytics.close()
```

- [ ] **Step 2: Run test to verify it fails (or passes — these are resilience tests)**

Run: `pytest tests/test_db.py::TestExtensionReattach -v`
Expected: Both tests likely PASS already (graceful failures). That's fine — the real value is ensuring the re-attach code doesn't break anything.

- [ ] **Step 3: Implement extension re-attach**

In `src/db.py`, modify `get_analytics_db_readonly()`. After the existing ATTACH loop (line ~279), before the `return conn` (line ~282), add:

```python
    # Re-attach remote extensions (BigQuery, Keboola, etc.)
    if extracts_dir.exists():
        _reattach_remote_extensions(conn, extracts_dir)
```

Add this helper function before `get_analytics_db_readonly()`:

```python
def _reattach_remote_extensions(
    conn: duckdb.DuckDBPyConnection, extracts_dir: Path
) -> None:
    """Re-load extensions from _remote_attach tables in extract.duckdb files."""
    already_attached = set()
    try:
        already_attached = {
            r[0] for r in conn.execute(
                "SELECT database_name FROM duckdb_databases()"
            ).fetchall()
        }
    except Exception:
        pass

    for ext_dir in sorted(extracts_dir.iterdir()):
        if not ext_dir.is_dir() or not _SAFE_IDENTIFIER.match(ext_dir.name):
            continue
        # Check if this extract has a _remote_attach table
        try:
            has_table = conn.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema='{ext_dir.name}' AND table_name='_remote_attach'"
            ).fetchall()
            if not has_table:
                continue
        except Exception:
            continue

        try:
            rows = conn.execute(
                f"SELECT alias, extension, url, token_env FROM {ext_dir.name}._remote_attach"
            ).fetchall()
        except Exception:
            continue

        for alias, extension, url, token_env in rows:
            if alias in already_attached:
                continue
            if not _SAFE_IDENTIFIER.match(alias) or not _SAFE_IDENTIFIER.match(extension):
                continue

            token = os.environ.get(token_env, "") if token_env else ""

            try:
                conn.execute(f"LOAD {extension};")
                if token:
                    escaped_token = token.replace("'", "''")
                    conn.execute(
                        f"ATTACH '{url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
                else:
                    conn.execute(
                        f"ATTACH '{url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
                already_attached.add(alias)
                logger.info("Re-attached remote source %s via %s", alias, extension)
            except Exception as e:
                logger.debug("Could not re-attach %s: %s", alias, e)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "fix: re-attach remote extensions in get_analytics_db_readonly()"
```

---

### Task 2: RemoteQueryEngine Core

**Files:**
- Create: `src/remote_query.py`
- Test: `tests/test_remote_query.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_remote_query.py`:

```python
"""Tests for RemoteQueryEngine."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest


@pytest.fixture
def analytics_conn(tmp_path):
    """DuckDB connection with a sample local view."""
    conn = duckdb.connect()
    conn.execute("CREATE TABLE orders (id INT, date DATE, amount DECIMAL(10,2))")
    conn.execute("INSERT INTO orders VALUES (1, '2026-01-01', 100.0), (2, '2026-01-15', 200.0)")
    yield conn
    conn.close()


def _mock_bq_arrow_table():
    """Create a mock Arrow table for BQ results."""
    import pyarrow as pa
    return pa.table({
        "date": ["2026-01-01", "2026-01-15"],
        "pageviews": [1000, 2000],
    })


class TestRemoteQueryEngineRegister:
    def test_register_bq_success(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine

        mock_arrow = _mock_bq_arrow_table()
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = mock_arrow
        mock_client = MagicMock()
        mock_client.query.return_value = mock_job
        # COUNT pre-check
        mock_count_job = MagicMock()
        mock_count_result = MagicMock()
        mock_count_result.fetchone.return_value = (2,)
        mock_count_job.result.return_value = mock_count_result
        mock_client.query.side_effect = [mock_count_job, mock_job]

        engine = RemoteQueryEngine(analytics_conn, _bq_client_factory=lambda: mock_client)
        stats = engine.register_bq("traffic", "SELECT date, pageviews FROM dataset.web")

        assert stats["alias"] == "traffic"
        assert stats["rows"] == 2
        # Verify the view is usable
        result = analytics_conn.execute("SELECT * FROM traffic").fetchall()
        assert len(result) == 2

    def test_register_bq_row_limit_exceeded(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine, RemoteQueryError

        mock_client = MagicMock()
        mock_count_job = MagicMock()
        mock_count_result = MagicMock()
        mock_count_result.fetchone.return_value = (999999,)
        mock_count_job.result.return_value = mock_count_result
        mock_client.query.return_value = mock_count_job

        engine = RemoteQueryEngine(
            analytics_conn,
            _bq_client_factory=lambda: mock_client,
            max_bq_registration_rows=1000,
        )
        with pytest.raises(RemoteQueryError, match="row_limit"):
            engine.register_bq("big", "SELECT * FROM huge_table")

    def test_register_bq_missing_package(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine, RemoteQueryError

        engine = RemoteQueryEngine(
            analytics_conn,
            _bq_client_factory=None,  # Will try real import
        )
        with patch.dict("sys.modules", {"google.cloud.bigquery": None}):
            with pytest.raises(RemoteQueryError, match="bq_error"):
                engine.register_bq("x", "SELECT 1")


class TestRemoteQueryEngineExecute:
    def test_execute_local_only(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine
        engine = RemoteQueryEngine(analytics_conn)
        result = engine.execute("SELECT id, amount FROM orders ORDER BY id")
        assert result["columns"] == ["id", "amount"]
        assert len(result["rows"]) == 2
        assert result["row_count"] == 2
        assert result["truncated"] is False

    def test_execute_with_registered_bq(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine
        import pyarrow as pa

        # Manually register an Arrow table (simulating BQ result)
        traffic = pa.table({"date": ["2026-01-01", "2026-01-15"], "views": [100, 200]})
        analytics_conn.register("traffic", traffic)

        engine = RemoteQueryEngine(analytics_conn)
        result = engine.execute(
            "SELECT o.id, t.views FROM orders o JOIN traffic t ON CAST(o.date AS VARCHAR) = t.date ORDER BY o.id"
        )
        assert len(result["rows"]) == 2
        assert result["columns"] == ["id", "views"]

    def test_execute_respects_max_result_rows(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine
        engine = RemoteQueryEngine(analytics_conn, max_result_rows=1)
        result = engine.execute("SELECT * FROM orders")
        assert len(result["rows"]) == 1
        assert result["truncated"] is True

    def test_execute_invalid_sql(self, analytics_conn):
        from src.remote_query import RemoteQueryEngine, RemoteQueryError
        engine = RemoteQueryEngine(analytics_conn)
        with pytest.raises(RemoteQueryError, match="query_error"):
            engine.execute("DROP TABLE orders")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_remote_query.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.remote_query'`

- [ ] **Step 3: Implement RemoteQueryEngine**

Create `src/remote_query.py`:

```python
"""Two-phase remote query engine.

Phase 1: Execute BigQuery subqueries, register results as in-memory Arrow tables.
Phase 2: Execute DuckDB query joining local Parquet views with BQ Arrow tables.
"""

import logging
import os
from typing import Any, Callable, Dict, List, Optional

import duckdb

logger = logging.getLogger(__name__)

# SQL blocklist — reused from app/api/query.py
_BLOCKED_KEYWORDS = [
    "drop ", "delete ", "insert ", "update ", "alter ", "create ",
    "copy ", "attach ", "detach ", "load ", "install ",
    "export ", "import ", "pragma ", "call ",
    "read_csv", "read_json", "read_parquet", "read_text",
    "write_csv", "write_parquet", "read_blob", "read_ndjson",
    "parquet_scan", "parquet_metadata", "parquet_schema",
    "json_scan", "csv_scan",
    "query_table", "iceberg_scan", "delta_scan",
    "glob(", "list_files",
    "'/", '"/', 'http://', 'https://', 's3://', 'gcs://',
    "information_schema", "duckdb_tables", "duckdb_columns",
    "duckdb_databases", "duckdb_settings", "duckdb_functions",
    "duckdb_views", "duckdb_indexes", "duckdb_schemas",
    "pragma_table_info", "pragma_storage_info",
    "'../", '"../',
    ";",
]


class RemoteQueryError(Exception):
    """Structured error for remote query failures."""

    def __init__(self, message: str, error_type: str, details: Optional[dict] = None):
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


class RemoteQueryEngine:
    """Two-phase query engine: BQ subqueries + DuckDB final query."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        _bq_client_factory: Optional[Callable] = None,
        max_bq_registration_rows: int = 500_000,
        max_memory_mb: float = 2048.0,
        max_result_rows: int = 100_000,
        timeout_seconds: int = 300,
    ):
        self.conn = conn
        self._bq_client_factory = _bq_client_factory
        self.max_bq_registration_rows = max_bq_registration_rows
        self.max_memory_mb = max_memory_mb
        self.max_result_rows = max_result_rows
        self.timeout_seconds = timeout_seconds
        self._bq_stats: Dict[str, dict] = {}

    def register_bq(self, alias: str, bq_sql: str) -> dict:
        """Execute BQ subquery, register result as in-memory DuckDB view.

        Returns dict with {alias, rows, columns, memory_mb}.
        Raises RemoteQueryError on failure.
        """
        _validate_sql(bq_sql)

        client = self._get_bq_client()

        # Phase 1a: COUNT(*) pre-check
        count_sql = f"SELECT COUNT(*) FROM ({bq_sql})"
        try:
            count_job = client.query(count_sql)
            row_count = count_job.result().fetchone()[0]
        except Exception as e:
            raise RemoteQueryError(
                f"BQ COUNT pre-check failed for '{alias}': {e}",
                error_type="bq_error",
                details={"alias": alias},
            )

        if row_count > self.max_bq_registration_rows:
            raise RemoteQueryError(
                f"BQ query '{alias}' returns {row_count:,} rows "
                f"(limit: {self.max_bq_registration_rows:,})",
                error_type="row_limit",
                details={"alias": alias, "rows": row_count, "limit": self.max_bq_registration_rows},
            )

        # Phase 1b: Execute and register
        try:
            job = client.query(bq_sql)
            try:
                arrow_table = job.to_arrow()
            except Exception:
                arrow_table = job.to_arrow(create_bqstorage_client=False)
        except Exception as e:
            raise RemoteQueryError(
                f"BQ query failed for '{alias}': {e}",
                error_type="bq_error",
                details={"alias": alias},
            )

        # Memory check (actual, not estimated)
        memory_mb = arrow_table.nbytes / (1024 * 1024)
        if memory_mb > self.max_memory_mb:
            raise RemoteQueryError(
                f"BQ result '{alias}' uses {memory_mb:.1f} MB "
                f"(limit: {self.max_memory_mb:.0f} MB)",
                error_type="memory_limit",
                details={"alias": alias, "memory_mb": memory_mb, "limit": self.max_memory_mb},
            )

        self.conn.register(alias, arrow_table)
        stats = {
            "alias": alias,
            "rows": arrow_table.num_rows,
            "columns": arrow_table.num_columns,
            "memory_mb": round(memory_mb, 3),
        }
        self._bq_stats[alias] = stats
        logger.info("Registered BQ view '%s': %d rows, %.1f MB", alias, arrow_table.num_rows, memory_mb)
        return stats

    def execute(self, sql: str) -> dict:
        """Execute final DuckDB query. Returns {columns, rows, row_count, truncated, bq_stats}."""
        _validate_sql(sql)

        try:
            result = self.conn.execute(sql).fetchmany(self.max_result_rows + 1)
            columns = [desc[0] for desc in self.conn.description] if self.conn.description else []
        except Exception as e:
            raise RemoteQueryError(
                f"Query execution failed: {e}",
                error_type="query_error",
            )

        truncated = len(result) > self.max_result_rows
        rows = result[:self.max_result_rows]

        # Serialize non-standard types
        serializable_rows = []
        for row in rows:
            serializable_rows.append([
                str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
                for v in row
            ])

        return {
            "columns": columns,
            "rows": serializable_rows,
            "row_count": len(serializable_rows),
            "truncated": truncated,
            "bq_stats": dict(self._bq_stats),
        }

    def _get_bq_client(self):
        """Get BigQuery client, using factory or default."""
        if self._bq_client_factory:
            return self._bq_client_factory()
        try:
            from scripts.duckdb_manager import _create_bq_client
            project = os.environ.get("BIGQUERY_PROJECT")
            if not project:
                raise RemoteQueryError(
                    "BIGQUERY_PROJECT env var not set",
                    error_type="bq_error",
                )
            return _create_bq_client(project)
        except ImportError:
            raise RemoteQueryError(
                "google-cloud-bigquery is not installed. "
                "Install with: pip install google-cloud-bigquery",
                error_type="bq_error",
            )


def _validate_sql(sql: str) -> None:
    """Validate SQL against blocklist. Raises RemoteQueryError."""
    sql_lower = sql.strip().lower()
    for keyword in _BLOCKED_KEYWORDS:
        if keyword in sql_lower:
            raise RemoteQueryError(
                f"Blocked SQL keyword: {keyword.strip()}",
                error_type="query_error",
            )
    if not sql_lower.startswith("select ") and not sql_lower.startswith("with "):
        raise RemoteQueryError(
            "Query must start with SELECT or WITH",
            error_type="query_error",
        )


def load_config() -> dict:
    """Load remote_query config from instance.yaml."""
    try:
        from app.instance_config import get_value
        return get_value("remote_query") or {}
    except Exception:
        return {}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_remote_query.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/remote_query.py tests/test_remote_query.py
git commit -m "feat: add RemoteQueryEngine with BQ registration and safety limits"
```

---

### Task 3: CLI `da query --register-bq`

**Files:**
- Modify: `cli/commands/query.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_cli.py`:

```python
class TestQueryHybrid:
    def test_register_bq_flag_help(self):
        result = runner.invoke(app, ["query", "--help"])
        assert result.exit_code == 0
        assert "register-bq" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::TestQueryHybrid -v`
Expected: FAIL — `register-bq` not in help output

- [ ] **Step 3: Implement CLI changes**

Replace `cli/commands/query.py` with:

```python
"""Query commands — da query."""

import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer


def query_command(
    sql: Optional[str] = typer.Argument(None, help="SQL query to execute"),
    sql_opt: Optional[str] = typer.Option(None, "--sql", help="SQL query (alternative to positional)"),
    remote: bool = typer.Option(False, "--remote", help="Execute on server instead of locally"),
    register_bq: Optional[List[str]] = typer.Option(None, "--register-bq", help="Register BQ subquery: alias=SQL"),
    stdin: bool = typer.Option(False, "--stdin", help="Read query spec from stdin (JSON)"),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    limit: int = typer.Option(1000, "--limit", help="Max rows to return"),
):
    """Execute SQL query against DuckDB. Supports hybrid BQ+local queries."""
    # Resolve SQL from positional, --sql, or --stdin
    if stdin:
        spec = json.loads(sys.stdin.read())
        final_sql = spec.get("sql", "")
        register_bq = [f"{k}={v}" for k, v in spec.get("register_bq", {}).items()]
    else:
        final_sql = sql or sql_opt
        if not final_sql:
            typer.echo("Error: provide SQL as argument, --sql, or --stdin", err=True)
            raise typer.Exit(1)

    if register_bq:
        _query_hybrid(final_sql, register_bq, fmt, limit)
    elif remote:
        _query_remote(final_sql, fmt, limit)
    else:
        _query_local(final_sql, fmt, limit)


def _query_hybrid(sql: str, register_bq_specs: List[str], fmt: str, limit: int):
    """Run two-phase hybrid query: BQ subqueries + local DuckDB."""
    import duckdb
    from src.remote_query import RemoteQueryEngine, RemoteQueryError, load_config

    local_dir = Path(os.environ.get("DA_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        typer.echo("Local DuckDB not found. Run: da sync", err=True)
        raise typer.Exit(1)

    config = load_config()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        engine = RemoteQueryEngine(
            conn,
            max_bq_registration_rows=config.get("max_bq_registration_rows", 500_000),
            max_memory_mb=config.get("max_memory_mb", 2048),
            max_result_rows=limit,
            timeout_seconds=config.get("timeout_seconds", 300),
        )

        # Phase 1: Register BQ subqueries
        for spec in register_bq_specs:
            eq_idx = spec.index("=")
            alias = spec[:eq_idx].strip()
            bq_sql = spec[eq_idx + 1:].strip()
            try:
                stats = engine.register_bq(alias, bq_sql)
                typer.echo(f"  BQ '{alias}': {stats['rows']} rows, {stats['memory_mb']} MB", err=True)
            except RemoteQueryError as e:
                typer.echo(f"Error registering '{alias}': {e}", err=True)
                raise typer.Exit(1)

        # Phase 2: Execute final query
        try:
            result = engine.execute(sql)
        except RemoteQueryError as e:
            typer.echo(f"Query error: {e}", err=True)
            raise typer.Exit(1)

        _output(result["columns"], result["rows"], fmt)
        if result["truncated"]:
            typer.echo(f"(truncated at {limit} rows)", err=True)
    finally:
        conn.close()


def _query_local(sql: str, fmt: str, limit: int):
    """Run query against local DuckDB."""
    import duckdb

    local_dir = Path(os.environ.get("DA_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        typer.echo("Local DuckDB not found. Run: da sync", err=True)
        raise typer.Exit(1)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(sql).fetchmany(limit)
        columns = [desc[0] for desc in conn.description] if conn.description else []
        _output(columns, result, fmt)
    except Exception as e:
        typer.echo(f"Query error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()


def _query_remote(sql: str, fmt: str, limit: int):
    """Run query against server DuckDB via API."""
    from cli.client import api_post

    resp = api_post("/api/query", json={"sql": sql, "limit": limit})
    if resp.status_code != 200:
        typer.echo(f"Query failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    _output(data["columns"], data["rows"], fmt)
    if data.get("truncated"):
        typer.echo(f"(truncated at {limit} rows)", err=True)


def _output(columns: list, rows: list, fmt: str):
    if fmt == "json":
        output = [dict(zip(columns, row)) for row in rows]
        typer.echo(json.dumps(output, indent=2, default=str))
    elif fmt == "csv":
        typer.echo(",".join(columns))
        for row in rows:
            typer.echo(",".join(str(v) if v is not None else "" for v in row))
    else:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table()
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(v) if v is not None else "" for v in row))
        console.print(table)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/query.py tests/test_cli.py
git commit -m "feat: add --register-bq and --stdin to da query for hybrid BQ+local queries"
```

---

### Task 4: API Endpoint `POST /api/query/hybrid`

**Files:**
- Create: `app/api/query_hybrid.py`
- Modify: `app/main.py` (register router)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_api.py`:

```python
class TestHybridQueryAPI:
    def test_hybrid_query_requires_admin(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1", "register_bq": {}},
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert resp.status_code == 403

    def test_hybrid_query_local_only(self, seeded_client):
        """Hybrid endpoint works without BQ registrations (just local query)."""
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1 AS val", "register_bq": {}},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["val"]
        assert data["rows"] == [[1]]

    def test_hybrid_query_blocked_sql(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={"sql": "DROP TABLE users", "register_bq": {}},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400

    def test_hybrid_query_blocked_bq_sql(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/query/hybrid",
            json={
                "sql": "SELECT 1",
                "register_bq": {"x": "DROP TABLE something"},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py::TestHybridQueryAPI -v`
Expected: FAIL — 404 on `/api/query/hybrid`

- [ ] **Step 3: Implement API endpoint**

Create `app/api/query_hybrid.py`:

```python
"""Hybrid query endpoint — two-phase BQ + DuckDB queries."""

from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import require_admin, _get_db
from src.db import get_analytics_db_readonly
from src.remote_query import RemoteQueryEngine, RemoteQueryError, load_config

router = APIRouter(prefix="/api/query", tags=["query"])


class HybridQueryRequest(BaseModel):
    sql: str
    register_bq: Dict[str, str] = {}
    format: str = "json"


@router.post("/hybrid")
async def hybrid_query(
    request: HybridQueryRequest,
    user: dict = Depends(require_admin),
):
    """Execute a two-phase hybrid query: BQ subqueries + DuckDB final query."""
    config = load_config()
    analytics = get_analytics_db_readonly()
    try:
        engine = RemoteQueryEngine(
            analytics,
            max_bq_registration_rows=config.get("max_bq_registration_rows", 500_000),
            max_memory_mb=config.get("max_memory_mb", 2048),
            max_result_rows=config.get("max_result_rows", 100_000),
            timeout_seconds=config.get("timeout_seconds", 300),
        )

        # Phase 1: Register BQ subqueries
        for alias, bq_sql in request.register_bq.items():
            try:
                engine.register_bq(alias, bq_sql)
            except RemoteQueryError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"BQ registration '{alias}' failed: {e.error_type}: {str(e)}",
                )

        # Phase 2: Execute final query
        try:
            result = engine.execute(request.sql)
        except RemoteQueryError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Query failed: {e.error_type}: {str(e)}",
            )

        return result
    finally:
        analytics.close()
```

Register in `app/main.py`:

```python
from app.api.query_hybrid import router as query_hybrid_router
# ...
app.include_router(query_hybrid_router)  # before web_router
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_api.py::TestHybridQueryAPI -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/query_hybrid.py app/main.py tests/test_api.py
git commit -m "feat: add POST /api/query/hybrid endpoint for two-phase BQ+DuckDB queries"
```

---

### Task 5: CLAUDE.md + Integration Test

**Files:**
- Modify: `CLAUDE.md`
- Test: run full suite

- [ ] **Step 1: Add hybrid query docs to CLAUDE.md**

After the "## Business Metrics" section, add:

```markdown
## Hybrid Queries (BigQuery + Local)

For tables too large to sync locally, use hybrid queries that JOIN local data with on-demand BigQuery results:

```bash
da query --sql "SELECT o.*, t.views FROM orders o JOIN traffic t ON o.date = t.date" \
         --register-bq "traffic=SELECT date, SUM(views) as views FROM dataset.web WHERE date > '2026-01-01' GROUP BY 1"
```

The `--register-bq` flag executes a BigQuery subquery, loads the result into memory, and makes it available as a DuckDB view for the final SQL. Multiple `--register-bq` flags can be used for multiple BQ sources.

For complex SQL, use stdin mode:
```bash
echo '{"register_bq": {"traffic": "SELECT ..."}, "sql": "SELECT ..."}' | da query --stdin
```
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add hybrid query usage instructions to CLAUDE.md"
```
