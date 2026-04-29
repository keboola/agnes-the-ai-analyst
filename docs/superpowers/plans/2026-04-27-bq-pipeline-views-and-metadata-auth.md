# BigQuery Pipeline: Views, Metadata Auth, Manifest Filter — Implementation Plan

> **Historical note (2026-04-29):** `CHANGELOG.md` was retired in favor of GitHub Releases. Wherever this plan instructs adding entries under `## [Unreleased]` or modifying `CHANGELOG.md`, the equivalent today is: write the change as the PR title bullet and put migration details in the PR description (Release Drafter auto-aggregates). See CLAUDE.md → "Release notes".
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agnes's BigQuery data source work end-to-end on GCE — handle BQ views (not just base tables), authenticate via the VM's GCE metadata-server token (no key file required), refresh ephemeral tokens at orchestrator rebuild time, and stop the analyst CLI from 404-ing on remote-mode tables.

**Architecture:** Extractor detects view-vs-table via INFORMATION_SCHEMA, generates appropriate DuckDB view (`bigquery_query()` for views, direct `bq.dataset.table` ref for base tables). Auth: extractor and orchestrator both fetch a fresh access token from `http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token` and create a session-scoped DuckDB BQ secret before ATTACH. Sync manifest exposes `query_mode` per table; `da sync` skips `query_mode='remote'` rows.

**Tech Stack:** Python 3.13, DuckDB + community `bigquery` extension, FastAPI, pytest. No new dependencies.

---

## File Structure

**Modify:**
- `connectors/bigquery/extractor.py` — add metadata-token fetch, view detection, dual ATTACH path; fix `__main__` yaml lookup
- `src/orchestrator.py` — refresh BQ token from metadata before ATTACH (replaces empty `token_env` path)
- `app/api/sync.py` — manifest exposes `query_mode` per table
- `cli/commands/sync.py` — skip `query_mode='remote'` in download loop
- `tests/test_bigquery_extractor.py` — new tests for metadata auth + view detection
- `tests/test_orchestrator.py` — new test for BQ token refresh path
- `tests/test_cli_sync.py` — new test for remote-skip behaviour
- `tests/test_sync_manifest.py` — new file (only if no existing manifest test file fits)
- `CHANGELOG.md` — entry under `## [Unreleased]`

**Create:**
- `connectors/bigquery/auth.py` — `get_metadata_token()` helper isolated from extractor for testability

---

## Decisions locked in

1. **Metadata token, not ADC, not key file.** Reason: VM has SA on metadata; key files are a security smell, ADC requires a separate `gcloud auth application-default login` step that production VMs don't run. Token TTL is ~1h; both extractor and orchestrator re-fetch.
2. **`extension == 'bigquery'` is the trigger** in orchestrator for the metadata-refresh path. No new `_remote_attach` schema column. The existing `token_env` field is left empty for BQ rows (signals "use built-in BQ auth path").
3. **View vs TABLE detection at extract time, not query time.** Extractor queries INFORMATION_SCHEMA via `bigquery_query()` once per registered table, picks the view template accordingly, writes a static DuckDB view into `extract.duckdb`. The orchestrator never re-detects.
4. **Both view paths share `bq` alias.** For base tables: `CREATE VIEW "X" AS SELECT * FROM bq."dataset"."X"`. For views: `CREATE VIEW "X" AS SELECT * FROM bigquery_query('project', 'SELECT * FROM \`dataset.X\`')`. Both require `bq` to be ATTACHed at query time.
5. **Manifest filter pushed both server- and client-side.** Server adds `query_mode` field. CLI checks it. Client-side check is the contract — server can't know which CLI version is calling.
6. **Vendor-agnostic OSS rule applies.** No project IDs, hostnames, or GRPN-specific tokens in code, tests, comments, or commit messages. Use placeholders (`my-project`, `my-dataset`, `my-table`) in tests and docs.

---

## Task 1: Extract metadata-token helper into `connectors/bigquery/auth.py`

**Files:**
- Create: `connectors/bigquery/auth.py`
- Test: `tests/test_bigquery_auth.py`

- [ ] **Step 1.1: Write failing test**

```python
# tests/test_bigquery_auth.py
"""Tests for BQ metadata-token auth helper."""

from unittest.mock import patch, MagicMock
import json
import pytest

from connectors.bigquery.auth import (
    get_metadata_token,
    BQMetadataAuthError,
)


def _mock_urlopen(payload: dict, status: int = 200):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    return resp


class TestGetMetadataToken:
    def test_returns_token_string(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"access_token": "ya29.test", "expires_in": 3599})
            token = get_metadata_token()
        assert token == "ya29.test"

    def test_passes_metadata_flavor_header(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"access_token": "tok"})
            get_metadata_token()
            req = m.call_args[0][0]
            assert req.headers.get("Metadata-flavor") == "Google"
            assert "metadata.google.internal" in req.full_url

    def test_raises_on_unreachable_metadata(self):
        from urllib.error import URLError
        with patch("connectors.bigquery.auth.urllib.request.urlopen", side_effect=URLError("no route")):
            with pytest.raises(BQMetadataAuthError, match="metadata server unreachable"):
                get_metadata_token()

    def test_raises_on_missing_access_token_field(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"error": "bad"})
            with pytest.raises(BQMetadataAuthError, match="no access_token in response"):
                get_metadata_token()
```

- [ ] **Step 1.2: Run test to verify failure**

Run: `pytest tests/test_bigquery_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'connectors.bigquery.auth'`

- [ ] **Step 1.3: Implement `connectors/bigquery/auth.py`**

```python
# connectors/bigquery/auth.py
"""BigQuery auth helper — fetch ephemeral access token from GCE metadata server.

Used by the BQ extractor and orchestrator when running on GCE with a service
account attached to the VM. No key file required.
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
_METADATA_TIMEOUT_S = 5


class BQMetadataAuthError(RuntimeError):
    """Raised when GCE metadata token cannot be obtained."""


def get_metadata_token() -> str:
    """Return a fresh access token from the GCE metadata server.

    Raises:
        BQMetadataAuthError: if the metadata server is unreachable or the
            response is malformed.
    """
    req = urllib.request.Request(
        _METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise BQMetadataAuthError(f"metadata server unreachable: {e}") from e
    except (json.JSONDecodeError, ValueError) as e:
        raise BQMetadataAuthError(f"metadata response not JSON: {e}") from e

    token = payload.get("access_token")
    if not token:
        raise BQMetadataAuthError("no access_token in response")
    return token
```

- [ ] **Step 1.4: Run test to verify pass**

Run: `pytest tests/test_bigquery_auth.py -v`
Expected: 4 passed

- [ ] **Step 1.5: Commit**

```bash
git add connectors/bigquery/auth.py tests/test_bigquery_auth.py
git commit -m "feat(bq): add metadata-token auth helper"
```

---

## Task 2: Add view detection helper to BQ extractor

**Files:**
- Modify: `connectors/bigquery/extractor.py`
- Test: `tests/test_bigquery_extractor.py`

- [ ] **Step 2.1: Write failing test**

Append to `tests/test_bigquery_extractor.py`:

```python
class TestDetectTableType:
    """Detect whether a BQ entity is a base table or a view."""

    def test_base_table_returns_table(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("BASE TABLE",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "BASE TABLE"

    def test_view_returns_view(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "VIEW"

    def test_missing_returns_none(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result is None

    def test_query_uses_bigquery_query_function(self):
        """Detection must use bigquery_query() table function (works on views via jobs API)."""
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        _detect_table_type(conn, "my-proj", "my_ds", "my_tbl")

        sql = conn.execute.call_args[0][0]
        assert "bigquery_query" in sql.lower()
        assert "INFORMATION_SCHEMA.TABLES" in sql
        assert "my_tbl" in sql
```

- [ ] **Step 2.2: Run test to verify failure**

Run: `pytest tests/test_bigquery_extractor.py::TestDetectTableType -v`
Expected: FAIL with `ImportError: cannot import name '_detect_table_type'`

- [ ] **Step 2.3: Implement `_detect_table_type` in `connectors/bigquery/extractor.py`**

Add near the top of the file, after imports:

```python
def _detect_table_type(
    conn: duckdb.DuckDBPyConnection,
    project: str,
    dataset: str,
    table: str,
) -> str | None:
    """Return BQ entity type for `project.dataset.table`.

    Uses `bigquery_query()` table function which routes through the BQ jobs
    API — works on tables, views, and materialized views alike. Returns one
    of 'BASE TABLE', 'VIEW', 'MATERIALIZED_VIEW', or None if not found.
    """
    bq_sql = (
        f"SELECT table_type FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
        f"WHERE table_name = '{table}' LIMIT 1"
    )
    duck_sql = f"SELECT * FROM bigquery_query('{project}', '{bq_sql}')"
    row = conn.execute(duck_sql).fetchone()
    return row[0] if row else None
```

- [ ] **Step 2.4: Run test to verify pass**

Run: `pytest tests/test_bigquery_extractor.py::TestDetectTableType -v`
Expected: 4 passed

- [ ] **Step 2.5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bigquery_extractor.py
git commit -m "feat(bq): add view/table detection via INFORMATION_SCHEMA"
```

---

## Task 3: Wire metadata-token + dual view path into `init_extract`

**Files:**
- Modify: `connectors/bigquery/extractor.py`
- Test: `tests/test_bigquery_extractor.py`

- [ ] **Step 3.1: Write failing tests**

Append to `tests/test_bigquery_extractor.py`:

```python
class TestViewVsTableTemplates:
    """init_extract must pick the right view template based on entity type."""

    def test_base_table_uses_direct_attach_ref(self, tmp_path, monkeypatch):
        """For BASE TABLE, generated DuckDB view references bq.dataset.table directly."""
        from connectors.bigquery.extractor import init_extract

        # Stub the metadata token fetch
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        # Stub _detect_table_type to return BASE TABLE
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )

        # Mock duckdb connection so we can capture executed SQL
        captured = []
        real_connect = duckdb.connect

        def spy_connect(*a, **kw):
            conn = real_connect(*a, **kw)
            orig_execute = conn.execute
            def execute_capturing(sql, *args, **kwargs):
                captured.append(sql)
                # Skip INSTALL/LOAD/ATTACH/CREATE SECRET — they need real BQ
                if any(s in sql.upper() for s in ("INSTALL ", "LOAD ", "ATTACH ", "CREATE SECRET")):
                    return MagicMock()
                # Skip CREATE VIEW that references bq.* (no real BQ)
                if 'FROM bq.' in sql or 'FROM bigquery_query' in sql:
                    return MagicMock()
                return orig_execute(sql, *args, **kwargs)
            conn.execute = execute_capturing
            return conn

        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", spy_connect)

        result = init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "orders", "bucket": "my_ds", "source_table": "orders", "description": ""}],
        )

        view_sqls = [s for s in captured if "CREATE OR REPLACE VIEW" in s.upper() or 'CREATE VIEW' in s.upper()]
        assert any('FROM bq."my_ds"."orders"' in s for s in view_sqls), \
            f"expected direct bq.dataset.table ref for BASE TABLE; got: {view_sqls}"
        assert not any("bigquery_query(" in s for s in view_sqls), \
            "BASE TABLE should not use bigquery_query() function"

    def test_view_uses_bigquery_query_function(self, tmp_path, monkeypatch):
        """For VIEW, generated DuckDB view wraps bigquery_query() (jobs API path)."""
        from connectors.bigquery.extractor import init_extract

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "VIEW",
        )

        captured = []
        real_connect = duckdb.connect

        def spy_connect(*a, **kw):
            conn = real_connect(*a, **kw)
            orig_execute = conn.execute
            def execute_capturing(sql, *args, **kwargs):
                captured.append(sql)
                if any(s in sql.upper() for s in ("INSTALL ", "LOAD ", "ATTACH ", "CREATE SECRET")):
                    return MagicMock()
                if 'FROM bq.' in sql or 'FROM bigquery_query' in sql:
                    return MagicMock()
                return orig_execute(sql, *args, **kwargs)
            conn.execute = execute_capturing
            return conn

        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", spy_connect)

        init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "session_view", "bucket": "my_ds", "source_table": "session_view", "description": ""}],
        )

        view_sqls = [s for s in captured if "CREATE OR REPLACE VIEW" in s.upper() or 'CREATE VIEW' in s.upper()]
        view_create = next((s for s in view_sqls if '"session_view"' in s), None)
        assert view_create is not None, f"no CREATE VIEW for session_view; got: {view_sqls}"
        assert "bigquery_query(" in view_create
        assert "my-project" in view_create
        assert "my_ds.session_view" in view_create or "session_view" in view_create


class TestRemoteAttachForBQ:
    """For BQ source, _remote_attach must signal metadata-auth (empty token_env)."""

    def test_remote_attach_token_env_is_empty_for_bq(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )

        # Patch out BQ-specific DuckDB calls
        real_connect = duckdb.connect
        def spy_connect(*a, **kw):
            conn = real_connect(*a, **kw)
            orig_execute = conn.execute
            def safe_execute(sql, *args, **kwargs):
                if any(s in sql.upper() for s in ("INSTALL ", "LOAD ", "ATTACH ", "CREATE SECRET")):
                    return MagicMock()
                if 'FROM bq.' in sql or 'FROM bigquery_query' in sql:
                    return MagicMock()
                return orig_execute(sql, *args, **kwargs)
            conn.execute = safe_execute
            return conn
        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", spy_connect)

        init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "t", "bucket": "ds", "source_table": "t", "description": ""}],
        )

        # Read _remote_attach from the produced extract.duckdb
        c = duckdb.connect(str(tmp_path / "extract.duckdb"), read_only=True)
        rows = c.execute(
            "SELECT alias, extension, url, token_env FROM _remote_attach"
        ).fetchall()
        c.close()

        assert len(rows) == 1
        alias, extension, url, token_env = rows[0]
        assert alias == "bq"
        assert extension == "bigquery"
        assert url == "project=my-project"
        assert token_env == "", \
            "BQ uses metadata auth — token_env must be empty so orchestrator triggers metadata path"
```

- [ ] **Step 3.2: Run tests to verify failure**

Run: `pytest tests/test_bigquery_extractor.py::TestViewVsTableTemplates tests/test_bigquery_extractor.py::TestRemoteAttachForBQ -v`
Expected: FAIL — `init_extract` doesn't yet call `get_metadata_token` or `_detect_table_type`.

- [ ] **Step 3.3: Update `init_extract` to use metadata token + dual path**

Modify `connectors/bigquery/extractor.py`. Replace existing `init_extract` body with:

```python
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import duckdb

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError

logger = logging.getLogger(__name__)


def _detect_table_type(...):  # unchanged from Task 2


def _create_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'remote'
    )""")


def _create_remote_attach_table(
    conn: duckdb.DuckDBPyConnection, project_id: str
) -> None:
    """Write _remote_attach. token_env is empty for BQ — orchestrator
    detects extension='bigquery' and refreshes the token from GCE metadata
    on its own."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["bq", "bigquery", f"project={project_id}", ""],
    )


def init_extract(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create extract.duckdb with remote views into BigQuery.

    Authenticates via the GCE metadata server. For each registered table,
    detects whether the BQ entity is a BASE TABLE or VIEW, and emits a
    DuckDB view that uses the appropriate path:
      - BASE TABLE → direct ATTACH ref (Storage Read API, fast)
      - VIEW       → bigquery_query() table function (jobs API, supports views)

    Args:
        output_dir: Path to write extract.duckdb
        project_id: GCP project ID for billing/job execution
        table_configs: List of table config dicts from table_registry

    Returns:
        Dict with stats: {tables_registered: int, errors: list}
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    stats: Dict[str, Any] = {"tables_registered": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Fetch token before opening DB so failure aborts cleanly without partial file
    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        logger.error("BQ metadata auth failed: %s", e)
        stats["errors"].append({"table": "<auth>", "error": str(e)})
        return stats

    conn = duckdb.connect(str(tmp_db_path))
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        # session-scoped DuckDB secret with the metadata token
        escaped_token = token.replace("'", "''")
        conn.execute(
            f"CREATE SECRET bq_session (TYPE bigquery, ACCESS_TOKEN '{escaped_token}')"
        )
        conn.execute(
            f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)"
        )
        logger.info("Attached BigQuery project: %s", project_id)

        _create_meta_table(conn)
        _create_remote_attach_table(conn, project_id)

        for tc in table_configs:
            table_name = tc["name"]
            dataset = tc.get("bucket", "")
            source_table = tc.get("source_table", table_name)

            try:
                entity_type = _detect_table_type(conn, project_id, dataset, source_table)
                if entity_type is None:
                    raise RuntimeError(
                        f"BQ entity {project_id}.{dataset}.{source_table} not found"
                    )

                if entity_type == "BASE TABLE":
                    # Storage Read API — fast for full scans
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f'SELECT * FROM bq."{dataset}"."{source_table}"'
                    )
                else:
                    # VIEW or MATERIALIZED_VIEW — use jobs API
                    bq_inner = (
                        f"SELECT * FROM `{project_id}.{dataset}.{source_table}`"
                    )
                    bq_inner_escaped = bq_inner.replace("'", "''")
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
                    )

                conn.execute(view_sql)
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_registered"] += 1
                logger.info(
                    "Registered remote view: %s -> %s.%s.%s (%s)",
                    table_name, project_id, dataset, source_table, entity_type,
                )
            except Exception as e:
                logger.error("Failed to register %s: %s", table_name, e)
                stats["errors"].append({"table": table_name, "error": str(e)})

        conn.execute("DETACH bq")
    finally:
        conn.close()

    # Atomic swap
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()
    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))
    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats
```

- [ ] **Step 3.4: Run tests to verify pass**

Run: `pytest tests/test_bigquery_extractor.py -v`
Expected: all classes pass (existing + new TestViewVsTableTemplates + TestRemoteAttachForBQ + TestDetectTableType)

- [ ] **Step 3.5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bigquery_extractor.py
git commit -m "feat(bq): metadata-token auth + view/table dual path in extractor"
```

---

## Task 4: Fix `__main__` config path in extractor

**Files:**
- Modify: `connectors/bigquery/extractor.py` (the `if __name__ == "__main__"` block)
- Test: `tests/test_bigquery_extractor.py`

- [ ] **Step 4.1: Write failing test**

Append to `tests/test_bigquery_extractor.py`:

```python
class TestExtractorMainModule:
    """Standalone `python -m connectors.bigquery.extractor` reads config correctly."""

    def test_main_reads_data_source_bigquery_project(self, tmp_path, monkeypatch):
        """__main__ must read project from data_source.bigquery.project (matches yaml example)."""
        from connectors.bigquery import extractor as ext_mod

        captured_project = {}

        def fake_init_extract(out, project_id, tables):
            captured_project["project"] = project_id
            captured_project["tables"] = tables
            return {"tables_registered": len(tables), "errors": []}

        monkeypatch.setattr(ext_mod, "init_extract", fake_init_extract)

        # Stub config loader to return a config with data_source.bigquery.project
        monkeypatch.setattr(
            "connectors.bigquery.extractor.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "my-test-project", "location": "US"},
                }
            },
        )
        # Stub system DB + repo so we don't hit a real DuckDB
        from unittest.mock import MagicMock
        fake_repo = MagicMock()
        fake_repo.list_by_source.return_value = [
            {"name": "t1", "bucket": "ds", "source_table": "t1", "description": ""},
        ]
        monkeypatch.setattr(
            "connectors.bigquery.extractor.TableRegistryRepository",
            lambda c: fake_repo,
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_system_db",
            lambda: MagicMock(close=lambda: None),
        )
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Re-execute the __main__ block
        import runpy
        runpy.run_module("connectors.bigquery.extractor", run_name="__main__")

        assert captured_project["project"] == "my-test-project"
        assert captured_project["tables"][0]["name"] == "t1"
```

- [ ] **Step 4.2: Run test to verify failure**

Run: `pytest tests/test_bigquery_extractor.py::TestExtractorMainModule -v`
Expected: FAIL — current `__main__` reads `config.get("bigquery")` (top-level), test config has `data_source.bigquery`.

- [ ] **Step 4.3: Update `__main__` block in `connectors/bigquery/extractor.py`**

Replace the existing `if __name__ == "__main__":` block with:

```python
if __name__ == "__main__":
    """Standalone: reads config from instance.yaml + table_registry, creates extract."""
    from config.loader import load_instance_config
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    config = load_instance_config()
    bq_config = config.get("data_source", {}).get("bigquery", {})
    project_id = bq_config.get("project", "")

    if not project_id:
        logger.error(
            "data_source.bigquery.project missing from instance.yaml — "
            "cannot run extractor"
        )
        raise SystemExit(2)

    sys_conn = get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("bigquery")
    finally:
        sys_conn.close()

    if not tables:
        logger.warning("No BigQuery tables registered in table_registry")
    else:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        result = init_extract(
            str(data_dir / "extracts" / "bigquery"), project_id, tables
        )
        logger.info("BigQuery extract init complete: %s", result)
```

- [ ] **Step 4.4: Run test to verify pass**

Run: `pytest tests/test_bigquery_extractor.py::TestExtractorMainModule -v`
Expected: PASS

- [ ] **Step 4.5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bigquery_extractor.py
git commit -m "fix(bq): standalone extractor reads data_source.bigquery.project"
```

---

## Task 5: Orchestrator BQ metadata-token refresh

**Files:**
- Modify: `src/orchestrator.py` (`_attach_remote_extensions`)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 5.1: Write failing test**

Append to `tests/test_orchestrator.py`:

```python
class TestBQMetadataAuth:
    """Orchestrator fetches a fresh metadata token for BQ remote attach."""

    def test_bq_extension_triggers_metadata_token_fetch(self, setup_env, monkeypatch):
        """When _remote_attach.extension='bigquery' with empty token_env, orchestrator
        calls get_metadata_token() and creates a DuckDB secret before ATTACH."""
        from src.orchestrator import SyncOrchestrator
        from unittest.mock import MagicMock

        # Build extract.duckdb with bq _remote_attach
        source_dir = setup_env["extracts_dir"] / "bigquery"
        source_dir.mkdir()
        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')"
        )
        # Local stub view so rebuild has something to attach (avoids INSTALL bigquery in test)
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute(
            "INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')"
        )
        conn.close()

        # Stub get_metadata_token
        called = {"count": 0}
        def fake_token():
            called["count"] += 1
            return "ya29.fake-token"
        monkeypatch.setattr(
            "src.orchestrator.get_metadata_token",
            fake_token,
        )

        # Capture executed SQL on the master connection
        captured = []
        real_connect = duckdb.connect
        def spy_connect(path, *a, **kw):
            c = real_connect(path, *a, **kw)
            orig = c.execute
            def cap(sql, *args, **kwargs):
                captured.append(sql)
                # Skip BQ-extension-specific calls
                if "INSTALL bigquery" in sql or "LOAD bigquery" in sql:
                    return MagicMock()
                if "CREATE SECRET" in sql and "bigquery" in sql.lower():
                    return MagicMock()
                if "ATTACH" in sql.upper() and "bigquery" in sql.lower():
                    return MagicMock()
                return orig(sql, *args, **kwargs)
            c.execute = cap
            return c
        monkeypatch.setattr("src.orchestrator.duckdb.connect", spy_connect)

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        orch.rebuild()

        assert called["count"] >= 1, "get_metadata_token() must be called for BQ source"
        assert any("CREATE SECRET" in s and "bigquery" in s.lower() for s in captured), \
            "orchestrator must create DuckDB secret with metadata token"
        assert any(
            "ATTACH" in s.upper() and "bigquery" in s.lower() and "token" not in s.lower()
            for s in captured
        ), "ATTACH for BQ must not pass TOKEN= directly (uses secret instead)"

    def test_bq_metadata_failure_logs_and_skips(self, setup_env, monkeypatch, caplog):
        """If metadata is unreachable, orchestrator logs and skips the BQ source — does not crash."""
        from src.orchestrator import SyncOrchestrator
        from connectors.bigquery.auth import BQMetadataAuthError

        # Build minimal BQ extract
        source_dir = setup_env["extracts_dir"] / "bigquery"
        source_dir.mkdir()
        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')"
        )
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute(
            "INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')"
        )
        conn.close()

        def boom():
            raise BQMetadataAuthError("metadata server unreachable: simulated")
        monkeypatch.setattr("src.orchestrator.get_metadata_token", boom)

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # Should still complete — local 'stub' from same source attaches
        assert "bigquery" in result
        assert "stub" in result["bigquery"]
        assert any(
            "metadata" in r.message.lower() and r.levelname == "ERROR"
            for r in caplog.records
        )
```

- [ ] **Step 5.2: Run tests to verify failure**

Run: `pytest tests/test_orchestrator.py::TestBQMetadataAuth -v`
Expected: FAIL — `src.orchestrator` does not yet import `get_metadata_token`.

- [ ] **Step 5.3: Update `_attach_remote_extensions` in `src/orchestrator.py`**

Add import at top:

```python
from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError
```

Replace the `for alias, extension, url, token_env in rows:` block in `_attach_remote_extensions` with:

```python
for alias, extension, url, token_env in rows:
    if not _validate_identifier(alias, "remote_attach alias"):
        continue
    if not _validate_identifier(extension, "remote_attach extension"):
        continue

    try:
        # Skip if already attached (multi-source extension sharing)
        attached = {
            r[0] for r in conn.execute(
                "SELECT database_name FROM duckdb_databases()"
            ).fetchall()
        }
        if alias in attached:
            logger.debug("Remote source %s already attached", alias)
            continue

        conn.execute(f"INSTALL {extension} FROM community; LOAD {extension};")

        # BQ-specific: refresh token from GCE metadata, create secret before ATTACH
        if extension == "bigquery":
            try:
                bq_token = get_metadata_token()
            except BQMetadataAuthError as e:
                logger.error(
                    "Failed to fetch BQ metadata token for %s: %s — skipping ATTACH",
                    alias, e,
                )
                continue
            escaped = bq_token.replace("'", "''")
            secret_name = f"bq_secret_{alias}"
            conn.execute(
                f"CREATE OR REPLACE SECRET {secret_name} "
                f"(TYPE bigquery, ACCESS_TOKEN '{escaped}')"
            )
            conn.execute(
                f"ATTACH '{url}' AS {alias} (TYPE {extension}, READ_ONLY)"
            )
        elif token_env:
            # Generic env-var path (e.g. Keboola)
            token = os.environ.get(token_env, "")
            if not token:
                logger.warning(
                    "Remote attach %s: env var %s not set, skipping",
                    alias, token_env,
                )
                continue
            escaped_token = token.replace("'", "''")
            conn.execute(
                f"ATTACH '{url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
            )
        else:
            # No auth required (or extension handles it via env)
            conn.execute(
                f"ATTACH '{url}' AS {alias} (TYPE {extension}, READ_ONLY)"
            )

        logger.info("Attached remote source %s via %s extension", alias, extension)
    except Exception as e:
        logger.error("Failed to attach remote source %s: %s", alias, e)
```

- [ ] **Step 5.4: Run tests to verify pass**

Run: `pytest tests/test_orchestrator.py::TestBQMetadataAuth -v`
Expected: 2 passed

- [ ] **Step 5.5: Run full orchestrator test suite to verify no regression**

Run: `pytest tests/test_orchestrator.py -v`
Expected: all pass (existing TestSyncOrchestrator tests must still work)

- [ ] **Step 5.6: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): refresh BQ metadata token before ATTACH"
```

---

## Task 6: Manifest exposes `query_mode` per table

**Files:**
- Modify: `app/api/sync.py` (manifest builder around the `tables[table_id] = ...` block)
- Test: `tests/test_sync_manifest.py` (create if it doesn't exist; otherwise add to nearest existing sync API test file)

- [ ] **Step 6.1: Locate or create the manifest test file**

Run: `ls tests/ | grep -i sync_manifest && echo MAYBE_EXISTS || echo CREATE_NEW`

If the file doesn't exist, the test in Step 6.2 creates `tests/test_sync_manifest.py`.

- [ ] **Step 6.2: Write failing test**

Create `tests/test_sync_manifest.py` (or append to existing file):

```python
"""Tests for /api/sync/manifest — the query_mode field in particular."""

from fastapi.testclient import TestClient
from app.main import app
import pytest


@pytest.fixture
def admin_headers(seeded_admin_token):
    """Reuse existing fixture from conftest.py that yields a session/PAT for admin."""
    return {"Authorization": f"Bearer {seeded_admin_token}"}


class TestManifestQueryMode:
    def test_local_table_has_query_mode_local(self, client, admin_headers, register_table):
        register_table(
            id="orders", source_type="keboola", bucket="sales",
            source_table="orders", query_mode="local",
        )
        r = client.get("/api/sync/manifest", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["tables"]["orders"]["query_mode"] == "local"

    def test_remote_table_has_query_mode_remote(self, client, admin_headers, register_table):
        register_table(
            id="bq_view", source_type="bigquery", bucket="ds",
            source_table="bq_view", query_mode="remote",
        )
        r = client.get("/api/sync/manifest", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["tables"]["bq_view"]["query_mode"] == "remote"
```

If `client`, `admin_headers`, `register_table` fixtures don't exist in `conftest.py`, the test file must define them inline or this task expands. Quick scan first: `grep -n "def client\|seeded_admin_token\|register_table" tests/conftest.py`. If absent, fall back to a more direct test that uses `SyncStateRepository` directly:

```python
def test_manifest_includes_query_mode(tmp_path, monkeypatch):
    """Direct test — populate sync_state + table_registry, call manifest builder."""
    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.api.sync import _build_manifest_for_user  # see Step 6.3

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id="bq_t", name="bq_t", source_type="bigquery",
            bucket="ds", source_table="bq_t", query_mode="remote",
        )
        SyncStateRepository(conn).update_sync(
            table_id="bq_t", rows=0, file_size_bytes=0, hash="",
        )

        admin = {"role": "admin", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["bq_t"]["query_mode"] == "remote"
    finally:
        conn.close()
```

- [ ] **Step 6.3: Refactor manifest into `_build_manifest_for_user` + add `query_mode`**

In `app/api/sync.py`, extract the manifest body into a testable function and join with `table_registry`:

```python
def _build_manifest_for_user(conn, user):
    """Build a manifest dict filtered by user's accessible tables."""
    sync_repo = SyncStateRepository(conn)
    table_repo = TableRegistryRepository(conn)
    all_states = sync_repo.get_all_states()
    registry_by_id = {t["id"]: t for t in table_repo.list_all()}

    if user.get("role") != "admin":
        all_states = [s for s in all_states if can_access_table(user, s["table_id"], conn)]

    data_dir = _get_data_dir()
    tables = {}
    for state in all_states:
        table_id = state["table_id"]
        reg = registry_by_id.get(table_id, {})
        tables[table_id] = {
            "hash": state.get("hash", ""),
            "updated": state.get("last_sync").isoformat() if state.get("last_sync") else None,
            "size_bytes": state.get("file_size_bytes", 0),
            "rows": state.get("rows", 0),
            "query_mode": reg.get("query_mode", "local"),
            "source_type": reg.get("source_type", ""),
        }

    # Asset hashes block — keep as-is
    docs_dir = data_dir / "docs"
    assets = {}
    for asset_name, asset_path in [
        ("docs", docs_dir),
        ("profiles", data_dir / "src_data" / "metadata" / "profiles.json"),
    ]:
        if asset_path.exists():
            if asset_path.is_file():
                assets[asset_name] = {"hash": _file_hash(asset_path)}
            else:
                newest = max(
                    (f.stat().st_mtime for f in asset_path.rglob("*") if f.is_file()),
                    default=0,
                )
                assets[asset_name] = {"hash": str(int(newest))}

    return {
        "tables": tables,
        "assets": assets,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
```

Add the import: `from src.repositories.table_registry import TableRegistryRepository`.

Then the FastAPI route becomes:

```python
@router.get("/manifest")
async def sync_manifest(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return _build_manifest_for_user(conn, user)
```

- [ ] **Step 6.4: Run tests to verify pass**

Run: `pytest tests/test_sync_manifest.py -v`
Expected: PASS

- [ ] **Step 6.5: Commit**

```bash
git add app/api/sync.py tests/test_sync_manifest.py
git commit -m "feat(sync): manifest exposes query_mode + source_type per table"
```

---

## Task 7: CLI sync skips remote tables

**Files:**
- Modify: `cli/commands/sync.py` (download decision around lines 67-77)
- Test: `tests/test_cli_sync.py`

- [ ] **Step 7.1: Write failing test**

Append to `tests/test_cli_sync.py` inside `class TestSyncHappyPath:` (or as a sibling class):

```python
class TestSyncRespectsQueryMode:
    def test_sync_skips_remote_query_mode_tables(self, tmp_config, monkeypatch):
        """Tables with query_mode='remote' must not be downloaded — they have no parquet on the server."""
        from cli.commands.sync import sync as sync_cmd

        manifest = {
            "tables": {
                "orders": {"hash": "abc", "query_mode": "local"},
                "bq_view": {"hash": "", "query_mode": "remote"},
            },
            "assets": {},
            "server_time": "2026-04-27T00:00:00Z",
        }

        called_downloads = []

        def fake_api_get(path):
            class R:
                status_code = 200
                def json(self): return manifest
            return R()

        def fake_stream_download(path, target):
            called_downloads.append(path)

        monkeypatch.setattr("cli.commands.sync.api_get", fake_api_get)
        monkeypatch.setattr("cli.commands.sync.stream_download", fake_stream_download)

        # Run with dry_run=False but mock the disk side
        # (use existing tmp_config fixture for state path)
        # Invoke sync_cmd as if from CLI
        from typer.testing import CliRunner
        from cli.main import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["sync"])

        # Only 'orders' should be downloaded
        downloaded_ids = [p.split("/")[-2] for p in called_downloads]
        assert "orders" in downloaded_ids
        assert "bq_view" not in downloaded_ids
```

- [ ] **Step 7.2: Run test to verify failure**

Run: `pytest tests/test_cli_sync.py::TestSyncRespectsQueryMode -v`
Expected: FAIL — current loop downloads `bq_view` because its hash is empty.

- [ ] **Step 7.3: Update download decision in `cli/commands/sync.py`**

Replace the loop around line 67-77 with:

```python
        # 2. Determine what to download
        to_download = []
        skipped_remote = []
        for tid, info in server_tables.items():
            if table and tid != table:
                continue
            if docs_only:
                continue
            # Tables with query_mode='remote' have no parquet on the server —
            # they're queried via /api/query (BQ pushdown). Skip them in sync.
            if info.get("query_mode") == "remote":
                skipped_remote.append(tid)
                continue
            local_hash = local_tables.get(tid, {}).get("hash", "")
            server_hash = info.get("hash", "")
            if server_hash != local_hash or tid not in local_tables or not server_hash:
                to_download.append(tid)

        if skipped_remote and not as_json:
            typer.echo(
                f"Skipping {len(skipped_remote)} remote-mode tables: "
                f"{', '.join(skipped_remote[:5])}"
                + (f" (+{len(skipped_remote) - 5} more)" if len(skipped_remote) > 5 else ""),
                err=True,
            )
```

- [ ] **Step 7.4: Run test to verify pass**

Run: `pytest tests/test_cli_sync.py::TestSyncRespectsQueryMode -v`
Expected: PASS

- [ ] **Step 7.5: Run full cli sync suite to verify no regression**

Run: `pytest tests/test_cli_sync.py -v`
Expected: all pass

- [ ] **Step 7.6: Commit**

```bash
git add cli/commands/sync.py tests/test_cli_sync.py
git commit -m "feat(cli): da sync skips query_mode=remote tables"
```

---

## Task 8: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 8.1: Add entries under `## [Unreleased]`**

Add to the topmost `## [Unreleased]` section in `CHANGELOG.md`:

```markdown
### Added
- BigQuery extractor: detect view-vs-table via INFORMATION_SCHEMA and emit DuckDB views with the appropriate path. BASE TABLEs use the Storage Read API (fast); VIEWs use `bigquery_query()` (jobs API, supports views and materialized views).
- BigQuery auth: extractor and orchestrator fetch fresh access tokens from the GCE metadata server on each extract/rebuild — no key file required when running on GCE with a service account attached. See `connectors/bigquery/auth.py`.
- `/api/sync/manifest` response now includes `query_mode` and `source_type` per table so clients can branch behaviour without a second lookup.

### Changed
- `da sync` skips `query_mode='remote'` tables in the download loop and prints a one-line summary of skipped tables to stderr. Previously, remote tables produced 404s because no parquet exists for them on the server.

### Fixed
- `python -m connectors.bigquery.extractor` (standalone CLI) now reads project ID from `data_source.bigquery.project` matching the `instance.yaml.example` shape. Previously it looked for a top-level `bigquery.project_id` key that the example doesn't document.
```

- [ ] **Step 8.2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): BigQuery views + metadata auth + manifest query_mode"
```

---

## Task 9: Integration verification on dev VM

**Files:** none — verification only, no code changes.

This task validates the whole pipeline against a real BigQuery dataset on the dev VM. Steps below assume the test branch has been pushed and the VM auto-upgrade has picked up the new image (or the engineer has triggered `sudo /usr/local/bin/agnes-auto-upgrade.sh`).

- [ ] **Step 9.1: Push branch and wait for image rebuild**

```bash
git push origin <branch-name>
gh run watch --branch <branch-name>  # or poll: gh run list --branch <branch-name>
```

Expected: release.yml completes, `:dev-<prefix>-latest` floating tag points to new digest.

- [ ] **Step 9.2: Trigger VM auto-upgrade**

On the dev VM (via SSH):

```bash
sudo /usr/local/bin/agnes-auto-upgrade.sh
docker exec agnes-app-1 curl -sS http://localhost:8000/api/health \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["commit_sha"], d["schema_version"])'
```

Expected: `commit_sha` matches your branch HEAD; `schema_version` is the latest.

- [ ] **Step 9.3: Register one BASE TABLE and one VIEW**

Pick any base table and any view from a BQ dataset the VM service account can read. Stop the app + scheduler briefly to release the system.duckdb lock, register both via direct DuckDB insert, then restart:

```bash
ssh <dev-vm>
cd /opt/agnes
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml \
  $([ -f /data/state/certs/fullchain.pem ] && echo '-f docker-compose.tls.yml') \
  stop app scheduler

sudo docker run --rm \
  --env-file /opt/agnes/.env \
  -v agnes_data:/data \
  -v /opt/agnes/config:/app/config:ro \
  -e DATA_DIR=/data \
  ghcr.io/keboola/agnes-the-ai-analyst:dev-<prefix>-latest \
  python3 -c '
from src.db import get_system_db
from src.repositories.table_registry import TableRegistryRepository
c = get_system_db()
TableRegistryRepository(c).register(
    id="example_table", name="example_table", folder="ds",
    source_type="bigquery", bucket="my_dataset",
    source_table="my_base_table", query_mode="remote",
)
TableRegistryRepository(c).register(
    id="example_view", name="example_view", folder="ds",
    source_type="bigquery", bucket="my_dataset",
    source_table="my_view", query_mode="remote",
)
c.close()
'
```

- [ ] **Step 9.4: Run extractor and verify both view templates**

```bash
sudo docker run --rm --network=host \
  --env-file /opt/agnes/.env \
  -v agnes_data:/data \
  -v /opt/agnes/config:/app/config:ro \
  -e DATA_DIR=/data \
  ghcr.io/keboola/agnes-the-ai-analyst:dev-<prefix>-latest \
  python3 -m connectors.bigquery.extractor

# verify
sudo docker run --rm \
  -v agnes_data:/data \
  ghcr.io/keboola/agnes-the-ai-analyst:dev-<prefix>-latest \
  python3 -c '
import duckdb
c = duckdb.connect("/data/extracts/bigquery/extract.duckdb", read_only=True)
print("_meta:", c.execute("SELECT table_name, query_mode FROM _meta").fetchall())
for view_name in ("example_table", "example_view"):
    sql = c.execute(f"SELECT sql FROM duckdb_views() WHERE view_name = ?", [view_name]).fetchone()
    print(view_name, "->", sql[0] if sql else "missing")
'
```

Expected output:
- `_meta` has both rows
- `example_table` definition contains `FROM bq."my_dataset"."my_base_table"`
- `example_view` definition contains `bigquery_query('my-project'`

- [ ] **Step 9.5: Run orchestrator rebuild**

```bash
sudo docker run --rm --network=host \
  --env-file /opt/agnes/.env \
  -v agnes_data:/data \
  -v /opt/agnes/config:/app/config:ro \
  -e DATA_DIR=/data \
  ghcr.io/keboola/agnes-the-ai-analyst:dev-<prefix>-latest \
  python3 -c '
import os
os.environ["DATA_DIR"] = "/data"
from src.orchestrator import SyncOrchestrator
print(SyncOrchestrator().rebuild())
'
```

Expected: `{'bigquery': ['example_table', 'example_view']}`

- [ ] **Step 9.6: Restart app+scheduler, query through `/api/query`**

```bash
sudo docker compose ... start app scheduler
sleep 6

# Get a session token (or use a PAT) from the running app
# Then query via curl with auth:
sudo docker exec agnes-app-1 curl -sS \
  -H "Content-Type: application/json" \
  -X POST \
  -d '{"sql": "SELECT COUNT(*) AS c FROM example_view"}' \
  http://localhost:8000/api/query
```

Expected: returns a row count from the BQ view (proves query pushed through DuckDB master view → bq.dataset.view → BigQuery jobs API).

- [ ] **Step 9.7: Verify `da sync` skips remote tables**

From a laptop with `da` CLI configured against the dev VM:

```bash
da sync 2>&1 | tail -5
```

Expected: stderr line `Skipping 2 remote-mode tables: example_table, example_view`. No 404s. No parquet downloaded.

- [ ] **Step 9.8: If E2E passes, mark plan tasks complete and open PR**

```bash
gh pr create --title "BigQuery: views, metadata auth, manifest filter" --body "$(cat <<EOF
## Summary
- BigQuery extractor now handles BQ views (uses bigquery_query() jobs API) in addition to base tables (Storage Read API).
- Auth via GCE metadata token instead of key file or ADC. Both extractor and orchestrator refresh the token on each run.
- /api/sync/manifest exposes query_mode + source_type; da sync skips remote-mode tables.

## Test plan
- [x] Unit: tests/test_bigquery_auth.py (new)
- [x] Unit: tests/test_bigquery_extractor.py (TestDetectTableType, TestViewVsTableTemplates, TestRemoteAttachForBQ, TestExtractorMainModule)
- [x] Unit: tests/test_orchestrator.py (TestBQMetadataAuth)
- [x] Unit: tests/test_sync_manifest.py
- [x] Unit: tests/test_cli_sync.py (TestSyncRespectsQueryMode)
- [x] E2E: registered base table + view on dev VM, ran extractor + orchestrator + /api/query, verified results
EOF
)"
```

If E2E fails, capture the failure mode in a comment on the PR or in the plan as a blocking issue, and stop here — don't merge until resolved.

---

## Self-Review

**Spec coverage:**
- ✅ Issue 1 (Storage Read fails on views) → Tasks 2 + 3 (detect type, dual path)
- ✅ Issue 2 (BQ auth needs metadata token) → Tasks 1 + 3 + 5 (helper + extractor + orchestrator)
- ✅ Issue 3 (`_remote_attach.token_env` empty) → Task 5 (orchestrator refreshes from metadata when extension='bigquery')
- ✅ Issue 4 (`__main__` config path bug) → Task 4
- ✅ Issue 6 (da sync 404 on remote) → Tasks 6 + 7 (manifest field + CLI skip)
- ⚠️ Issue 5 (BQ discovery in /api/admin/discover-tables) — **out of scope**, deferred (admin UI fluency, can use direct REST in the meantime)
- ⚠️ Issue 7 (scheduler 401 on /api/sync/trigger) — **out of scope**, separate auth design issue

**Placeholder scan:** No "TBD" / "implement later" / "similar to Task N". Each task has full code blocks.

**Type consistency:**
- `get_metadata_token()` signature stable across Tasks 1, 3, 5
- `_detect_table_type(conn, project, dataset, table) -> str | None` stable across Tasks 2, 3
- `_remote_attach.token_env=""` (empty) is the contract for BQ across Tasks 3 (writes) and 5 (reads)

**Notes for the engineer:**
- Tests in Tasks 3 and 5 use `monkeypatch` to stub the BQ extension calls (INSTALL/LOAD/ATTACH/CREATE SECRET). The community BQ extension is not installable in CI without internet + GCS auth, so we never run real BQ in unit tests. All real-BQ verification happens in Task 9.
- The `tests/test_sync_manifest.py` fallback test in Task 6.2 uses `_build_manifest_for_user` directly — make sure to expose it (don't keep it as a closure inside the route). The route refactor is in Step 6.3.
- Task 9 requires SSH access to a dev VM with GCE metadata access AND the VM SA having `bigquery.jobs.create` + `bigquery.tables.get` on the target project.
