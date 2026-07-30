"""Targeted (``only_tool_id``) MCP materialize runs must MERGE, not replace.

``extract_source`` / ``extract_source_async`` write a fresh
``extract.duckdb.tmp`` and rename it over the existing file. A targeted run
(the per-tool "Materialize now" button on /admin sources, or the
/admin/linked-apps wizard) used to write ONLY the targeted tool into that
fresh file — dropping the ``_meta`` rows and views of every other
materialize-mode tool of the source until the next full-source run, and the
orchestrator's next rebuild lost those analytics views.

These tests pin the merge semantics: a targeted run carries forward the
untouched tools' ``_meta`` rows + views (their parquets were never deleted),
and the orchestrator rebuild keeps seeing all of them.
"""

from __future__ import annotations

import asyncio

import duckdb
import pandas as pd
import pytest

from connectors.mcp import extractor as mcp_extractor
from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import ToolRegistryRepository

SOURCE_ID = "src_crm"
SOURCE_NAME = "crm"


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
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id="tool_a",
        source_id=SOURCE_ID,
        original_name="list_accounts",
        exposed_name="accounts",
        mode="materialize",
        schedule="0 * * * *",
    )
    tools.upsert(
        tool_id="tool_b",
        source_id=SOURCE_ID,
        original_name="list_orders",
        exposed_name="orders",
        mode="materialize",
        schedule="0 * * * *",
    )
    yield conn
    conn.close()


def _fake_rows(tool_name: str, batch: str):
    return [{"id": f"{tool_name}-{batch}-1"}, {"id": f"{tool_name}-{batch}-2"}]


def _make_fake_materialize(batch: str, fail_tools: set[str] | None = None):
    """Return a sync fake for ``_materialize_one_tool`` writing marker rows."""

    def _fake(*, source, tool, output_path):
        name = tool["exposed_name"]
        if fail_tools and name in fail_tools:
            raise RuntimeError(f"upstream flaked for {name}")
        rows = _fake_rows(name, batch)
        parquet_path = output_path / "data" / f"{name}.parquet"
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(parquet_path, index=False)
        return (len(rows), parquet_path.stat().st_size)

    return _fake


def _make_fake_materialize_async(batch: str, fail_tools: set[str] | None = None):
    sync_fake = _make_fake_materialize(batch, fail_tools)

    async def _fake(*, source, tool, output_path):
        return sync_fake(source=source, tool=tool, output_path=output_path)

    return _fake


def _extract_state(db_path):
    """Read (meta table_names, {view: id-values}) from an extract.duckdb."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        meta = {r[0] for r in conn.execute("SELECT table_name FROM _meta").fetchall()}
        views = {}
        for name in meta:
            views[name] = sorted(r[0] for r in conn.execute(f'SELECT id FROM "{name}"').fetchall())
        return meta, views
    finally:
        conn.close()


def _run_full(system_conn, e2e_env, monkeypatch, batch: str):
    monkeypatch.setattr(mcp_extractor, "_materialize_one_tool", _make_fake_materialize(batch))
    return mcp_extractor.extract_source(
        system_conn=system_conn,
        source_id=SOURCE_ID,
        output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
    )


class TestTargetedRunMerges:
    def test_sync_targeted_run_preserves_other_tools(self, system_conn, e2e_env, monkeypatch):
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        monkeypatch.setattr(mcp_extractor, "_materialize_one_tool", _make_fake_materialize("v2"))
        result = mcp_extractor.extract_source(
            system_conn=system_conn,
            source_id=SOURCE_ID,
            only_tool_id="tool_a",
            output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
        )
        assert [t["table"] for t in result["tables"]] == ["accounts"]
        assert result["errors"] == []

        meta, views = _extract_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {"accounts", "orders"}
        # targeted tool refreshed, untouched tool keeps its previous data
        assert views["accounts"] == ["accounts-v2-1", "accounts-v2-2"]
        assert views["orders"] == ["orders-v1-1", "orders-v1-2"]
        assert (e2e_env["extracts_dir"] / SOURCE_NAME / "data" / "orders.parquet").exists()

    def test_async_targeted_run_preserves_other_tools(self, system_conn, e2e_env, monkeypatch):
        """The admin /materialize endpoint (per-tool button + linked-apps
        wizard) goes through ``extract_source_async`` — same merge contract."""
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        monkeypatch.setattr(mcp_extractor, "_materialize_one_tool_async", _make_fake_materialize_async("v2"))
        result = asyncio.run(
            mcp_extractor.extract_source_async(
                system_conn=system_conn,
                source_id=SOURCE_ID,
                only_tool_id="tool_b",
                output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
            )
        )
        assert [t["table"] for t in result["tables"]] == ["orders"]

        meta, views = _extract_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {"accounts", "orders"}
        assert views["orders"] == ["orders-v2-1", "orders-v2-2"]
        assert views["accounts"] == ["accounts-v1-1", "accounts-v1-2"]

    def test_targeted_run_failure_keeps_last_known_good(self, system_conn, e2e_env, monkeypatch):
        """If the targeted tool's upstream call fails, the previous extract's
        row for that tool (and everything else) survives — a flaky upstream
        must not vaporize the existing table."""
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        monkeypatch.setattr(
            mcp_extractor,
            "_materialize_one_tool",
            _make_fake_materialize("v2", fail_tools={"accounts"}),
        )
        result = mcp_extractor.extract_source(
            system_conn=system_conn,
            source_id=SOURCE_ID,
            only_tool_id="tool_a",
            output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
        )
        assert result["tables"] == []
        assert [e["tool"] for e in result["errors"]] == ["accounts"]

        meta, views = _extract_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {"accounts", "orders"}
        assert views["accounts"] == ["accounts-v1-1", "accounts-v1-2"]
        assert views["orders"] == ["orders-v1-1", "orders-v1-2"]

    def test_targeted_run_prunes_unregistered_tables(self, system_conn, e2e_env, monkeypatch):
        """A previous table whose tool was renamed (or disabled/deleted) since
        the last run must NOT be carried forward — otherwise the stale
        old-named table would survive indefinitely under repeated targeted
        runs. Only tables of currently registered, enabled materialize tools
        are carried."""
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        # rename tool_b's exposed table: orders → invoices
        ToolRegistryRepository(system_conn).upsert(
            tool_id="tool_b",
            source_id=SOURCE_ID,
            original_name="list_orders",
            exposed_name="invoices",
            mode="materialize",
            schedule="0 * * * *",
        )
        monkeypatch.setattr(mcp_extractor, "_materialize_one_tool", _make_fake_materialize("v2"))
        result = mcp_extractor.extract_source(
            system_conn=system_conn,
            source_id=SOURCE_ID,
            only_tool_id="tool_a",
            output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
        )
        assert result["carried_forward"] == []

        meta, views = _extract_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {"accounts"}
        assert views["accounts"] == ["accounts-v2-1", "accounts-v2-2"]

    def test_full_run_still_replaces(self, system_conn, e2e_env, monkeypatch):
        """No ``only_tool_id`` → replace semantics stay: tools that left the
        registry (disabled/deleted) drop out of the fresh extract."""
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        ToolRegistryRepository(system_conn).upsert(
            tool_id="tool_b",
            source_id=SOURCE_ID,
            original_name="list_orders",
            exposed_name="orders",
            mode="materialize",
            schedule="0 * * * *",
            enabled=False,
        )
        _run_full(system_conn, e2e_env, monkeypatch, batch="v2")

        meta, views = _extract_state(e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb")
        assert meta == {"accounts"}
        assert views["accounts"] == ["accounts-v2-1", "accounts-v2-2"]


class TestOrchestratorRebuildAfterTargetedRun:
    def test_rebuild_sees_both_tables(self, system_conn, e2e_env, monkeypatch):
        _run_full(system_conn, e2e_env, monkeypatch, batch="v1")

        monkeypatch.setattr(mcp_extractor, "_materialize_one_tool", _make_fake_materialize("v2"))
        mcp_extractor.extract_source(
            system_conn=system_conn,
            source_id=SOURCE_ID,
            only_tool_id="tool_a",
            output_root=e2e_env["extracts_dir"] / SOURCE_NAME,
        )

        from src.orchestrator import SyncOrchestrator

        views_by_source = SyncOrchestrator().rebuild()
        assert set(views_by_source.get(SOURCE_NAME, [])) == {"accounts", "orders"}

        # Master views reference the ATTACHed extract (schema "crm") — attach
        # it the way the serving layer does before querying through them.
        extract_path = e2e_env["extracts_dir"] / SOURCE_NAME / "extract.duckdb"
        conn = duckdb.connect(e2e_env["analytics_db"], read_only=True)
        try:
            conn.execute(f"ATTACH '{extract_path}' AS {SOURCE_NAME} (READ_ONLY)")
            accounts = {r[0] for r in conn.execute('SELECT id FROM "accounts"').fetchall()}
            orders = {r[0] for r in conn.execute('SELECT id FROM "orders"').fetchall()}
        finally:
            conn.close()
        assert accounts == {"accounts-v2-1", "accounts-v2-2"}
        assert orders == {"orders-v1-1", "orders-v1-2"}


def test_carry_forward_keeps_dashed_table_names(tmp_path, monkeypatch):
    """MCP exposed names routinely carry dashes (`crm_get-library-docs`), and
    the write path accepts them — carry-forward must use the quoted-identifier
    validator so a targeted run doesn't drop tables a full run wrote fine
    (Devin Review on #1119)."""
    import duckdb

    from connectors.mcp.extractor import _carry_forward_untouched

    output_root = tmp_path / "out"
    (output_root / "data").mkdir(parents=True)
    # A parquet must exist for the row to be carried.
    for name in ("crm_get-library-docs", "plain_table"):
        duckdb.connect().execute(
            f"COPY (SELECT 1 AS x) TO '{output_root / 'data' / (name + '.parquet')}' (FORMAT PARQUET)"
        )

    prev_db = tmp_path / "prev.duckdb"
    prev = duckdb.connect(str(prev_db))
    prev.execute(
        "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    for name in ("crm_get-library-docs", "plain_table"):
        prev.execute("INSERT INTO _meta VALUES (?, '', 1, 10, now(), 'local')", [name])
    prev.close()

    out_conn = duckdb.connect(str(tmp_path / "new.duckdb"))
    out_conn.execute(
        "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    carried = _carry_forward_untouched(
        out_conn,
        prev_db_path=prev_db,
        output_root=output_root,
        keep={"crm_get-library-docs", "plain_table"},
        exclude=set(),
    )
    out_conn.close()

    assert set(carried) == {"crm_get-library-docs", "plain_table"}


def test_full_run_keeps_last_known_good_for_a_failed_tool(tmp_path, monkeypatch):
    """A full run must not vaporize a healthy table because one tool's
    upstream call was flaky — the failed tool keeps its last-known-good
    _meta/view, while a tool removed from the registry still drops out
    (Devin Review on #1119)."""
    import duckdb

    from connectors.mcp.extractor import _carry_forward_untouched

    output_root = tmp_path / "out"
    (output_root / "data").mkdir(parents=True)
    for name in ("tool_ok", "tool_flaky", "tool_removed"):
        duckdb.connect().execute(
            f"COPY (SELECT 1 AS x) TO '{output_root / 'data' / (name + '.parquet')}' (FORMAT PARQUET)"
        )

    prev_db = tmp_path / "prev.duckdb"
    prev = duckdb.connect(str(prev_db))
    prev.execute(
        "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    for name in ("tool_ok", "tool_flaky", "tool_removed"):
        prev.execute("INSERT INTO _meta VALUES (?, '', 1, 10, now(), 'local')", [name])
    prev.close()

    out_conn = duckdb.connect(str(tmp_path / "new.duckdb"))
    out_conn.execute(
        "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    # This run wrote tool_ok; tool_flaky raised; tool_removed left the registry.
    carried = _carry_forward_untouched(
        out_conn,
        prev_db_path=prev_db,
        output_root=output_root,
        keep={"tool_flaky"},
        exclude={"tool_ok"},
    )
    out_conn.close()

    assert carried == ["tool_flaky"]
    assert "tool_removed" not in carried
