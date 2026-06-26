"""Per-table connection_id resolution in _run_materialized_pass.

Two cases:
1. Table with connection_id=None uses global KEBOOLA_STORAGE_TOKEN
2. Table with connection_id="conn-123" uses that connection's URL+token
   (looked up via source_connections_repo)

Pattern matches tests/test_sync_trigger_keboola_materialized.py.
"""

import duckdb
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.sync_state import SyncStateRepository
from connectors.bigquery.access import BqAccess, BqProjects


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    monkeypatch.setattr(
        "app.api.sync.table_registry_repo",
        lambda: TableRegistryRepository(conn),
    )
    monkeypatch.setattr(
        "app.api.sync.sync_state_repo",
        lambda: SyncStateRepository(conn),
    )

    yield conn
    conn.close()


@pytest.fixture
def stub_bq():
    @contextmanager
    def _session(_p):
        conn = duckdb.connect(":memory:")
        try:
            yield conn
        finally:
            conn.close()

    return BqAccess(
        BqProjects(billing="t", data="t"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session,
    )


def test_global_token_used_when_no_connection_id(system_db, stub_bq, tmp_path, monkeypatch):
    """Table with connection_id=None must use global KEBOOLA_STORAGE_TOKEN env var."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="global_table",
        name="global_table",
        source_type="keboola",
        query_mode="materialized",
        bucket="in.c-global",
        source_table="data",
        source_query=None,
        sync_schedule="every 1m",
    )

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-token-abc")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.keboola.com/")

    def _fake_get_value(*keys, default=None):
        if keys == ("data_source", "keboola", "stack_url"):
            return "https://connection.keboola.com/"
        if keys == ("data_source", "keboola", "token_env"):
            return "KEBOOLA_STORAGE_TOKEN"
        if keys == ("data_source", "bigquery", "max_bytes_per_materialize"):
            return default if default is not None else 0
        return default

    parquet_dir = tmp_path / "data" / "extracts" / "keboola" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "global_table.parquet").write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")

    captured_clients = []

    def _fake_kb_client(*, url, token):
        client = MagicMock()
        client._url = url
        client._token = token
        captured_clients.append(client)
        return client

    kb_called = MagicMock(
        return_value={
            "table_id": "global_table",
            "rows": 5,
            "bytes": 200,
            "md5": "deadbeef",
            "path": str(parquet_dir / "global_table.parquet"),
        }
    )

    from app.api import sync as sync_mod

    with (
        patch("app.instance_config.get_value", _fake_get_value),
        patch("connectors.keboola.extractor.materialize_query", kb_called),
        patch("connectors.keboola.storage_api.KeboolaStorageClient", _fake_kb_client),
    ):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert "global_table" in summary["materialized"], f"expected global_table in materialized, got {summary}"
    assert len(captured_clients) == 1
    assert captured_clients[0]._token == "global-token-abc"
    assert captured_clients[0]._url == "https://connection.keboola.com/"


def test_connection_id_token_used_when_present(system_db, stub_bq, tmp_path, monkeypatch):
    """Table with connection_id='conn-123' must use that connection's URL+token."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="project_b_table",
        name="project_b_table",
        source_type="keboola",
        query_mode="materialized",
        bucket="in.c-project-b",
        source_table="events",
        source_query=None,
        sync_schedule="every 1m",
        connection_id="conn-123",
    )

    # Global token should NOT be used for this table.
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "global-should-not-be-used")

    def _fake_get_value(*keys, default=None):
        if keys == ("data_source", "bigquery", "max_bytes_per_materialize"):
            return default if default is not None else 0
        return default

    parquet_dir = tmp_path / "data" / "extracts" / "keboola" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "project_b_table.parquet").write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")

    # Stub source_connections_repo to return a connection record for conn-123.
    mock_conn_record = {
        "id": "conn-123",
        "name": "Project B",
        "source_type": "keboola",
        "config": {"stack_url": "https://connection.eu-west-1.keboola.com/"},
        "token_env": "PROJECT_B_TOKEN",
    }
    monkeypatch.setenv("PROJECT_B_TOKEN", "project-b-secret-token")

    mock_sc_repo = MagicMock()
    mock_sc_repo.get.return_value = mock_conn_record

    captured_clients = []

    def _fake_kb_client(*, url, token):
        client = MagicMock()
        client._url = url
        client._token = token
        captured_clients.append(client)
        return client

    kb_called = MagicMock(
        return_value={
            "table_id": "project_b_table",
            "rows": 10,
            "bytes": 500,
            "md5": "cafebabe",
            "path": str(parquet_dir / "project_b_table.parquet"),
        }
    )

    from app.api import sync as sync_mod

    with (
        patch("app.instance_config.get_value", _fake_get_value),
        patch("connectors.keboola.extractor.materialize_query", kb_called),
        patch("connectors.keboola.storage_api.KeboolaStorageClient", _fake_kb_client),
        patch("app.api.sync.source_connections_repo", return_value=mock_sc_repo),
    ):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert "project_b_table" in summary["materialized"], f"expected project_b_table in materialized, got {summary}"
    assert len(captured_clients) == 1, f"expected 1 client, got {len(captured_clients)}"
    # Must use the connection-specific URL, not the global one.
    assert captured_clients[0]._url == "https://connection.eu-west-1.keboola.com/"
    # Must use the connection-specific token from PROJECT_B_TOKEN env var.
    assert captured_clients[0]._token == "project-b-secret-token"

    # Must NOT have used the global token.
    assert captured_clients[0]._token != "global-should-not-be-used"


def test_missing_connection_id_produces_error(system_db, stub_bq, tmp_path, monkeypatch):
    """If connection_id is set but the record doesn't exist, error is recorded."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orphan_table",
        name="orphan_table",
        source_type="keboola",
        query_mode="materialized",
        bucket="in.c-orphan",
        source_table="data",
        source_query=None,
        sync_schedule="every 1m",
        connection_id="conn-missing",
    )

    def _fake_get_value(*keys, default=None):
        if keys == ("data_source", "bigquery", "max_bytes_per_materialize"):
            return default if default is not None else 0
        return default

    mock_sc_repo = MagicMock()
    mock_sc_repo.get.return_value = None  # not found

    from app.api import sync as sync_mod

    with (
        patch("app.instance_config.get_value", _fake_get_value),
        patch("app.api.sync.source_connections_repo", return_value=mock_sc_repo),
    ):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert "orphan_table" not in summary["materialized"]
    assert any(e["table"] == "orphan_table" for e in summary["errors"]), (
        f"expected error for orphan_table, got {summary['errors']}"
    )
    err_msg = next(e["error"] for e in summary["errors"] if e["table"] == "orphan_table")
    assert "conn-missing" in err_msg
