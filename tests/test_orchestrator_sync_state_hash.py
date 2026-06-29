"""SyncOrchestrator._update_sync_state must store the content MD5.

`agnes pull` re-hashes the downloaded parquet bytes and compares against
the manifest's hash for that table. If the orchestrator stores a
fingerprint (mtime+size) or a truncated MD5, every `agnes pull` of a
Keboola local-mode table fails with `hash mismatch: expected … got …`.
"""
import hashlib
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
            id="orders", name="orders", source_type="keboola",
            bucket="in.c-crm", source_table="orders", query_mode="local",
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
    with patch("src.db.get_system_db", side_effect=fake_get_system_db), \
         patch("src.repositories.get_system_db", side_effect=fake_get_system_db), \
         patch("src.orchestrator._get_extracts_dir", return_value=data_dir / "extracts"):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._update_sync_state(meta_rows=meta_rows, source_name="keboola")


def test_update_sync_state_stores_content_md5(
    system_db_path, parquet_with_known_md5, tmp_path
):
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


def test_update_sync_state_empty_hash_when_parquet_missing(
    system_db_path, tmp_path
):
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
