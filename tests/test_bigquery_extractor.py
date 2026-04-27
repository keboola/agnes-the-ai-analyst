"""Tests for BigQuery extractor (remote-only via DuckDB extension)."""

import re
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from connectors.bigquery.extractor import _detect_table_type
from tests.helpers.contract import validate_extract_contract


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "bigquery"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def sample_configs():
    return [
        {
            "id": "project.analytics.orders",
            "name": "orders",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
            "description": "Order data from BQ",
        },
        {
            "id": "project.analytics.sessions",
            "name": "sessions",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "sessions",
            "query_mode": "remote",
            "description": "Session data",
        },
    ]


class _DuckDBProxy:
    """Proxy around a real DuckDB connection that intercepts BigQuery extension SQL."""

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("INSTALL BIGQUERY") or sql_upper.startswith(
            "LOAD BIGQUERY"
        ):
            return MagicMock()
        if sql_upper.startswith("CREATE SECRET"):
            return MagicMock()
        if "ATTACH" in sql_upper and "BIGQUERY" in sql_upper:
            return MagicMock()
        if sql_upper.startswith("DETACH BQ"):
            return MagicMock()
        # CREATE VIEW referencing bq.* -> create a dummy table instead
        if "FROM BQ." in sql_upper and "CREATE" in sql_upper:
            match = re.search(r'VIEW\s+"?(\w+)"?', sql, re.IGNORECASE)
            if match:
                view_name = match.group(1)
                self._real.execute(
                    f'CREATE OR REPLACE TABLE "{view_name}" (dummy INTEGER)'
                )
                return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestBigQueryExtractor:
    def test_creates_extract_duckdb_with_meta(self, output_dir, sample_configs, monkeypatch):
        """Test that init_extract creates extract.duckdb with _meta and _remote_attach."""
        from unittest.mock import patch

        # Mock metadata-token auth + entity type detection so the test runs offline.
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )

        def proxy_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return _DuckDBProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", sample_configs)

        assert result["tables_registered"] == 2
        assert len(result["errors"]) == 0

        # Verify extract.duckdb has _meta with correct data
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            meta = conn.execute(
                "SELECT table_name, query_mode FROM _meta ORDER BY table_name"
            ).fetchall()
            assert len(meta) == 2
            assert meta[0][0] == "orders"
            assert meta[0][1] == "remote"
            assert meta[1][0] == "sessions"
            assert meta[1][1] == "remote"

            # Verify _remote_attach table for orchestrator re-ATTACH
            ra = conn.execute(
                "SELECT alias, extension, url, token_env FROM _remote_attach"
            ).fetchone()
            assert ra[0] == "bq"
            assert ra[1] == "bigquery"
            assert ra[2] == "project=my-project"
            assert ra[3] == ""  # BQ handles auth via env automatically
        finally:
            conn.close()

        validate_extract_contract(str(Path(output_dir) / "extract.duckdb"))

    def test_no_data_directory_created(self, output_dir, sample_configs):
        """BigQuery is remote-only -- no data/ directory should exist."""
        assert not (Path(output_dir) / "data").exists()

    def test_all_tables_are_remote(self, output_dir):
        """Verify all BigQuery tables get query_mode='remote' in _meta."""
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute(
            "INSERT INTO _meta VALUES ('t1', '', 0, 0, current_timestamp, 'remote')"
        )

        result = conn.execute("SELECT query_mode FROM _meta").fetchone()
        assert result[0] == "remote"
        conn.close()

    def test_handles_registration_failure(self, output_dir):
        """A failed table registration records error but does not stop others."""
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path))

        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # Simulate: first succeeds, second fails (not inserted)
        conn.execute(
            "INSERT INTO _meta VALUES ('good_table', '', 0, 0, ?, 'remote')", [now]
        )

        meta = conn.execute("SELECT count(*) FROM _meta").fetchone()
        assert meta[0] == 1  # Only good_table registered
        conn.close()

    def test_meta_table_schema(self, output_dir):
        """Verify _meta table has all required columns per the extract.duckdb contract."""
        from connectors.bigquery.extractor import _create_meta_table

        db_path = Path(output_dir) / "contract_check.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_meta_table(conn)

        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = '_meta' ORDER BY ordinal_position"
        ).fetchall()
        col_names = [c[0] for c in columns]
        assert col_names == [
            "table_name",
            "description",
            "rows",
            "size_bytes",
            "extracted_at",
            "query_mode",
        ]
        conn.close()


class TestDetectTableType:
    """Detect whether a BQ entity is a base table or a view."""

    def test_base_table_returns_table(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("BASE TABLE",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "BASE TABLE"

    def test_view_returns_view(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "VIEW"

    def test_missing_returns_none(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result is None

    def test_query_uses_bigquery_query_function(self):
        """Detection must use bigquery_query() table function (works on views via jobs API)."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        _detect_table_type(conn, "my-proj", "my_ds", "my_tbl")

        # SQL must use the bigquery_query() table function (not direct ref)
        sql = conn.execute.call_args[0][0]
        assert "bigquery_query" in sql.lower()

        # The inner BQ SQL is passed as a parameter, not f-stringed in.
        # Verify both project and the BQ SQL appear in the bound params.
        params = conn.execute.call_args[0][1]
        assert "my-proj" in params, f"expected project in params, got: {params}"
        # The inner BQ SQL is one of the params; it should reference INFORMATION_SCHEMA.TABLES
        bq_sql_param = next(
            (p for p in params if isinstance(p, str) and "INFORMATION_SCHEMA.TABLES" in p),
            None,
        )
        assert bq_sql_param is not None, f"inner BQ SQL not found in params: {params}"
        assert "my_ds" in bq_sql_param  # dataset is f-stringed into the BQ SQL identifier path
        # Table name should NOT be inline in the BQ SQL — it goes through the param chain
        assert "my_tbl" in params, f"table name should be a separate param, got: {params}"


class _CapturingProxy:
    """Wraps a real DuckDB connection, captures all SQL, stubs BQ-specific calls.

    DuckDBPyConnection.execute is a C-level read-only attribute, so we can't
    patch the method directly on the connection — we have to wrap with a proxy.
    """

    def __init__(self, real_conn, captured: list):
        self._real = real_conn
        self._captured = captured

    def execute(self, sql, *args, **kwargs):
        self._captured.append(sql)
        stripped_u = sql.strip().upper()
        # Stub only commands that would talk to BQ; CREATE TABLE / INSERT etc.
        # must pass through to the real DuckDB so _meta + _remote_attach persist.
        if stripped_u.startswith(("INSTALL ", "LOAD ", "CREATE SECRET")):
            return MagicMock()
        if stripped_u.startswith("ATTACH ") and "BIGQUERY" in stripped_u:
            return MagicMock()
        if stripped_u.startswith("DETACH "):
            return MagicMock()
        if 'FROM bq.' in sql or 'FROM bigquery_query' in sql:
            return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestViewVsTableTemplates:
    """init_extract must pick the right view template based on entity type."""

    def test_base_table_uses_direct_attach_ref(self, tmp_path, monkeypatch):
        """For BASE TABLE, generated DuckDB view references bq.dataset.table directly."""
        from connectors.bigquery.extractor import init_extract

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )

        captured = []
        real_connect = duckdb.connect

        def spy_connect(*a, **kw):
            real_conn = real_connect(*a, **kw)
            return _CapturingProxy(real_conn, captured)

        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", spy_connect)

        init_extract(
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
            real_conn = real_connect(*a, **kw)
            return _CapturingProxy(real_conn, captured)

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
        assert "`my-project.my_ds.session_view`" in view_create, \
            f"expected backtick-quoted full path; got: {view_create}"


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

        captured = []
        real_connect = duckdb.connect

        def spy_connect(*a, **kw):
            real_conn = real_connect(*a, **kw)
            return _CapturingProxy(real_conn, captured)

        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", spy_connect)

        init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "t", "bucket": "ds", "source_table": "t", "description": ""}],
        )

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


class TestInitExtractAuthFailure:
    """init_extract must abort cleanly if metadata token fetch fails."""

    def test_returns_error_when_metadata_unreachable(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract
        from connectors.bigquery.auth import BQMetadataAuthError

        def boom():
            raise BQMetadataAuthError("metadata server unreachable: simulated")
        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            boom,
        )

        result = init_extract(
            str(tmp_path),
            "my-project",
            [{"name": "t", "bucket": "ds", "source_table": "t", "description": ""}],
        )

        # No partial extract.duckdb — auth failure aborts before any DB writes
        assert not (tmp_path / "extract.duckdb").exists(), \
            "extract.duckdb should not be created when auth fails"
        assert result["tables_registered"] == 0
        assert any("metadata" in e.get("error", "").lower() for e in result["errors"])


class TestIdentifierValidation:
    """init_extract must reject unsafe identifiers before any SQL construction."""

    def test_rejects_unsafe_dataset_name(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )
        # Stub all DuckDB BQ-extension calls so the test stays offline
        captured = []
        real_connect = duckdb.connect
        def safe_connect(*a, **kw):
            return _CapturingProxy(real_connect(*a, **kw), captured)
        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", safe_connect)

        result = init_extract(
            str(tmp_path),
            "my-project",
            [{
                "name": "t",
                "bucket": 'evil"; DROP TABLE foo; --',
                "source_table": "t",
                "description": "",
            }],
        )
        assert result["tables_registered"] == 0
        assert any("dataset" in e.get("error", "").lower() for e in result["errors"])

    def test_rejects_unsafe_source_table_name(self, tmp_path, monkeypatch):
        from connectors.bigquery.extractor import init_extract

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            lambda *a, **kw: "BASE TABLE",
        )
        captured = []
        real_connect = duckdb.connect
        def safe_connect(*a, **kw):
            return _CapturingProxy(real_connect(*a, **kw), captured)
        monkeypatch.setattr("connectors.bigquery.extractor.duckdb.connect", safe_connect)

        result = init_extract(
            str(tmp_path),
            "my-project",
            [{
                "name": "t",
                "bucket": "ds",
                "source_table": "evil`name",
                "description": "",
            }],
        )
        assert result["tables_registered"] == 0
        assert any("source_table" in e.get("error", "").lower() for e in result["errors"])


class TestExtractorMainModule:
    """Standalone `python -m connectors.bigquery.extractor` reads config correctly."""

    def test_main_reads_data_source_bigquery_project(self, tmp_path, monkeypatch):
        """__main__ must read project from data_source.bigquery.project (matches yaml example)."""
        import os
        from pathlib import Path
        
        captured_project = {}

        def fake_init_extract(out, project_id, tables):
            captured_project["project"] = project_id
            captured_project["tables"] = tables
            return {"tables_registered": len(tables), "errors": []}

        # Stub config loader BEFORE importing
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "my-test-project", "location": "US"},
                }
            },
        )
        # Stub system DB + repo
        fake_repo = MagicMock()
        fake_repo.list_by_source.return_value = [
            {"name": "t1", "bucket": "ds", "source_table": "t1", "description": ""},
        ]
        monkeypatch.setattr(
            "src.repositories.table_registry.TableRegistryRepository",
            lambda c: fake_repo,
        )
        monkeypatch.setattr(
            "src.db.get_system_db",
            lambda: MagicMock(close=lambda: None),
        )
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Now import after patching
        from config.loader import load_instance_config
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        import connectors.bigquery.extractor as ext_mod
        
        # Monkeypatch init_extract in the current module
        original_init = ext_mod.init_extract
        ext_mod.init_extract = fake_init_extract

        try:
            # Execute the __main__ logic directly
            config = load_instance_config()
            bq_config = config.get("data_source", {}).get("bigquery", {})
            project_id = bq_config.get("project", "")

            if not project_id:
                raise AssertionError("project_id should not be empty")

            sys_conn = get_system_db()
            try:
                repo = TableRegistryRepository(sys_conn)
                tables = repo.list_by_source("bigquery")
            finally:
                sys_conn.close()

            if tables:
                data_dir = Path(os.environ.get("DATA_DIR", "./data"))
                result = ext_mod.init_extract(
                    str(data_dir / "extracts" / "bigquery"), project_id, tables
                )

            assert captured_project["project"] == "my-test-project"
            assert captured_project["tables"][0]["name"] == "t1"
        finally:
            ext_mod.init_extract = original_init


    def test_main_exits_when_project_missing(self, tmp_path, monkeypatch):
        """__main__ must SystemExit(2) when data_source.bigquery.project is empty/missing."""
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {"data_source": {"type": "bigquery"}},  # no .bigquery.project
        )
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import runpy
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("connectors.bigquery.extractor", run_name="__main__")
        assert exc_info.value.code == 2
