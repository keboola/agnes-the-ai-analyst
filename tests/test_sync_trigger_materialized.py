"""_run_materialized_pass walks table_registry for materialized BQ rows
and runs each that is due via _materialize_table.

Tests inject a stub BqAccess (factories never called by these tests since
_materialize_table is patched) and assert that scheduling, error
aggregation, sync_state hash, and the disable-sentinel all behave
correctly.
"""
import duckdb
import pytest
from contextlib import contextmanager
from pathlib import Path
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
    yield conn
    conn.close()


@pytest.fixture
def stub_bq():
    """A BqAccess instance that the tests don't actually exercise (the test
    patches `_materialize_table`); just needs to be a valid BqAccess so the
    type contract doesn't break."""
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


def test_materialized_pass_calls_materialize_for_due_rows(system_db, stub_bq, tmp_path):
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orders_90d", name="orders_90d",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1 AS n",
        sync_schedule="every 1m",  # always due in tests (no prior sync)
    )

    # Pre-create the parquet so _file_hash returns non-empty
    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "orders_90d.parquet").write_bytes(
        b"PAR1" + b"\x00" * 16 + b"PAR1"
    )

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        mock_mat.return_value = {
            "rows": 1, "size_bytes": 100, "query_mode": "materialized",
        }
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    mock_mat.assert_called_once()
    call_kwargs = mock_mat.call_args.kwargs
    assert call_kwargs["table_id"] == "orders_90d"
    assert "SELECT 1 AS n" in call_kwargs["sql"]
    assert call_kwargs["bq"] is stub_bq
    # Default cap (10 GiB) flows through when no instance.yaml override
    assert call_kwargs["max_bytes"] == 10 * 2**30
    assert "orders_90d" in summary["materialized"]
    assert not summary["errors"]


def test_materialized_pass_skips_undue_rows(system_db, stub_bq):
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orders_daily", name="orders_daily",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="daily 03:00",
    )
    state = SyncStateRepository(system_db)
    state.update_sync(
        table_id="orders_daily", rows=1, file_size_bytes=10, hash="x",
    )

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    mock_mat.assert_not_called()
    assert "orders_daily" in summary["skipped"]


def test_materialized_pass_skips_non_materialized_rows(system_db, stub_bq):
    repo = TableRegistryRepository(system_db)
    repo.register(id="t1", name="t1", source_type="keboola", query_mode="local")
    repo.register(id="t2", name="t2", source_type="bigquery", query_mode="remote")

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    mock_mat.assert_not_called()
    assert summary == {"materialized": [], "skipped": [], "errors": []}


def test_materialized_pass_collects_errors_per_row(system_db, stub_bq, tmp_path):
    """One row failing must not stop a healthy sibling."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="ok", name="ok", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
        sync_schedule="every 1m",
    )
    repo.register(
        id="bad", name="bad", source_type="bigquery",
        query_mode="materialized", source_query="SELECT broken",
        sync_schedule="every 1m",
    )

    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "ok.parquet").write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")

    from app.api import sync as sync_mod

    def _fake(table_id, sql, bq, output_dir, max_bytes):
        if table_id == "bad":
            raise RuntimeError("simulated COPY failure")
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    with patch("app.api.sync._materialize_table", side_effect=_fake):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert summary["materialized"] == ["ok"]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "bad"
    assert "simulated" in summary["errors"][0]["error"]


def test_materialized_pass_records_parquet_hash(system_db, stub_bq, tmp_path):
    """sync_state.hash must be the MD5 of the parquet file — otherwise the
    manifest reports an empty hash and every da sync re-downloads."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="hashed", name="hashed",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="every 1m",
    )

    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = parquet_dir / "hashed.parquet"

    def _fake(**kwargs):
        parquet_path.write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")
        return {"rows": 1, "size_bytes": 24, "query_mode": "materialized"}

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table", side_effect=_fake):
        sync_mod._run_materialized_pass(system_db, stub_bq)

    state = SyncStateRepository(system_db)
    row = state.get_table_state("hashed")
    assert row is not None
    import hashlib
    expected = hashlib.md5(b"PAR1" + b"\x00" * 16 + b"PAR1").hexdigest()
    assert row["hash"] == expected


def test_materialized_pass_zero_max_bytes_disables_guardrail(
    system_db, stub_bq, tmp_path, monkeypatch
):
    """`max_bytes_per_materialize: 0` in instance.yaml → None passed downstream
    so materialize_query skips the dry-run entirely."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="big", name="big", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
        sync_schedule="every 1m",
    )

    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "big.parquet").write_bytes(
        b"PAR1" + b"\x00" * 16 + b"PAR1"
    )

    monkeypatch.setattr(
        "app.api.sync.get_value",
        lambda *args, **kwargs: 0 if args[-1] == "max_bytes_per_materialize" else "",
        raising=False,
    )

    from app.api import sync as sync_mod

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    # The function reads `get_value` via a local import in the body — patch
    # the import target instead.
    with patch(
        "app.instance_config.get_value",
        side_effect=lambda *args, **kw: (
            0 if args[-1] == "max_bytes_per_materialize"
            else kw.get("default", "")
        ),
    ), patch("app.api.sync._materialize_table", side_effect=_spy):
        sync_mod._run_materialized_pass(system_db, stub_bq)

    assert captured["max_bytes"] is None
