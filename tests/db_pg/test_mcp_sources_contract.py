"""Cross-engine contract tests for the ``mcp_sources`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

Follows the pattern established in ``test_data_packages_contract.py``.
Closes the Devin Review follow-up on PR #474 (cross-engine contract
tests missing for the 3 new repository pairs landed by Cowork + MCP).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MCPSourceRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
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

    from src.repositories.mcp_sources_pg import MCPSourcePgRepository
    return MCPSourcePgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields an ``mcp_sources`` repo bound to either DuckDB or PG."""
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
    repo.upsert(
        id="s1", name="filesystem", transport="stdio",
        command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        enabled=True,
    )
    row = repo.get("s1")
    assert row is not None
    assert row["id"] == "s1"
    assert row["name"] == "filesystem"
    assert row["transport"] == "stdio"
    assert row["command"] == "npx"
    # args is JSON on the wire but the repo returns a list on both backends
    assert row["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert row["enabled"] is True
    assert row["scope"] == "shared"


def test_upsert_replaces_existing_row(repo):
    repo.upsert(id="s1", name="x", transport="http", url="https://a.example/mcp")
    repo.upsert(id="s1", name="x", transport="http", url="https://b.example/mcp", enabled=False)
    row = repo.get("s1")
    assert row is not None
    assert row["url"] == "https://b.example/mcp"
    assert row["enabled"] is False


def test_get_returns_none_for_missing_id(repo):
    assert repo.get("not-here") is None


def test_get_by_name_resolves_and_returns_none_when_missing(repo):
    repo.upsert(id="s1", name="weather", transport="stdio", command="weather-mcp")
    found = repo.get_by_name("weather")
    assert found is not None
    assert found["id"] == "s1"
    assert repo.get_by_name("does-not-exist") is None


def test_list_all_orders_and_filters_enabled_only(repo):
    repo.upsert(id="s1", name="a", transport="stdio", command="a-mcp", enabled=True)
    repo.upsert(id="s2", name="b", transport="stdio", command="b-mcp", enabled=False)
    repo.upsert(id="s3", name="c", transport="stdio", command="c-mcp", enabled=True)

    # Both backends must return the same set when enabled_only=False
    all_ids = {r["id"] for r in repo.list_all(enabled_only=False)}
    assert all_ids == {"s1", "s2", "s3"}

    # And the same filtered set when enabled_only=True
    enabled_ids = {r["id"] for r in repo.list_all(enabled_only=True)}
    assert enabled_ids == {"s1", "s3"}


def test_delete_removes_row(repo):
    repo.upsert(id="s1", name="x", transport="stdio", command="x-mcp")
    assert repo.get("s1") is not None
    repo.delete("s1")
    assert repo.get("s1") is None


def test_delete_missing_id_is_idempotent(repo):
    """Both backends must treat delete-of-nonexistent as a no-op (no raise)."""
    repo.delete("never-existed")
    # And a real row alongside it still works
    repo.upsert(id="s1", name="x", transport="stdio", command="x-mcp")
    repo.delete("s1")
    assert repo.get("s1") is None


def test_transport_validation_is_consistent(repo):
    """Both backends must reject bad transport with the same shape (ValueError)."""
    with pytest.raises(ValueError):
        repo.upsert(id="s1", name="x", transport="grpc", command="x")


def test_scope_validation_is_consistent(repo):
    with pytest.raises(ValueError):
        repo.upsert(
            id="s1", name="x", transport="stdio", command="x",
            scope="weird-scope",  # type: ignore[arg-type]
        )


def test_stdio_without_command_rejected(repo):
    with pytest.raises(ValueError):
        repo.upsert(id="s1", name="x", transport="stdio")


def test_http_without_url_rejected(repo):
    with pytest.raises(ValueError):
        repo.upsert(id="s1", name="x", transport="http")
