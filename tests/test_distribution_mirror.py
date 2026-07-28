"""Tests for the ``distribution-mirror`` LIGHT job kind (three-plane
wave 2-H, WS F, task WF-3 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

Covers:

- the mirror handler (``app.worker.kinds._run_distribution_mirror``):
  uploads changed parquets, skips md5-matches, skips ``remote``/
  ``server_only`` rows, writes the marker index, clean no-op when
  ``object_store()`` is ``None`` (never imports ``boto3``);
- the marker-index helpers (``src.distribution.write_mirror_index`` /
  ``read_mirror_index``): round-trip + fail-open on store error;
- registration: ``distribution-mirror`` is a LIGHT-lane kind, alongside the
  other seven real kinds (worker-role gating is generic — see
  ``app/main.py``'s ``role_enabled(Role.WORKER)`` guard around the whole
  worker loop — so there is nothing kind-specific to gate here beyond
  correct lane registration);
- the ``data-refresh`` → ``distribution-mirror`` chain-enqueue, gated on
  ``object_store()`` being configured and the sync having actually run
  (not a no-op).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.object_store_fakes import FakeObjectStore


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def mirror_env(tmp_path, monkeypatch):
    """A fresh system.duckdb (DATA_DIR-scoped) with a small table_registry +
    sync_state fixture:

    - ``orders`` — query_mode=local, keboola, on-disk parquet, synced.
    - ``sales_report`` — query_mode=materialized, on-disk parquet, synced.
    - ``bq_view`` — query_mode=remote — never has a local parquet.
    - ``internal_report`` — query_mode=local, server_only=True — has a
      parquet on disk (server keeps it fresh) but must never be mirrored.

    Returns the ``tmp_path`` DATA_DIR so tests can inspect/mutate parquet
    bytes directly.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)

    from src.db import close_system_db, get_system_db
    from src.repositories import sync_state_repo, table_registry_repo

    get_system_db()

    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)

    orders_bytes = b"orders-parquet-v1"
    sales_bytes = b"sales-report-parquet-v1"
    internal_bytes = b"internal-report-parquet-v1"

    (extracts / "orders.parquet").write_bytes(orders_bytes)
    (extracts / "sales_report.parquet").write_bytes(sales_bytes)
    (extracts / "internal_report.parquet").write_bytes(internal_bytes)

    registry = table_registry_repo()
    registry.register(id="orders", name="orders", source_type="keboola", query_mode="local")
    registry.register(id="sales_report", name="sales_report", source_type="keboola", query_mode="materialized")
    registry.register(id="bq_view", name="bq_view", source_type="bigquery", query_mode="remote")
    registry.register(
        id="internal_report",
        name="internal_report",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )

    state = sync_state_repo()
    state.update_sync(table_id="orders", rows=10, file_size_bytes=len(orders_bytes), hash=_md5(orders_bytes))
    state.update_sync(table_id="sales_report", rows=5, file_size_bytes=len(sales_bytes), hash=_md5(sales_bytes))
    state.update_sync(
        table_id="internal_report", rows=1, file_size_bytes=len(internal_bytes), hash=_md5(internal_bytes)
    )
    # bq_view intentionally has no sync_state row (remote tables never get one).

    yield {
        "data_dir": tmp_path,
        "orders_md5": _md5(orders_bytes),
        "sales_md5": _md5(sales_bytes),
        "internal_md5": _md5(internal_bytes),
    }
    close_system_db()


class TestDistributionMirrorHandler:
    def test_uploads_changed_files(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert uploaded_keys == {"orders.parquet", "sales_report.parquet"}
        assert fake.objects["orders.parquet"] == b"orders-parquet-v1"
        assert fake.metadata["orders.parquet"]["md5"] == mirror_env["orders_md5"]

    def test_skips_md5_matches(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        # Pre-seed the store as already current for `orders`.
        fake.metadata["orders.parquet"] = {"md5": mirror_env["orders_md5"]}
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "orders.parquet" not in uploaded_keys
        assert "sales_report.parquet" in uploaded_keys

    def test_skips_remote_and_server_only_tables(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "bq_view.parquet" not in uploaded_keys
        assert "internal_report.parquet" not in uploaded_keys
        assert "bq_view.parquet" not in fake.objects
        assert "internal_report.parquet" not in fake.objects

    def test_per_file_failure_logs_and_continues(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        calls = {"n": 0}
        real_put_file = fake.put_file

        def flaky_put_file(local_path, key, md5):
            calls["n"] += 1
            if key == "orders.parquet":
                raise RuntimeError("simulated upload failure")
            return real_put_file(local_path, key, md5)

        monkeypatch.setattr(fake, "put_file", flaky_put_file)
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})  # must not raise

        assert "orders.parquet" not in fake.objects
        assert "sales_report.parquet" in fake.objects

    def test_writes_mirror_index_for_currently_mirrored_tables(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror
        from src.distribution import MIRROR_INDEX_KEY

        _run_distribution_mirror({})

        raw = fake.objects[MIRROR_INDEX_KEY]
        payload = json.loads(raw)
        assert payload["tables"] == {
            "orders": mirror_env["orders_md5"],
            "sales_report": mirror_env["sales_md5"],
        }
        assert "updated" in payload

    def test_marker_index_includes_preexisting_current_tables_not_just_this_runs_uploads(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        # `orders` is already mirrored+current before this run (skip path);
        # only `sales_report` is a fresh upload this run.
        fake.metadata["orders.parquet"] = {"md5": mirror_env["orders_md5"]}
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror
        from src.distribution import MIRROR_INDEX_KEY

        _run_distribution_mirror({})

        payload = json.loads(fake.objects[MIRROR_INDEX_KEY])
        assert payload["tables"] == {
            "orders": mirror_env["orders_md5"],
            "sales_report": mirror_env["sales_md5"],
        }

    def test_noop_when_object_store_is_none(self, mirror_env, monkeypatch):
        monkeypatch.setattr("src.object_store.object_store", lambda: None)

        import builtins

        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "boto3" or name.startswith("boto3."):
                raise AssertionError("boto3 must not be imported when object_store() is None")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})  # must not raise, must not touch boto3


class TestMirrorIndexHelpers:
    def test_write_then_read_round_trips(self):
        fake = FakeObjectStore()
        from src.distribution import read_mirror_index, write_mirror_index

        write_mirror_index(fake, {"orders": "abc123", "sales_report": "def456"})

        assert read_mirror_index(fake) == {"orders": "abc123", "sales_report": "def456"}

    def test_read_returns_empty_dict_when_absent(self):
        fake = FakeObjectStore()
        from src.distribution import read_mirror_index

        assert read_mirror_index(fake) == {}

    def test_read_fails_open_on_store_error(self):
        fake = FakeObjectStore()
        fake.fail_get_bytes = True
        from src.distribution import read_mirror_index

        assert read_mirror_index(fake) == {}

    def test_read_fails_open_on_malformed_json(self):
        fake = FakeObjectStore()
        from src.distribution import MIRROR_INDEX_KEY, read_mirror_index

        fake.objects[MIRROR_INDEX_KEY] = b"not json"

        assert read_mirror_index(fake) == {}


class TestDistributionMirrorRegistration:
    def test_registered_as_light_lane_kind(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS, LIGHT_LANE

        register_all_kinds()

        assert "distribution-mirror" in JOB_KINDS
        assert JOB_KINDS["distribution-mirror"].lane == LIGHT_LANE


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


class TestChainedEnqueueAfterDataRefresh:
    def test_enqueues_distribution_mirror_when_store_configured_and_sync_ok(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: True)
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert len(rows) == 1

    def test_does_not_enqueue_when_no_object_store_configured(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: True)
        monkeypatch.setattr("src.object_store.object_store", lambda: None)

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []

    def test_does_not_enqueue_when_sync_was_a_noop(self, jobs_db, monkeypatch):
        """`ok is None` means another same-process `_run_sync` call already
        held the lock — a rebuild may still be in flight, so mirroring now
        would risk reading half-written parquet. Only a clean `True` run
        triggers the follow-up."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: None)
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []

    def test_does_not_enqueue_when_sync_failed(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: False)
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        with pytest.raises(RuntimeError):
            JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []
