"""``mcp_sources.connect_hint`` (schema v92) — repo round-trip.

Per-source, admin-authored instructions telling a user where to obtain
their personal token for a ``per_user`` source. Rendered through
``app/markdown_render.render_safe`` on the connect page.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def system_db(tmp_path):
    """Fresh system.duckdb connection with the schema applied."""
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    yield conn
    conn.close()


def test_connect_hint_round_trips(system_db):
    from src.repositories.mcp_sources import MCPSourceRepository

    repo = MCPSourceRepository(system_db)
    repo.upsert(
        id="s1",
        name="src_one",
        transport="stdio",
        command="x",
        scope="per_user",
        connect_hint="Generate a token in Settings → API.",
    )
    got = repo.get("s1")
    assert got["connect_hint"] == "Generate a token in Settings → API."


def test_connect_hint_defaults_to_none(system_db):
    from src.repositories.mcp_sources import MCPSourceRepository

    repo = MCPSourceRepository(system_db)
    repo.upsert(id="s1", name="src_one", transport="stdio", command="x")
    got = repo.get("s1")
    assert got.get("connect_hint") is None
