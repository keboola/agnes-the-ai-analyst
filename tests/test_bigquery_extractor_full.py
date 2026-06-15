"""Full tests for the BigQuery extractor connector."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from tests.helpers.contract import validate_extract_contract


# ---------------------------------------------------------------------------
# Helpers for cache-first entity_type tests
# ---------------------------------------------------------------------------


def _make_cache_row(entity_type: str) -> dict:
    """Build a minimal bq_metadata_cache row dict with the given entity_type."""
    return {
        "table_id": "placeholder",
        "rows": 1000,
        "size_bytes": 512000,
        "partition_by": None,
        "clustered_by": None,
        "entity_type": entity_type,
        "known_columns": None,
        "refreshed_at": None,
        "error_at": None,
        "error_msg": None,
    }


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "bigquery"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def sample_configs():
    return [
        {
            "id": "proj.analytics.orders",
            "name": "orders",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
            "description": "Order data from BQ",
        },
        {
            "id": "proj.analytics.sessions",
            "name": "sessions",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "sessions",
            "query_mode": "remote",
            "description": "Session data from BQ",
        },
    ]


class _DuckDBProxy:
    """Proxy around a real DuckDB connection that intercepts BigQuery extension SQL."""

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("INSTALL BIGQUERY") or sql_upper.startswith("LOAD BIGQUERY"):
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
                self._real.execute(f'CREATE OR REPLACE TABLE "{view_name}" (dummy INTEGER)')
                return MagicMock()
        # bigquery_query() table function — stub as a CREATE TABLE so the view persists
        if "BIGQUERY_QUERY(" in sql_upper and "CREATE" in sql_upper:
            match = re.search(r'VIEW\s+"?(\w+)"?', sql, re.IGNORECASE)
            if match:
                view_name = match.group(1)
                self._real.execute(f'CREATE OR REPLACE TABLE "{view_name}" (dummy INTEGER)')
                return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _proxy_connect(path=None, **kwargs):
    real_conn = duckdb.connect(path)
    return _DuckDBProxy(real_conn)


@pytest.fixture
def mock_bq_auth_and_detect(monkeypatch):
    """Stub metadata-token auth + entity-type detection so init_extract runs offline."""
    monkeypatch.setattr(
        "connectors.bigquery.extractor.get_metadata_token",
        lambda: "test-token",
    )
    monkeypatch.setattr(
        "connectors.bigquery.extractor._detect_table_type",
        lambda *a, **kw: "BASE TABLE",
    )


@pytest.mark.usefixtures("mock_bq_auth_and_detect")
class TestBigQueryExtractorFull:
    def test_init_extract_creates_contract_compliant_db(self, output_dir, sample_configs):
        """init_extract() creates extract.duckdb that passes contract validation."""
        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-gcp-project", sample_configs)

        assert result["tables_registered"] == 2
        assert result["errors"] == []

        db_path = str(Path(output_dir) / "extract.duckdb")
        validate_extract_contract(db_path)

    def test_remote_attach_table_has_correct_values(self, output_dir, sample_configs):
        """_remote_attach row must have alias=bq, extension=bigquery, url=project=<id>, token_env=''."""
        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            init_extract(output_dir, "acme-project", sample_configs)

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        ra = conn.execute("SELECT alias, extension, url, token_env FROM _remote_attach").fetchone()
        conn.close()

        assert ra[0] == "bq"
        assert ra[1] == "bigquery"
        assert ra[2] == "project=acme-project"
        assert ra[3] == ""  # BigQuery uses GOOGLE_APPLICATION_CREDENTIALS, not token_env

    def test_all_tables_have_remote_query_mode(self, output_dir, sample_configs):
        """All BigQuery tables must have query_mode='remote' in _meta."""
        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            init_extract(output_dir, "my-project", sample_configs)

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        modes = conn.execute("SELECT DISTINCT query_mode FROM _meta").fetchall()
        conn.close()

        assert len(modes) == 1
        assert modes[0][0] == "remote"

    def test_no_data_directory_created(self, output_dir, sample_configs):
        """BigQuery is remote-only — no data/ directory should be created."""
        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            init_extract(output_dir, "my-project", sample_configs)

        assert not (Path(output_dir) / "data").exists()

    def test_meta_table_schema(self, output_dir):
        """_meta table must have the exact contract-required columns."""
        from connectors.bigquery.extractor import _create_meta_table

        db_path = Path(output_dir) / "schema_check.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_meta_table(conn)
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='_meta' ORDER BY ordinal_position"
        ).fetchall()
        conn.close()

        assert [c[0] for c in cols] == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"]

    def test_remote_attach_table_schema(self, output_dir):
        """_remote_attach table must have the exact contract-required columns."""
        from connectors.bigquery.extractor import _create_remote_attach_table

        db_path = Path(output_dir) / "ra_check.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_remote_attach_table(conn, "test-project")
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='_remote_attach' ORDER BY ordinal_position"
        ).fetchall()
        conn.close()

        assert [c[0] for c in cols] == ["alias", "extension", "url", "token_env"]

    def test_table_registration_failure_records_error(self, output_dir):
        """A failed table registration records the error but continues others."""
        configs = [
            {"name": "good", "bucket": "ds", "source_table": "good", "query_mode": "remote", "description": ""},
            {"name": "bad", "bucket": "ds", "source_table": "bad", "query_mode": "remote", "description": ""},
        ]

        call_count = [0]

        class FailingProxy(_DuckDBProxy):
            def execute(self, sql, *args, **kwargs):
                sql_upper = sql.strip().upper()
                # Intercept: fail view creation for 'bad'
                if "FROM BQ." in sql_upper and "CREATE" in sql_upper and '"bad"' in sql.lower():
                    call_count[0] += 1
                    raise Exception("Table not found in BigQuery")
                return super().execute(sql, *args, **kwargs)

        def failing_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return FailingProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = failing_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", configs)

        assert result["tables_registered"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["table"] == "bad"

    def test_empty_table_list(self, output_dir):
        """init_extract with no tables still creates a valid (empty) extract.duckdb."""
        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", [])

        assert result["tables_registered"] == 0
        assert result["errors"] == []

        db_path = Path(output_dir) / "extract.duckdb"
        assert db_path.exists()
        conn = duckdb.connect(str(db_path), read_only=True)
        count = conn.execute("SELECT count(*) FROM _meta").fetchone()[0]
        conn.close()
        assert count == 0


class TestCacheFirstEntityType:
    """Verify that init_extract uses bq_metadata_cache for entity_type when available.

    On a warm cache the live _detect_table_type BQ round-trip must be skipped
    entirely so that O(N) BQ jobs-API calls don't saturate the box during
    back-to-back rebuilds.
    """

    def _configs_with_ids(self):
        return [
            {
                "id": "proj.analytics.orders",
                "name": "orders",
                "source_type": "bigquery",
                "bucket": "analytics",
                "source_table": "orders",
                "query_mode": "remote",
                "description": "Orders",
            },
            {
                "id": "proj.analytics.sessions",
                "name": "sessions",
                "source_type": "bigquery",
                "bucket": "analytics",
                "source_table": "sessions",
                "query_mode": "remote",
                "description": "Sessions",
            },
        ]

    def test_warm_cache_skips_live_detect(self, output_dir, monkeypatch):
        """When cache has entity_type for all tables, _detect_table_type is never called."""
        detect_calls = []

        def _fail_if_called(*args, **kwargs):
            detect_calls.append(args)
            raise AssertionError("_detect_table_type must not be called when cache is warm")

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _fail_if_called,
        )

        cache = {
            "proj.analytics.orders": _make_cache_row("BASE TABLE"),
            "proj.analytics.sessions": _make_cache_row("BASE TABLE"),
        }

        mock_repo = MagicMock()
        mock_repo.get.side_effect = lambda table_id: cache.get(table_id)

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            lambda: mock_repo,
        )

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", self._configs_with_ids())

        assert result["tables_registered"] == 2
        assert result["errors"] == []
        assert detect_calls == [], "live _detect_table_type must not be called on warm cache"

    def test_cache_miss_falls_back_to_live_detect(self, output_dir, monkeypatch):
        """When cache returns None for a table, falls back to live _detect_table_type."""
        detect_calls = []

        def _live_detect(*args, **kwargs):
            detect_calls.append(args)
            return "BASE TABLE"

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _live_detect,
        )

        # Cache only has orders, sessions is a miss
        cache = {
            "proj.analytics.orders": _make_cache_row("BASE TABLE"),
        }

        mock_repo = MagicMock()
        mock_repo.get.side_effect = lambda table_id: cache.get(table_id)

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            lambda: mock_repo,
        )

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", self._configs_with_ids())

        assert result["tables_registered"] == 2
        assert result["errors"] == []
        # Live detect called exactly once — for the sessions cache miss
        assert len(detect_calls) == 1

    def test_repo_unavailable_falls_back_to_live_detect(self, output_dir, monkeypatch):
        """When the cache repo raises (e.g. no system DB), falls back gracefully."""
        detect_calls = []

        def _live_detect(*args, **kwargs):
            detect_calls.append(args)
            return "BASE TABLE"

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _live_detect,
        )

        def _failing_repo():
            raise RuntimeError("no system DB in standalone context")

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            _failing_repo,
        )

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", self._configs_with_ids())

        assert result["tables_registered"] == 2
        assert result["errors"] == []
        # Live detect called for all 2 tables when repo is unavailable
        assert len(detect_calls) == 2

    def test_materialized_view_space_format_normalized(self, output_dir, monkeypatch):
        """Cache stores 'MATERIALIZED VIEW' (space, BQ canonical); extractor
        normalizes it to 'MATERIALIZED_VIEW' so the VIEW branch still fires."""
        detect_calls = []

        def _fail_if_called(*args, **kwargs):
            detect_calls.append(args)
            raise AssertionError("_detect_table_type must not be called when cache is warm")

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _fail_if_called,
        )

        # Cache stores BQ-canonical "MATERIALIZED VIEW" with a space
        cache = {
            "proj.analytics.orders": _make_cache_row("MATERIALIZED VIEW"),
        }

        mock_repo = MagicMock()
        mock_repo.get.side_effect = lambda table_id: cache.get(table_id)

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            lambda: mock_repo,
        )

        configs = [
            {
                "id": "proj.analytics.orders",
                "name": "orders",
                "source_type": "bigquery",
                "bucket": "analytics",
                "source_table": "orders",
                "query_mode": "remote",
                "description": "Orders",
            }
        ]

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", configs)

        # Must register via the VIEW/bigquery_query path, not be skipped as unknown
        assert result["tables_registered"] == 1
        assert result["errors"] == []
        assert detect_calls == [], "live detect must not be called for cached MATERIALIZED VIEW"

        # Confirm the inner view exists (as a dummy TABLE from _proxy_connect)
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path), read_only=True)
        row = conn.execute("SELECT table_name FROM _meta WHERE table_name = 'orders'").fetchone()
        conn.close()
        assert row is not None, "orders must appear in _meta"

    def test_live_fallback_materialized_view_normalized(self, output_dir, monkeypatch):
        """On cache miss the live _detect_table_type returns the raw BQ value
        'MATERIALIZED VIEW' (space). It must be normalized to 'MATERIALIZED_VIEW'
        so the VIEW branch fires and the table is registered, not skipped as an
        unverified entity type."""
        detect_calls = []

        def _live_detect(*args, **kwargs):
            detect_calls.append(args)
            return "MATERIALIZED VIEW"  # raw BQ INFORMATION_SCHEMA form (space)

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _live_detect,
        )

        # Cache miss for everything → forces the live fallback path.
        mock_repo = MagicMock()
        mock_repo.get.return_value = None

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            lambda: mock_repo,
        )

        configs = [
            {
                "id": "proj.analytics.orders",
                "name": "orders",
                "source_type": "bigquery",
                "bucket": "analytics",
                "source_table": "orders",
                "query_mode": "remote",
                "description": "Orders",
            }
        ]

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", configs)

        # Registered via the VIEW/bigquery_query path, not skipped as unknown.
        assert result["tables_registered"] == 1
        assert result["errors"] == []
        assert len(detect_calls) == 1, "live detect must run on cache miss"

    def test_no_table_id_falls_back_to_live_detect(self, output_dir, monkeypatch):
        """When tc has no 'id' key, cache lookup is skipped and live detect runs."""
        detect_calls = []

        def _live_detect(*args, **kwargs):
            detect_calls.append(args)
            return "BASE TABLE"

        monkeypatch.setattr(
            "connectors.bigquery.extractor.get_metadata_token",
            lambda: "test-token",
        )
        monkeypatch.setattr(
            "connectors.bigquery.extractor._detect_table_type",
            _live_detect,
        )

        mock_repo = MagicMock()
        mock_repo.get.return_value = _make_cache_row("BASE TABLE")

        monkeypatch.setattr(
            "connectors.bigquery.extractor.bq_metadata_cache_repo",
            lambda: mock_repo,
        )

        # Configs WITHOUT 'id' key (legacy path)
        configs_no_id = [
            {
                "name": "orders",
                "bucket": "analytics",
                "source_table": "orders",
                "query_mode": "remote",
                "description": "Orders",
            }
        ]

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = _proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", configs_no_id)

        assert result["tables_registered"] == 1
        assert result["errors"] == []
        # Live detect called because no 'id' to key the cache lookup
        assert len(detect_calls) == 1
