"""Per-source partial-rebuild filtering (issue #327).

`POST /api/sync/trigger?source=<type>` (and `agnes admin sync --source <type>`)
scope a rebuild to a single registered ``source_type``: only that source's
local + materialized rows are rebuilt, leaving the other source's
``extract.duckdb`` untouched. A bare trigger rebuilds everything.

Three layers:
  1. `_run_materialized_pass(..., source_type=...)` only materializes rows
     whose registry source_type matches.
  2. `_run_sync(source_type_filter=...)` end-to-end: with one Keboola local
     row + one BQ materialized row, `source='bigquery'` rebuilds only the
     BQ row and leaves the Keboola extract.duckdb mtime unchanged; a bare
     sync rebuilds both.
  3. Endpoint + CLI passthrough scope identically.
"""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from app.api import sync as sync_module
from connectors.bigquery.access import BqAccess, BqProjects
from src.db import _ensure_schema
from src.repositories.sync_state import SyncStateRepository
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture(autouse=True)
def reset_sync_lock():
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    sync_module._recent_trigger_at = 0.0
    yield
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    sync_module._recent_trigger_at = 0.0


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


# ---- Layer 1: _run_materialized_pass source filter -------------------------


def _seed_dual_source(conn, tmp_path):
    """Register one Keboola materialized row + one BQ materialized row, both
    due, and pre-create both parquet files so the hash step succeeds."""
    repo = TableRegistryRepository(conn)
    repo.register(
        id="kbc_orders",
        name="kbc_orders",
        source_type="keboola",
        query_mode="materialized",
        bucket="in.c-foo",
        source_table="orders",
        source_query=None,
        sync_schedule="every 1m",
    )
    repo.register(
        id="bq_sessions",
        name="bq_sessions",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT 1 AS n",
        sync_schedule="every 1m",
    )
    for source, fname in (("keboola", "kbc_orders"), ("bigquery", "bq_sessions")):
        d = tmp_path / "data" / "extracts" / source / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{fname}.parquet").write_bytes(b"PAR1" + b"\x00" * 16 + b"PAR1")


def test_materialized_pass_source_filter_scopes_to_bigquery(tmp_path, monkeypatch, stub_bq):
    """`source_type='bigquery'` materializes only the BQ row; the Keboola
    materialized row is skipped with reason='source_filter'."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
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
    _seed_dual_source(conn, tmp_path)

    materialized = []

    def _fake_bq(table_id, sql, bq, output_dir, max_bytes, fetch_timeout_s=None):
        materialized.append(table_id)
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    try:
        with patch("app.api.sync._materialize_table", side_effect=_fake_bq):
            summary = sync_module._run_materialized_pass(
                conn,
                stub_bq,
                source_type="bigquery",
            )
    finally:
        conn.close()

    assert materialized == ["bq_sessions"], "only the BQ materialized row should be rebuilt under source='bigquery'"
    assert summary["materialized"] == ["bq_sessions"]
    assert {"table": "kbc_orders", "reason": "source_filter"} in summary["skipped"]


def test_materialized_pass_no_filter_processes_all(tmp_path, monkeypatch, stub_bq):
    """No source filter → both materialized rows are processed (BQ via
    _materialize_table, Keboola via the storage-client path)."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://example.invalid")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "fake")
    monkeypatch.setattr(
        "app.api.sync.table_registry_repo",
        lambda: TableRegistryRepository(conn),
    )
    monkeypatch.setattr(
        "app.api.sync.sync_state_repo",
        lambda: SyncStateRepository(conn),
    )
    _seed_dual_source(conn, tmp_path)

    seen = []

    def _fake_bq(table_id, sql, bq, output_dir, max_bytes, fetch_timeout_s=None):
        seen.append(("bq", table_id))
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    def _fake_kb(**kwargs):
        seen.append(("kb", kwargs["table_id"]))
        return {"table_id": kwargs["table_id"], "path": "x", "rows": 1, "bytes": 100, "md5": "deadbeef"}

    try:
        with (
            patch("app.api.sync._materialize_table", side_effect=_fake_bq),
            patch(
                "connectors.keboola.extractor.materialize_query",
                side_effect=_fake_kb,
            ),
        ):
            summary = sync_module._run_materialized_pass(conn, stub_bq)
    finally:
        conn.close()

    assert ("bq", "bq_sessions") in seen
    assert ("kb", "kbc_orders") in seen
    assert sorted(summary["materialized"]) == ["bq_sessions", "kbc_orders"]


# ---- Layer 2: _run_sync end-to-end with mtime isolation --------------------


def _run_sync_harness(tmp_path, monkeypatch):
    """Set up `_run_sync` to run against a real registry with one Keboola
    local row + one BQ materialized row. Returns a dict of spies +
    the path of a pre-created keboola extract.duckdb so the caller can
    assert its mtime is unchanged."""
    import src.db as _db_mod

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://example.invalid")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "fake")

    # Register both rows through the same get_system_db() singleton that
    # `_run_sync`'s repo factory uses, then close it so `_run_sync` re-opens
    # the same on-disk DB cleanly.
    conn = _db_mod.get_system_db()
    repo = TableRegistryRepository(conn)
    repo.register(
        id="kbc_orders",
        name="kbc_orders",
        source_type="keboola",
        query_mode="local",
        bucket="in.c-foo",
        source_table="orders",
    )
    repo.register(
        id="bq_sessions",
        name="bq_sessions",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT 1 AS n",
        sync_schedule="every 1m",
    )
    _db_mod.close_system_db()
    # Dual-source deployment: the instance reports keboola as its primary
    # source, but the registry carries both. The `?source=` filter is what
    # scopes a partial rebuild.
    monkeypatch.setattr(
        "app.instance_config.get_data_source_type",
        lambda: "keboola",
    )
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: "my-bq-proj" if (args and args[-1] == "project") else kw.get("default", ""),
    )

    # Pre-create a real keboola extract.duckdb on disk; the Keboola extractor
    # subprocess is what would normally rewrite it. We assert it stays
    # untouched under a BQ-scoped rebuild.
    kbc_dir = tmp_path / "data" / "extracts" / "keboola"
    kbc_dir.mkdir(parents=True, exist_ok=True)
    kbc_extract = kbc_dir / "extract.duckdb"
    kbc_extract.write_bytes(b"existing-keboola-extract")
    # Backdate so a no-rewrite is unambiguous against filesystem mtime
    # resolution.
    old = 1_000_000_000
    os.utime(kbc_extract, (old, old))

    spies = {"keboola_subprocess": 0, "materialized": []}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            spies["keboola_subprocess"] += 1
            self.cmd = cmd
            self.returncode = 0
            self.pid = 999

        def communicate(self, input=None, timeout=None):
            return ("{}", "")

    monkeypatch.setattr(sync_module.subprocess, "Popen", _FakePopen)

    def _spy_materialized(_conn, _bq, *, tables=None, source_type=None):
        # Use the connection `_run_sync` already passed (single-writer).
        repo2 = TableRegistryRepository(_conn)
        for row in repo2.list_all():
            if row.get("query_mode") != "materialized":
                continue
            rst = row.get("source_type") or "bigquery"
            if source_type is not None and rst != source_type:
                continue
            spies["materialized"].append(row["name"])
        return {"materialized": list(spies["materialized"]), "skipped": [], "errors": []}

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        _spy_materialized,
    )

    class _OrchStub:
        def rebuild(self):
            # Reads extract.duckdb files read-only; never rewrites them.
            return {}

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: _OrchStub(),
    )

    return spies, kbc_extract


def test_run_sync_bigquery_filter_skips_keboola_extract(tmp_path, monkeypatch):
    """source='bigquery': only the BQ materialized row rebuilds; the Keboola
    extractor subprocess never runs, so keboola/extract.duckdb mtime is
    unchanged."""
    spies, kbc_extract = _run_sync_harness(tmp_path, monkeypatch)
    mtime_before = kbc_extract.stat().st_mtime

    sync_module._run_sync(tables=None, source_type_filter="bigquery")

    assert spies["keboola_subprocess"] == 0, "Keboola extractor subprocess must NOT run under source='bigquery'"
    assert spies["materialized"] == ["bq_sessions"], "only the BQ materialized row should be rebuilt"
    assert kbc_extract.stat().st_mtime == mtime_before, (
        "keboola/extract.duckdb mtime must be unchanged by a BQ-scoped rebuild"
    )


def test_run_sync_no_filter_rebuilds_both(tmp_path, monkeypatch):
    """Bare sync (no source filter): the Keboola extractor subprocess runs
    AND the BQ materialized row is rebuilt. A clean run with no per-table
    errors reports success (`True`) — the honest outcome the wave-2B
    `data-refresh` job path relies on to decide `done` vs `failed`."""
    spies, _kbc_extract = _run_sync_harness(tmp_path, monkeypatch)

    result = sync_module._run_sync(tables=None)

    assert spies["keboola_subprocess"] == 1, "Keboola extractor subprocess must run on a full sweep"
    assert spies["materialized"] == ["bq_sessions"], "the BQ materialized row must also be rebuilt on a full sweep"
    assert result is True, "a clean run with no collected errors must report success"


def test_run_sync_returns_false_on_per_table_errors(tmp_path, monkeypatch):
    """A per-table failure (materialized pass reports an error, no fatal
    exception) must still make `_run_sync` report `False`. Before the
    wave-2B honesty fix, this case was swallowed entirely (logged +
    best-effort webhook notify) and the function returned nothing — a
    `data-refresh` job built on top would have finalized 'done' even
    though a table failed."""
    _run_sync_harness(tmp_path, monkeypatch)

    def _materialized_with_error(_conn, _bq, *, tables=None, source_type=None):
        return {"materialized": [], "skipped": [], "errors": [{"table": "bq_sessions", "error": "boom"}]}

    monkeypatch.setattr("app.api.sync._run_materialized_pass", _materialized_with_error)

    result = sync_module._run_sync(tables=None)

    assert result is False, "a per-table failure must be reported as an unsuccessful run"


# ---- Layer 3: endpoint + CLI passthrough -----------------------------------


def _make_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.access import require_admin

    app = FastAPI()
    app.include_router(sync_module.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "t", "email": "t@e"}
    return TestClient(app)


class _FakeJobsRepo:
    """Minimal stand-in for `jobs_repo()` — no in-flight job, so every
    `enqueue()` call looks like a fresh trigger. See
    `tests/test_sync_trigger_singleton.py` for the fuller dedup-aware
    version; this file only needs to assert what payload reaches
    `enqueue()`, not the dedup branch."""

    def __init__(self):
        self.enqueue_calls: list[dict] = []

    def list(self, *, kind=None, status=None, limit=50):
        return []

    def enqueue(self, kind, payload, *, idempotency_key=None, **kwargs):
        self.enqueue_calls.append({"kind": kind, "payload": payload, "idempotency_key": idempotency_key})
        return {"id": "fake-job-id", "kind": kind, "status": "queued", "idempotency_key": idempotency_key}


def test_trigger_threads_source_into_run_sync():
    """`?source=bigquery` reaches the enqueued `data-refresh` job's payload
    as `source`."""
    client = _make_client()
    fake_repo = _FakeJobsRepo()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger?source=bigquery")
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "bigquery"
    assert fake_repo.enqueue_calls == [
        {"kind": "data-refresh", "payload": {"tables": None, "source": "bigquery"}, "idempotency_key": "sync"}
    ]


def test_trigger_source_is_normalized_lowercase():
    """Source is normalized (trim + lowercase) before validation/dispatch."""
    client = _make_client()
    fake_repo = _FakeJobsRepo()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger?source=BigQuery")
    assert resp.status_code == 200, resp.text
    assert fake_repo.enqueue_calls[0]["payload"]["source"] == "bigquery"


def test_trigger_rejects_unknown_source():
    """An unknown source_type fails fast with 422 — never silently rebuilds
    nothing, and never reaches the job queue."""
    client = _make_client()
    fake_repo = _FakeJobsRepo()
    with patch("app.api.sync.jobs_repo", lambda: fake_repo):
        resp = client.post("/api/sync/trigger?source=snowflake")
    assert resp.status_code == 422, resp.text
    assert not fake_repo.enqueue_calls


def test_cli_admin_sync_passes_source_query_param():
    """`agnes admin sync --source bigquery` posts to /api/sync/trigger with
    `?source=bigquery` and no body — scoping identically to the endpoint."""
    from cli.commands import admin as admin_cli

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "triggered", "tables": "all", "source": "bigquery"}

    def _fake_post(path, **kwargs):
        captured["path"] = path
        captured["params"] = kwargs.get("params")
        captured["json"] = kwargs.get("json")
        return _Resp()

    with patch.object(admin_cli, "api_post", side_effect=_fake_post):
        admin_cli.sync(source="bigquery", tables=None, as_json=False)

    assert captured["path"] == "/api/sync/trigger"
    assert captured["params"] == {"source": "bigquery"}
    assert captured["json"] is None


def test_cli_admin_sync_full_sweep_sends_no_source():
    """No `--source` → no query param (full sweep)."""
    from cli.commands import admin as admin_cli

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "triggered", "tables": "all", "source": "all"}

    def _fake_post(path, **kwargs):
        captured["params"] = kwargs.get("params")
        captured["json"] = kwargs.get("json")
        return _Resp()

    with patch.object(admin_cli, "api_post", side_effect=_fake_post):
        admin_cli.sync(source=None, tables=None, as_json=False)

    assert captured["params"] is None
    assert captured["json"] is None
