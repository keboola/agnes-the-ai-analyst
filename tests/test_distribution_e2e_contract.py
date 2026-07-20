"""End-to-end presign contract test (three-plane wave 2-H, WS F, task
WF-5 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

This is the wave's integration gate: it drives the FULL signed-URL
distribution loop through a single in-process ``FakeObjectStore`` (no
``boto3``, no ``moto``, no network) —

    distribution-mirror job -> manifest signed_url -> `agnes pull` fetch
    -> md5-verify + promote

— rather than re-testing WF-1..4's units in isolation (those live in
``tests/test_object_store.py`` / ``tests/test_distribution_mirror.py`` /
``tests/test_manifest_signed_urls.py`` / ``tests/test_pull_signed_url.py``).

Covers:

- happy path: mirror uploads the parquet + marker index -> manifest
  carries ``signed_url``/``signed_url_expires_at`` -> `run_pull` fetches
  via the signed URL and md5-verifies + promotes it;
- forced signed-URL failure (wrong bytes, and a raised transport error)
  -> `run_pull` falls back to the app-served
  ``/api/data/{id}/download`` path and the md5 gate still holds;
- inert when ``object_store()`` is ``None``: the mirror job no-ops, the
  manifest carries no ``signed_url`` at all, and `run_pull` never even
  attempts a signed-URL fetch.

Deterministic throughout — no real S3, no sleeps. The TTL assertion checks
the delta against "now" (bounded to the 900s TTL), not a fixed wall-clock
value.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.object_store_fakes import FakeObjectStore

ORDERS_BYTES = b"orders-parquet-e2e-wave2h-v1"
ORDERS_MD5 = hashlib.md5(ORDERS_BYTES).hexdigest()
FAKE_STORE_URL_PREFIX = "https://fake-object-store.example.com/"


@pytest.fixture(autouse=True)
def _reset_caches():
    from src.distribution import reset_mirror_index_cache
    from src.object_store import reset_object_store_cache

    reset_object_store_cache()
    reset_mirror_index_cache()
    yield
    reset_object_store_cache()
    reset_mirror_index_cache()


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """A fresh system.duckdb with one local ``orders`` table: registered,
    synced (sync_state row carrying ``ORDERS_MD5``), its parquet actually
    on disk under the extracts tree, and one analyst user whose group
    holds a ``required`` grant on a data package wrapping ``orders`` —
    ready to be mirrored, manifested, and pulled.

    A plain god-mode Admin user is deliberately NOT used here: the v49
    manifest's ``direct_tables`` section is always ``[]`` (per-table
    grants are retired — see ``_build_direct_tables_section``), so
    `cli/lib/pull.py`'s #506 stack-scoped download filter would treat an
    admin with no package grants as having an EMPTY authorized-name set
    and skip every download. Routing through a real data-package grant
    exercises the RBAC path exactly as a real analyst pull would.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)

    from src.db import close_system_db, get_system_db
    from src.repositories import sync_state_repo, table_registry_repo
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()

    if UserRepository(conn).get_by_id("analyst1") is None:
        UserRepository(conn).create(id="analyst1", email="analyst1@test.com", name="Analyst")
    group = UserGroupsRepository(conn).create(name="E2EDistributionGroup", description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")

    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    (extracts / "orders.parquet").write_bytes(ORDERS_BYTES)

    table_registry_repo().register(id="orders", name="orders", source_type="keboola", query_mode="local")
    sync_state_repo().update_sync(table_id="orders", rows=10, file_size_bytes=len(ORDERS_BYTES), hash=ORDERS_MD5)

    pkg_repo = DataPackagesRepository(conn)
    pkg_id = pkg_repo.create(
        name="OrdersPkg", slug="orders-pkg-e2e", description=None, icon=None, color=None, created_by="test"
    )
    pkg_repo.add_table(pkg_id, "orders", added_by="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
        ["grant-orders-pkg-e2e", gid, pkg_id],
    )

    yield {"conn": conn, "data_dir": tmp_path, "user": {"id": "analyst1", "email": "analyst1@test.com"}}
    close_system_db()


def _configure_store(monkeypatch, store, *, mode: str = "on") -> None:
    """Point both the mirror job's and the manifest builder's object-store
    seams at *store* — the end state a real ``distribution.object_store``
    instance.yaml block + ``distribution.signed_urls: on|auto`` would
    produce, minus the yaml/env resolution plumbing itself (already
    covered by ``tests/test_object_store.py``)."""
    monkeypatch.setattr("src.object_store.object_store", lambda: store)
    monkeypatch.setattr("app.api.sync.object_store", lambda: store)
    monkeypatch.setattr("app.api.sync.distribution_signed_urls_mode", lambda: mode)


def _mirror_then_build_manifest(e2e_env: dict) -> dict:
    """Run the distribution-mirror job, then build the manifest — the
    first two hops of the full loop, shared by every scenario below."""
    from app.api.sync import _build_manifest_for_user
    from app.worker.kinds import _run_distribution_mirror

    _run_distribution_mirror({})
    return _build_manifest_for_user(e2e_env["conn"], e2e_env["user"])


def _signed_url_fetcher(store: FakeObjectStore, *, deliver: bytes | None = None, raise_error: bool = False):
    """Stand-in for `cli.lib.pull._fetch_signed_url` that resolves the
    fake store's deterministic presigned URL
    (``https://fake-object-store.example.com/{key}?ttl={ttl}``) back to
    the bytes actually sitting in ``store.objects`` — the in-process
    equivalent of a real signed GET hitting the bucket, without any
    network I/O or SSRF-guard machinery (that machinery is exercised for
    real in ``tests/test_pull_signed_url.py``; this test is about the
    wiring between mirror -> manifest -> pull, not re-proving the guard).

    ``deliver`` overrides what's served, simulating an object that exists
    but doesn't match the expected content (corruption / wrong object).
    ``raise_error`` simulates a network failure / expired-URL rejection.
    """
    prefix = FAKE_STORE_URL_PREFIX

    def _fetch(url: str, target_path: str, progress_callback=None) -> None:
        if raise_error:
            raise ConnectionError("simulated signed-url fetch failure")
        assert url.startswith(prefix), f"unexpected signed url shape: {url}"
        key = url[len(prefix) :].split("?", 1)[0]
        data = deliver if deliver is not None else store.objects.get(key)
        if data is None:
            raise FileNotFoundError(f"no such object: {key}")
        Path(target_path).write_bytes(data)

    return _fetch


def _fake_api_get(manifest: dict):
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        resp.raise_for_status = lambda: None
        return resp

    return _api_get


def _app_served_stream_download(body: bytes):
    def _stream_download(path, target_path, progress_callback=None):
        Path(target_path).write_bytes(body)

    return _stream_download


class TestSignedUrlDistributionE2E:
    def test_mirror_then_manifest_then_pull_prefers_signed_url(self, e2e_env, monkeypatch, tmp_path):
        store = FakeObjectStore()
        _configure_store(monkeypatch, store)

        # Hop 1+2: distribution-mirror job -> manifest.
        manifest = _mirror_then_build_manifest(e2e_env)

        assert store.objects["orders.parquet"] == ORDERS_BYTES
        assert store.metadata["orders.parquet"]["md5"] == ORDERS_MD5
        from src.distribution import MIRROR_INDEX_KEY

        assert MIRROR_INDEX_KEY in store.objects

        entry = manifest["tables"]["orders"]
        assert entry["signed_url"] == f"{FAKE_STORE_URL_PREFIX}orders.parquet?ttl=900"
        assert entry["hash"] == ORDERS_MD5
        expires_at = datetime.fromisoformat(entry["signed_url_expires_at"])
        delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
        assert 0 < delta <= 900  # 15-minute TTL bound, not a wall-clock value

        # Hop 3: `agnes pull` — signed-URL happy path. The flat
        # `manifest["tables"]` download loop (`_download_one`, the one
        # WF-4 touches) must NEVER hit the app-served route when the
        # signed URL succeeds. `run_pull` also runs a SEPARATE v49
        # stack-sync pass over `data_packages[].tables[]` (unrelated to
        # this wave — see the module docstrings in `app/api/sync.py` /
        # `tests/test_manifest_signed_urls.py`: the typed section never
        # carries a `signed_url`), which legitimately calls
        # `stream_download` for its own `.claude/data/_shared/` copy —
        # recorded here rather than banned outright, and asserted to
        # never target the flat-loop's `server/parquet/orders.parquet`.
        workspace = tmp_path / "workspace"
        monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
        monkeypatch.setattr("cli.lib.pull._fetch_signed_url", _signed_url_fetcher(store), raising=False)
        stream_download_calls: list[tuple[str, str]] = []

        def _stream_download_spy(path, target_path, progress_callback=None):
            stream_download_calls.append((path, target_path))
            Path(target_path).write_bytes(ORDERS_BYTES)

        monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download_spy, raising=False)

        from cli.lib.pull import run_pull

        result = run_pull(server_url="http://x", token="t", workspace=workspace)

        assert result.errors == []
        assert result.tables_updated == 1
        assert result.tables_via_signed_url == 1
        assert result.tables_via_app == 0
        main_target = str(workspace / "server" / "parquet" / "orders.parquet")
        assert all(target != main_target for _, target in stream_download_calls)
        assert (workspace / "server" / "parquet" / "orders.parquet").read_bytes() == ORDERS_BYTES

    def test_signed_url_wrong_bytes_falls_back_to_app_md5_gate_holds(self, e2e_env, monkeypatch, tmp_path):
        store = FakeObjectStore()
        _configure_store(monkeypatch, store)
        manifest = _mirror_then_build_manifest(e2e_env)
        assert manifest["tables"]["orders"]["signed_url"]

        workspace = tmp_path / "workspace"
        monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
        # The store serves wrong bytes for the signed URL (corruption /
        # stale object) — md5 verify must reject it and fall back.
        monkeypatch.setattr(
            "cli.lib.pull._fetch_signed_url",
            _signed_url_fetcher(store, deliver=b"not-the-real-parquet-bytes"),
            raising=False,
        )
        monkeypatch.setattr("cli.lib.pull.stream_download", _app_served_stream_download(ORDERS_BYTES), raising=False)

        from cli.lib.pull import run_pull

        result = run_pull(server_url="http://x", token="t", workspace=workspace)

        assert result.errors == []
        assert result.tables_updated == 1
        assert result.tables_via_signed_url == 0
        assert result.tables_via_app == 1
        assert (workspace / "server" / "parquet" / "orders.parquet").read_bytes() == ORDERS_BYTES

    def test_signed_url_transport_error_falls_back_to_app_md5_gate_holds(self, e2e_env, monkeypatch, tmp_path):
        store = FakeObjectStore()
        _configure_store(monkeypatch, store)
        manifest = _mirror_then_build_manifest(e2e_env)
        assert manifest["tables"]["orders"]["signed_url"]

        workspace = tmp_path / "workspace"
        monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
        monkeypatch.setattr(
            "cli.lib.pull._fetch_signed_url",
            _signed_url_fetcher(store, raise_error=True),
            raising=False,
        )
        monkeypatch.setattr("cli.lib.pull.stream_download", _app_served_stream_download(ORDERS_BYTES), raising=False)

        from cli.lib.pull import run_pull

        result = run_pull(server_url="http://x", token="t", workspace=workspace)

        assert result.errors == []
        assert result.tables_updated == 1
        assert result.tables_via_signed_url == 0
        assert result.tables_via_app == 1
        assert (workspace / "server" / "parquet" / "orders.parquet").read_bytes() == ORDERS_BYTES

    def test_inert_when_object_store_is_none(self, e2e_env, monkeypatch, tmp_path):
        """No object store configured (the S/M-tier default): the mirror
        job no-ops, the manifest carries no `signed_url` at all (byte-for-
        byte identical to a pre-wave-2H manifest), and `run_pull` never
        even attempts a signed-URL fetch — only the app-served path
        runs."""
        monkeypatch.setattr("src.object_store.object_store", lambda: None)
        monkeypatch.setattr("app.api.sync.object_store", lambda: None)

        from app.api.sync import _build_manifest_for_user
        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})  # must be a clean no-op

        manifest = _build_manifest_for_user(e2e_env["conn"], e2e_env["user"])
        entry = manifest["tables"]["orders"]
        assert "signed_url" not in entry
        assert "signed_url_expires_at" not in entry

        workspace = tmp_path / "workspace"
        monkeypatch.setattr("cli.lib.pull.api_get", _fake_api_get(manifest), raising=False)
        fetch_signed_url_mock = MagicMock(side_effect=AssertionError("must not be called without a signed_url"))
        monkeypatch.setattr("cli.lib.pull._fetch_signed_url", fetch_signed_url_mock, raising=False)
        monkeypatch.setattr("cli.lib.pull.stream_download", _app_served_stream_download(ORDERS_BYTES), raising=False)

        from cli.lib.pull import run_pull

        result = run_pull(server_url="http://x", token="t", workspace=workspace)

        assert result.errors == []
        assert result.tables_updated == 1
        fetch_signed_url_mock.assert_not_called()
        assert result.tables_via_signed_url == 0
        assert result.tables_via_app == 1
        assert (workspace / "server" / "parquet" / "orders.parquet").read_bytes() == ORDERS_BYTES
