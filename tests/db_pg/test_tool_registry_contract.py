"""Cross-engine contract tests for the ``tool_registry`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back.

Sister of ``test_mcp_sources_contract.py`` — closes the second half of
the Devin Review follow-up on PR #474 (cross-engine contract tests for
the MCP repository pairs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _seed_source_duckdb(conn) -> None:
    from src.repositories.mcp_sources import MCPSourceRepository
    src_repo = MCPSourceRepository(conn)
    src_repo.upsert(
        id="src1", name="filesystem", transport="stdio", command="fs-mcp",
    )


def _seed_source_pg(engine) -> None:
    from src.repositories.mcp_sources_pg import MCPSourcePgRepository
    src_repo = MCPSourcePgRepository(engine)
    src_repo.upsert(
        id="src1", name="filesystem", transport="stdio", command="fs-mcp",
    )


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`)
    # so the session timezone is pinned to UTC — same rationale as in
    # `test_mcp_sources_contract.py::_make_duckdb_repo`.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.tool_registry import ToolRegistryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    _seed_source_duckdb(conn)
    return ToolRegistryRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    _seed_source_pg(db_pg.get_engine())

    from src.repositories.tool_registry_pg import ToolRegistryPgRepository
    return ToolRegistryPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a ``tool_registry`` repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------

def test_upsert_then_get_returns_same_shape(repo):
    from src.repositories.tool_registry import PASSTHROUGH

    repo.upsert(
        tool_id="t1", source_id="src1",
        original_name="read_file", exposed_name="filesystem__read_file",
        mode=PASSTHROUGH,
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        description="Read a file from the configured root.",
        enabled=True,
    )
    row = repo.get("t1")
    assert row is not None
    assert row["tool_id"] == "t1"
    assert row["source_id"] == "src1"
    assert row["original_name"] == "read_file"
    assert row["exposed_name"] == "filesystem__read_file"
    assert row["mode"] == PASSTHROUGH
    # JSON-on-wire / dict-in-app contract must hold on both backends
    assert row["input_schema"]["required"] == ["path"]
    assert row["enabled"] is True


def test_upsert_replaces_existing_row(repo):
    from src.repositories.tool_registry import PASSTHROUGH

    repo.upsert(
        tool_id="t1", source_id="src1",
        original_name="read_file", exposed_name="orig_name",
        mode=PASSTHROUGH,
    )
    repo.upsert(
        tool_id="t1", source_id="src1",
        original_name="read_file", exposed_name="new_name",
        mode=PASSTHROUGH,
        description="updated",
    )
    row = repo.get("t1")
    assert row is not None
    assert row["exposed_name"] == "new_name"
    assert row["description"] == "updated"


def test_get_returns_none_for_missing_id(repo):
    assert repo.get("not-here") is None


def test_list_for_source_filters_and_excludes_other_sources(repo):
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import PASSTHROUGH

    # Seed a second source on whichever backend the parametrized repo is bound to
    if hasattr(repo, "conn"):
        # DuckDB path
        MCPSourceRepository(repo.conn).upsert(
            id="src2", name="other", transport="stdio", command="other-mcp",
        )
    else:
        # PG path — repo holds an engine
        from src.repositories.mcp_sources_pg import MCPSourcePgRepository
        MCPSourcePgRepository(repo.engine).upsert(
            id="src2", name="other", transport="stdio", command="other-mcp",
        )

    repo.upsert(tool_id="t1", source_id="src1", original_name="a", exposed_name="a", mode=PASSTHROUGH)
    repo.upsert(tool_id="t2", source_id="src1", original_name="b", exposed_name="b", mode=PASSTHROUGH)
    repo.upsert(tool_id="t3", source_id="src2", original_name="c", exposed_name="c", mode=PASSTHROUGH)

    src1_ids = {r["tool_id"] for r in repo.list_for_source("src1")}
    src2_ids = {r["tool_id"] for r in repo.list_for_source("src2")}
    assert src1_ids == {"t1", "t2"}
    assert src2_ids == {"t3"}


def test_delete_removes_row(repo):
    from src.repositories.tool_registry import PASSTHROUGH

    repo.upsert(tool_id="t1", source_id="src1", original_name="x", exposed_name="x", mode=PASSTHROUGH)
    assert repo.get("t1") is not None
    repo.delete("t1")
    assert repo.get("t1") is None


def test_delete_missing_id_is_idempotent(repo):
    """Both backends must treat delete-of-nonexistent as a no-op (no raise)."""
    repo.delete("never-existed")


def test_mode_validation_is_consistent(repo):
    with pytest.raises(ValueError):
        repo.upsert(
            tool_id="t1", source_id="src1",
            original_name="x", exposed_name="x", mode="unknown_mode",
        )


def test_materialize_without_schedule_rejected(repo):
    from src.repositories.tool_registry import MATERIALIZE
    with pytest.raises(ValueError):
        repo.upsert(
            tool_id="t1", source_id="src1",
            original_name="x", exposed_name="x", mode=MATERIALIZE,
            # missing schedule
        )
