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


def test_run_sync_runs_materialized_pass_on_bq_only_deployment(
    tmp_path, monkeypatch,
):
    """REGRESSION (Devin BUG_0002 on 2fa44f2): on BigQuery-only deployments
    `list_local('bigquery')` is always empty (BQ rows are remote or
    materialized, never local). The pre-fix _run_sync early-returned in
    that case → materialized pass + orchestrator rebuild were dead code.
    Post-fix: run_extractor_subprocess flag skips just the Keboola
    subprocess, and the materialized pass still fires."""
    import duckdb
    from src.db import _ensure_schema

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    repo = TableRegistryRepository(conn)
    # Materialized BQ row — would be invisible to list_local('bigquery').
    repo.register(
        id="m1", name="m1", source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="every 1m",
    )
    conn.close()

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    # Patch the heavy collaborators so we observe what _run_sync invoked
    # without actually running BQ / orchestrator.
    from app.api import sync as sync_mod

    materialized_called = {"count": 0}
    orchestrator_called = {"count": 0}

    def _spy_materialized_pass(_conn, _bq):
        materialized_called["count"] += 1
        return {"materialized": ["m1"], "skipped": [], "errors": []}

    class _OrchStub:
        def rebuild(self):
            orchestrator_called["count"] += 1
            return {}

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        _spy_materialized_pass,
    )
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: _OrchStub(),
    )
    # Pretend instance.yaml says data_source.type=bigquery
    monkeypatch.setattr(
        "app.instance_config.get_data_source_type",
        lambda: "bigquery",
    )
    # bq_project must be truthy so the materialized pass branch fires.
    real_get_value = sync_mod.__dict__.get("get_value")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: (
            "my-bq-proj" if (args and args[-1] == "project")
            else kw.get("default", "")
        ),
    )

    sync_mod._run_sync()

    assert materialized_called["count"] == 1, (
        "materialized pass must run on BQ-only deployment (no local rows)"
    )
    assert orchestrator_called["count"] == 1, (
        "orchestrator rebuild must run so materialized parquets are picked up"
    )


@pytest.mark.parametrize("yaml_value, expected_max", [
    (10737418240, 10737418240),       # int — canonical
    (10737418240.0, 10737418240),     # float — YAML often parses as float
    (1e10, 10000000000),              # scientific notation
    ("10737418240", 10737418240),     # string — coerced
    (0, None),                        # explicit disable sentinel
    (None, None),                     # missing key
    ("not-a-number", None),           # malformed → fail-open + warn
])
def test_materialized_pass_max_bytes_yaml_coercion(
    system_db, stub_bq, tmp_path, monkeypatch, yaml_value, expected_max,
):
    """`max_bytes_per_materialize` YAML value is coerced to int regardless of
    the YAML scalar type (int / float / scientific / string). Devin found
    that an `isinstance(raw, int)` guard silently disabled the guardrail
    on float values."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="t", name="t", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
        sync_schedule="every 1m",
    )

    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "t.parquet").write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")

    captured = {}

    def _spy(table_id, sql, bq, output_dir, max_bytes):
        captured["max_bytes"] = max_bytes
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    from app.api import sync as sync_mod

    with patch(
        "app.instance_config.get_value",
        side_effect=lambda *a, **kw: (
            yaml_value if a[-1] == "max_bytes_per_materialize"
            else kw.get("default", "")
        ),
    ), patch("app.api.sync._materialize_table", side_effect=_spy):
        sync_mod._run_materialized_pass(system_db, stub_bq)

    assert captured["max_bytes"] == expected_max


def test_materialized_pass_keys_sync_state_by_name_not_id(
    system_db, stub_bq, tmp_path,
):
    """Devin review: when admin registers a name with mixed case (e.g.
    "Orders_90d") the slug-derived id ("orders_90d") differs from name.
    sync_state must be keyed by `name` so the manifest's `registry_by_name`
    lookup resolves and `query_mode='materialized'` flows through to the
    client. Otherwise CLI sees `query_mode='local'` and downloads the
    wrong file or skips the row."""
    repo = TableRegistryRepository(system_db)
    # Mixed-case name — id will be slugified to lowercase by the API path,
    # but at the repo level we control both directly.
    repo.register(
        id="orders_90d", name="Orders_90d",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="every 1m",
    )

    # Pre-create the parquet at the NAME-keyed path.
    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "Orders_90d.parquet").write_bytes(
        b"PAR1" + b"\x00" * 16 + b"PAR1"
    )

    from app.api import sync as sync_mod

    captured = {}

    def _spy(table_id, sql, bq, output_dir, max_bytes):
        captured["table_id"] = table_id
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    with patch("app.api.sync._materialize_table", side_effect=_spy):
        sync_mod._run_materialized_pass(system_db, stub_bq)

    # materialize_query was called with the NAME, not the id.
    assert captured["table_id"] == "Orders_90d"

    # sync_state row keyed by name.
    state = SyncStateRepository(system_db)
    name_row = state.get_table_state("Orders_90d")
    id_row = state.get_table_state("orders_90d")
    assert name_row is not None, "sync_state should be keyed by name"
    assert id_row is None, "sync_state should NOT be keyed by id"


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
