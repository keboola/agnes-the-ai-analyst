"""SyncOrchestrator._update_sync_state must store the content MD5.

`agnes pull` re-hashes the downloaded parquet bytes and compares against
the manifest's hash for that table. If the orchestrator stores a
fingerprint (mtime+size) or a truncated MD5, every `agnes pull` of a
Keboola local-mode table fails with `hash mismatch: expected … got …`.
"""

import hashlib
import logging
from unittest.mock import patch

import duckdb
import pytest

from src.db import _ensure_schema
from src.orchestrator import SyncOrchestrator
from src.repositories.sync_state import SyncStateRepository
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def system_db_path(tmp_path):
    """Path to a system.duckdb the orchestrator opens via get_system_db."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="in.c-crm",
            source_table="orders",
            query_mode="local",
            description="",
        )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def parquet_with_known_md5(tmp_path):
    """Lay down /tmp/data/extracts/keboola/data/orders.parquet with bytes
    whose MD5 the test knows up front."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    pq = extracts / "orders.parquet"
    bytes_payload = b"PAR1" + b"x" * 1024 + b"PAR1"
    pq.write_bytes(bytes_payload)
    return pq, hashlib.md5(bytes_payload).hexdigest()


def _run_update(system_db_path, meta_rows, data_dir):
    """Helper: invoke `_update_sync_state` with `get_system_db` redirected
    at our test DB and `_get_extracts_dir` redirected at our temp tree."""

    def fake_get_system_db():
        return duckdb.connect(str(system_db_path))

    # The orchestrator now writes sync_state through the repo factory, which
    # binds get_system_db at src.repositories import time — patch both the
    # source and the factory's binding so the redirect takes effect.
    with (
        patch("src.db.get_system_db", side_effect=fake_get_system_db),
        patch("src.repositories.get_system_db", side_effect=fake_get_system_db),
        patch("src.orchestrator._get_extracts_dir", return_value=data_dir / "extracts"),
    ):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._update_sync_state(meta_rows=meta_rows, source_name="keboola")


def test_update_sync_state_stores_content_md5(system_db_path, parquet_with_known_md5, tmp_path):
    """The hash written into sync_state must equal MD5 of the parquet's
    raw bytes, full 32 hex chars — same shape as the CLI's `_md5_file`."""
    pq_path, expected_md5 = parquet_with_known_md5
    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state is not None, "sync_state row should exist"
    stored = state["hash"]
    assert stored == expected_md5, (
        f"sync_state.hash must be the content MD5 ({expected_md5}) "
        f"so `agnes pull` post-download integrity check passes; got {stored!r}"
    )
    assert len(stored) == 32, "full hex MD5, not truncated"


def test_update_sync_state_empty_hash_when_parquet_missing(system_db_path, tmp_path):
    """If the parquet isn't on disk (race / failed extract), store empty
    string rather than crashing or writing a stale hash."""
    (tmp_path / "extracts" / "keboola" / "data").mkdir(parents=True)
    _run_update(
        system_db_path,
        meta_rows=[("orders", 0, 0, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state is not None
    assert state["hash"] == ""


# ---------------------------------------------------------------------------
# Partitioned tables: per-part hashing (partitioned distribution).
# A table stored as a directory of parquet parts (Jira hive
# `month=*/data.parquet`, Keboola flat `<key>.parquet`) gets a `parts`
# list + a rollup hash in sync_state, instead of the empty hash it gets
# today (no single `{table}.parquet` for the single-file path to find).
# ---------------------------------------------------------------------------

from src.orchestrator import _hash_table_parts, _parts_rollup_hash  # noqa: E402


def test_hash_table_parts_hive_layout(tmp_path):
    tdir = tmp_path / "issues"
    (tdir / "month=2026-06").mkdir(parents=True)
    (tdir / "month=2026-07").mkdir(parents=True)
    b6, b7 = b"jun" * 10, b"july" * 20
    (tdir / "month=2026-06" / "data.parquet").write_bytes(b6)
    (tdir / "month=2026-07" / "data.parquet").write_bytes(b7)

    assert _hash_table_parts(tdir) == [
        {"path": "month=2026-06/data.parquet", "hash": hashlib.md5(b6).hexdigest(), "size_bytes": len(b6)},
        {"path": "month=2026-07/data.parquet", "hash": hashlib.md5(b7).hexdigest(), "size_bytes": len(b7)},
    ]


def test_hash_table_parts_flat_layout(tmp_path):
    tdir = tmp_path / "cost"
    tdir.mkdir()
    b = b"data123"
    (tdir / "2025_11.parquet").write_bytes(b)
    assert _hash_table_parts(tdir) == [
        {"path": "2025_11.parquet", "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}
    ]


def test_hash_table_parts_none_when_not_a_dir(tmp_path):
    assert _hash_table_parts(tmp_path / "nope") is None


def test_hash_table_parts_none_when_no_parquets(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "readme.txt").write_text("x")
    assert _hash_table_parts(d) is None


def test_parts_rollup_hash_order_independent_and_full_md5():
    a = [
        {"path": "month=1/data.parquet", "hash": "aa", "size_bytes": 1},
        {"path": "month=2/data.parquet", "hash": "bb", "size_bytes": 2},
    ]
    assert _parts_rollup_hash(a) == _parts_rollup_hash(list(reversed(a)))
    assert len(_parts_rollup_hash(a)) == 32


def test_parts_rollup_hash_changes_when_a_part_changes():
    a = [{"path": "month=1/data.parquet", "hash": "aa", "size_bytes": 1}]
    b = [{"path": "month=1/data.parquet", "hash": "cc", "size_bytes": 1}]
    assert _parts_rollup_hash(a) != _parts_rollup_hash(b)


def test_update_sync_state_stores_parts_for_partitioned_table(system_db_path, tmp_path):
    """A table whose data is a directory of parts (no single {table}.parquet)
    gets a parts list + rollup hash + summed size in sync_state."""
    tdir = tmp_path / "extracts" / "keboola" / "data" / "orders" / "month=2026-06"
    tdir.mkdir(parents=True)
    b = b"PAR1" + b"y" * 512 + b"PAR1"
    (tdir / "data.parquet").write_bytes(b)

    _run_update(system_db_path, meta_rows=[("orders", 50, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state["parts"] == [
        {"path": "month=2026-06/data.parquet", "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}
    ]
    assert state["hash"] == _parts_rollup_hash(state["parts"])
    assert state["file_size_bytes"] == len(b)


def test_update_sync_state_single_file_still_has_no_parts(system_db_path, parquet_with_known_md5, tmp_path):
    """Backward-compat: a single-file table writes parts=None (NULL)."""
    pq_path, _ = parquet_with_known_md5
    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )
    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["parts"] is None


# ---------------------------------------------------------------------------
# Both-layouts collision (#1339): a flat `<table>.parquet` file AND a
# `<table>/` partition directory present at the same time. The flat file
# silently wins today — unchanged by this fix (precedence + stale-sibling
# cleanup are open human decisions, see the TODO(#1339) in the source) — but
# until now the collision was invisible: the manifest kept advertising the
# flat file's (possibly stale) hash with nothing to say a fresher directory
# sat right beside it. This must be loud, not silent.
# ---------------------------------------------------------------------------


def _write_both_layouts(tmp_path):
    """Flat `orders.parquet` + a sibling `orders/` partition directory."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    flat_bytes = b"PAR1" + b"stale" * 50 + b"PAR1"
    pq_path = extracts / "orders.parquet"
    pq_path.write_bytes(flat_bytes)
    table_dir = extracts / "orders"
    table_dir.mkdir()
    (table_dir / "2025_11.parquet").write_bytes(b"fresh-partitioned-bytes")
    return pq_path, table_dir, flat_bytes


def test_both_layouts_collision_logs_error_naming_both_paths_and_table(system_db_path, tmp_path, caplog):
    pq_path, table_dir, _ = _write_both_layouts(tmp_path)

    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )

    collision_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR"
        and "orders" in r.getMessage()
        and str(pq_path) in r.getMessage()
        and str(table_dir) in r.getMessage()
    ]
    assert collision_records, (
        "expected an ERROR log naming the table id and BOTH concrete paths; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


def test_both_layouts_collision_flags_sync_state_but_keeps_flat_hash(system_db_path, tmp_path):
    """The served bytes must stay byte-for-byte identical to the flat-only
    case (the flat file still wins) — only the flagging is new."""
    pq_path, table_dir, flat_bytes = _write_both_layouts(tmp_path)

    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state is not None
    # Bytes served: identical to the flat-only case — precedence unchanged.
    assert state["hash"] == hashlib.md5(flat_bytes).hexdigest()
    assert state["parts"] is None
    assert state["rows"] == 100
    # No longer invisible: flagged via the existing sync_state error
    # mechanism (the same set_error() `GET /api/admin/registry` already
    # surfaces as `last_sync_error`).
    assert state["status"] == "error"
    assert str(pq_path) in (state["error"] or "")
    assert str(table_dir) in (state["error"] or "")


def test_flat_only_layout_is_not_flagged_as_a_collision(system_db_path, parquet_with_known_md5, tmp_path, caplog):
    """Regression pin: the ordinary single-file case must keep behaving
    exactly as before — no ERROR log, no sync_state error flip."""
    pq_path, expected_md5 = parquet_with_known_md5
    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )

    assert not [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["hash"] == expected_md5
    assert state["status"] == "ok"
    assert not state.get("error")


def test_dir_only_layout_is_not_flagged_as_a_collision(system_db_path, tmp_path, caplog):
    """Regression pin: the ordinary partitioned-only case (no flat sibling)
    must keep behaving exactly as before either."""
    tdir = tmp_path / "extracts" / "keboola" / "data" / "orders" / "month=2026-06"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"PAR1" + b"y" * 512 + b"PAR1")

    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(system_db_path, meta_rows=[("orders", 50, 0, "local")], data_dir=tmp_path)

    assert not [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["status"] == "ok"
    assert not state.get("error")
