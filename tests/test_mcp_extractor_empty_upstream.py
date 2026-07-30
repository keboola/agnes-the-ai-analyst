"""An empty upstream response must RESET the table, not pin it to stale data.

Since the carry-forward merge landed (#1119), a materialize-mode tool whose
upstream call fails keeps its last-known-good ``_meta`` row + view — the right
call for a flaky upstream. But ``_find_data_array`` treated "the collection is
empty" as the same hard error as "this response isn't table-shaped", so a tool
whose rows were all legitimately deleted upstream kept serving its last
non-empty snapshot on every subsequent run, forever, with only an entry in
``errors``. Analytics silently showed deleted data.

These tests pin the distinction:

* empty upstream **with** a previous snapshot → zero-row parquet written with
  the previous file's schema (columns survive, rows don't);
* empty upstream **with no** previous snapshot → distinct ``empty_upstream``
  error code, no table (an empty JSON list carries no schema to write);
* a genuine transient failure → still carries the last-known-good forward.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import duckdb
import pytest

import connectors.mcp.client as mcp_client
from connectors.mcp import extractor as mcp_extractor
from connectors.mcp.client import ToolCallResult
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import ToolRegistryRepository

SOURCE_ID = "src_crm"
SOURCE_NAME = "crm"
TOOL_ID = "tool_a"
UPSTREAM_NAME = "list_accounts"
TABLE = "accounts"


@pytest.fixture
def system_conn(e2e_env):
    conn = _open_duckdb(str(e2e_env["data_dir"] / "state" / "system.duckdb"))
    _ensure_schema(conn)
    MCPSourceRepository(conn).upsert(
        id=SOURCE_ID,
        name=SOURCE_NAME,
        transport="http",
        url="https://example.com/mcp",
    )
    ToolRegistryRepository(conn).upsert(
        tool_id=TOOL_ID,
        source_id=SOURCE_ID,
        original_name=UPSTREAM_NAME,
        exposed_name=TABLE,
        mode="materialize",
        schedule="0 * * * *",
    )
    yield conn
    conn.close()


def _payload(batch: str) -> Dict[str, Any]:
    return {
        "accounts": [
            {"id": f"a-{batch}-1", "name": f"Acme {batch}"},
            {"id": f"a-{batch}-2", "name": f"Globex {batch}"},
        ],
        "total": 2,
    }


EMPTY_PAYLOAD = {"accounts": [], "total": 0}


def _fake_upstream(monkeypatch, outcome: Any):
    """Patch the upstream call. ``outcome`` is a JSON payload or an Exception."""

    async def _call(source, tool_name, arguments=None, *, caller_user_id=None):
        if isinstance(outcome, BaseException):
            raise outcome
        return ToolCallResult(text=json.dumps(outcome), data=outcome, is_error=False)

    monkeypatch.setattr(mcp_client, "call_tool_async", _call)


def _run(system_conn, e2e_env, *, sync: bool = False):
    kwargs = dict(
        system_conn=system_conn,
        source_id=SOURCE_ID,
        output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
    )
    if sync:
        return mcp_extractor.extract_source(**kwargs)
    return asyncio.run(mcp_extractor.extract_source_async(**kwargs))


def _table_state(db_path):
    """Return (meta rows-by-table, view columns, view id values)."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        meta = {r[0]: r[1] for r in conn.execute("SELECT table_name, rows FROM _meta").fetchall()}
        cols, ids = None, None
        if TABLE in meta:
            cols = [d[0] for d in conn.execute(f'SELECT * FROM "{TABLE}"').description]
            ids = sorted(r[0] for r in conn.execute(f'SELECT id FROM "{TABLE}"').fetchall())
        return meta, cols, ids
    finally:
        conn.close()


class TestEmptyUpstream:
    def test_resets_table_to_zero_rows_keeping_the_schema(self, system_conn, e2e_env, monkeypatch):
        """The whole point: deleted-upstream rows must disappear from analytics."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        result = _run(system_conn, e2e_env)

        assert result["errors"] == []
        assert result["tables"] == [
            {"table": TABLE, "rows": 0, "size_bytes": result["tables"][0]["size_bytes"], "empty_upstream": True}
        ]
        # not carried forward — this run wrote it
        assert result["carried_forward"] == []

        meta, cols, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {TABLE: 0}
        assert cols == ["id", "name"]  # schema reused from the previous snapshot
        assert ids == []  # stale rows gone

    def test_sync_path_resets_too(self, system_conn, e2e_env, monkeypatch):
        """The scheduler/CLI wrapper shares the contract with the async one."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env, sync=True)

        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        result = _run(system_conn, e2e_env, sync=True)

        assert result["errors"] == []
        assert result["tables"][0]["rows"] == 0
        _, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert ids == []

    def test_top_level_empty_list_also_resets(self, system_conn, e2e_env, monkeypatch):
        _fake_upstream(monkeypatch, [{"id": "a-1", "name": "Acme"}])
        _run(system_conn, e2e_env)

        _fake_upstream(monkeypatch, [])
        result = _run(system_conn, e2e_env)

        assert result["errors"] == []
        _, cols, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert cols == ["id", "name"]
        assert ids == []

    def test_without_previous_snapshot_is_a_distinct_error(self, system_conn, e2e_env, monkeypatch):
        """No previous parquet = no schema to write. Surface it distinctly so
        an admin can act (re-run later, or reclassify as passthrough) instead
        of reading it as a generic upstream failure."""
        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        result = _run(system_conn, e2e_env)

        assert result["tables"] == []
        assert [(e["tool"], e["code"]) for e in result["errors"]] == [(TABLE, "empty_upstream")]
        assert "no previous snapshot" in result["errors"][0]["error"]

        meta, _, _ = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {}
        assert not (e2e_env["extracts_dir"] / SOURCE_NAME / "data" / f"{TABLE}.parquet").exists()

    def test_repopulates_after_an_empty_run(self, system_conn, e2e_env, monkeypatch):
        """Zero rows is not a terminal state — the next non-empty run refills."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)
        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        _run(system_conn, e2e_env)
        _fake_upstream(monkeypatch, _payload("v3"))
        _run(system_conn, e2e_env)

        meta, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {TABLE: 2}
        assert ids == ["a-v3-1", "a-v3-2"]

    def test_orchestrator_rebuild_serves_zero_rows(self, system_conn, e2e_env, monkeypatch):
        """End of the chain: the analytics view must show the emptied table."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)
        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        _run(system_conn, e2e_env)

        from src.orchestrator import SyncOrchestrator

        views_by_source = SyncOrchestrator().rebuild()
        assert set(views_by_source.get(SOURCE_NAME, [])) == {TABLE}

        extract_path = e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb"
        conn = duckdb.connect(e2e_env["analytics_db"], read_only=True)
        try:
            conn.execute(f"ATTACH '{extract_path}' AS {SOURCE_NAME} (READ_ONLY)")
            assert conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0] == 0
        finally:
            conn.close()


class TestFailuresStillCarryForward:
    def test_transient_failure_keeps_last_known_good(self, system_conn, e2e_env, monkeypatch):
        """The #1119 contract is unchanged for real failures, and the error
        now carries a code that separates it from an empty upstream."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        _fake_upstream(monkeypatch, RuntimeError("upstream flaked"))
        result = _run(system_conn, e2e_env)

        assert result["tables"] == []
        assert [(e["tool"], e["code"]) for e in result["errors"]] == [(TABLE, "materialize_failed")]
        assert result["carried_forward"] == [TABLE]

        meta, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {TABLE: 2}
        assert ids == ["a-v1-1", "a-v1-2"]

    def test_upstream_error_flag_keeps_last_known_good(self, system_conn, e2e_env, monkeypatch):
        """``is_error`` responses are failures, never "the table is empty"."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        async def _err(source, tool_name, arguments=None, *, caller_user_id=None):
            return ToolCallResult(text="rate limited", data=None, is_error=True)

        monkeypatch.setattr(mcp_client, "call_tool_async", _err)
        result = _run(system_conn, e2e_env)

        assert [e["code"] for e in result["errors"]] == ["materialize_failed"]
        assert result["carried_forward"] == [TABLE]
        _, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert ids == ["a-v1-1", "a-v1-2"]

    def test_torn_zero_row_write_leaves_the_previous_snapshot_intact(self, system_conn, e2e_env, monkeypatch):
        """The reset overwrites the very file whose schema it needs, so it goes
        through a temp file: a failed write (full disk) must not corrupt the
        last-known-good parquet — carry-forward would drop the table entirely."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        import pyarrow.parquet as pq

        def _boom(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(pq, "write_table", _boom)
        _fake_upstream(monkeypatch, EMPTY_PAYLOAD)
        result = _run(system_conn, e2e_env)

        assert [e["code"] for e in result["errors"]] == ["materialize_failed"]
        assert result["carried_forward"] == [TABLE]
        assert not (e2e_env["extracts_dir"] / SOURCE_NAME / "data" / f"{TABLE}.parquet.tmp").exists()
        _, cols, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert cols == ["id", "name"]
        assert ids == ["a-v1-1", "a-v1-2"]

    def test_soft_failed_200_keeps_the_parquet_on_disk(self, system_conn, e2e_env, monkeypatch):
        """An upstream that answers 200-with-an-error and an empty collection
        (a rate limit, typically) must not be read as "the table is empty" — the
        reset overwrites the parquet in place, so the previous rows would be
        gone from disk, not just from extract.duckdb (Devin Review)."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        _fake_upstream(monkeypatch, {"error": "quota exceeded", "accounts": []})
        result = _run(system_conn, e2e_env)

        assert [e["code"] for e in result["errors"]] == ["materialize_failed"]
        assert result["carried_forward"] == [TABLE]
        _, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert ids == ["a-v1-1", "a-v1-2"]

    def test_non_table_shaped_response_keeps_last_known_good(self, system_conn, e2e_env, monkeypatch):
        """A response with no collection at all is a classification/upstream
        problem, not an empty table — stay conservative and carry forward."""
        _fake_upstream(monkeypatch, _payload("v1"))
        _run(system_conn, e2e_env)

        _fake_upstream(monkeypatch, {"status": "degraded", "message": "try later"})
        result = _run(system_conn, e2e_env)

        assert [e["code"] for e in result["errors"]] == ["materialize_failed"]
        assert result["carried_forward"] == [TABLE]
        _, _, ids = _table_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert ids == ["a-v1-1", "a-v1-2"]


class TestEmptyDetection:
    """``_upstream_is_empty`` decides reset-vs-carry-forward — pin its shapes."""

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"accounts": [], "total": 0},
            {"items": [], "next_cursor": None},
            # a success-valued status must not read as a failure signal
            {"accounts": [], "status": "ok"},
            # keys that ride along with successful responses stay allowed, or a
            # genuinely empty table would go back to being pinned to stale data
            {"accounts": [], "message": "no results", "detail": "0 of 0"},
            {"accounts": [], "status": 200},
            {"accounts": [], "status": 0},  # 0 = no error in several RPC dialects
            {"accounts": [], "success": True},
            # sibling metadata objects are not the payload — an ordinary empty
            # page must still reset the table
            {"accounts": [], "pagination": {"page": 1, "total": 0}},
            {"items": [], "meta": {"took_ms": 4}},
        ],
    )
    def test_empty_shapes(self, payload):
        assert mcp_extractor._find_data_array(payload) is None
        assert mcp_extractor._upstream_is_empty(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "degraded"},  # no collection at all
            {"names": ["a", "b"]},  # non-empty list of scalars → not table-shaped
            {"items": [], "rows": ["a"]},  # mixed: something non-empty is there
            ["a", "b"],  # non-empty list of scalars
            "just text",
            None,
            42,
            # Soft-failed 200s — resetting the table on a rate limit would wipe
            # the last-known-good parquet (Devin Review on this change).
            {"error": "quota exceeded", "accounts": []},
            {"status": "degraded", "details": []},
            {"errors": "boom", "accounts": []},
            {"error": True, "accounts": []},
            {"error": {"code": 429}, "accounts": []},
            {"state": "throttled", "items": []},
            # Numeric status and a negated success flag are the other two ways a
            # 200 says "it didn't work".
            {"status": 429, "items": []},
            {"status": 500, "data": []},
            {"success": False, "data": []},
            {"ok": False, "items": []},
            # Real content in a container we failed to interpret: an id → record
            # map could itself be the table.
            {"accounts": {"a1": {"id": 1}}, "warnings": []},
        ],
    )
    def test_not_empty_shapes(self, payload):
        assert mcp_extractor._find_data_array(payload) is None
        assert mcp_extractor._upstream_is_empty(payload) is False

    def test_non_empty_table_shaped_payload_is_unaffected(self):
        payload = {"accounts": [{"id": 1}], "total": 1}
        assert mcp_extractor._find_data_array(payload) == [{"id": 1}]
        assert mcp_extractor._upstream_is_empty(payload) is False
