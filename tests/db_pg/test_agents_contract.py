"""Cross-engine contract tests for the agents repository (v103).

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both
backends; the same return shapes must come back — in particular the JSON
columns (knowledge/plugins/surfaces) must come back DECODED as list/dict on
both engines, never as a raw JSON string.

Follows the pattern established in test_file_corpora_contract.py.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.agents import AgentsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentsRepository(conn), conn


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

    from src.repositories.agents_pg import AgentsPgRepository

    return AgentsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields an agents repo bound to either DuckDB or PG."""
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
# contract tests
# ---------------------------------------------------------------------------


def test_create_then_get_returns_same_shape(repo):
    agent_id = repo.create(
        name="Revenue Analyst",
        slug="revenue-analyst",
        created_by="user1",
        role="Finance partner",
        instructions="Always cite the metric definition.",
        tone="formal",
        greeting="What do you need?",
    )
    row = repo.get(agent_id)
    assert row is not None
    assert row["id"] == agent_id
    assert row["slug"] == "revenue-analyst"
    assert row["name"] == "Revenue Analyst"
    assert row["role"] == "Finance partner"
    assert row["instructions"] == "Always cite the metric definition."
    assert row["tone"] == "formal"
    assert row["greeting"] == "What do you need?"
    assert row["created_by"] == "user1"
    assert row["status"] == "draft"
    assert row["deleted_at"] is None


def test_create_id_has_agt_prefix(repo):
    assert repo.create(name="X", slug="x", created_by="u").startswith("agt_")


def test_create_ids_are_unique(repo):
    id1 = repo.create(name="A", slug="a", created_by="u")
    id2 = repo.create(name="B", slug="b", created_by="u")
    assert id1 != id2


def test_json_columns_round_trip_decoded(repo):
    """knowledge/plugins are lists and surfaces is a dict on BOTH engines."""
    agent_id = repo.create(
        name="Scoped",
        slug="scoped",
        created_by="u",
        knowledge=["col_abc", "pkg_def"],
        plugins=["marketplace/thing"],
        surfaces={"web": True, "slack": True, "telegram": False, "cli": False, "mcp": False},
    )
    row = repo.get(agent_id)
    assert row["knowledge"] == ["col_abc", "pkg_def"]
    assert row["plugins"] == ["marketplace/thing"]
    assert row["surfaces"]["slack"] is True
    assert row["surfaces"]["web"] is True
    assert row["surfaces"]["mcp"] is False


def test_json_columns_default_when_omitted(repo):
    row = repo.get(repo.create(name="Bare", slug="bare", created_by="u"))
    assert row["knowledge"] == []
    assert row["plugins"] == []
    # Web chat is the always-on baseline surface.
    assert row["surfaces"]["web"] is True


def test_get_returns_none_when_missing(repo):
    assert repo.get("agt_nonexistent") is None


def test_get_by_slug_resolves(repo):
    agent_id = repo.create(name="A", slug="a-slug", created_by="u")
    found = repo.get_by_slug("a-slug")
    assert found is not None
    assert found["id"] == agent_id


def test_get_by_slug_excludes_soft_deleted(repo):
    agent_id = repo.create(name="Ghost", slug="ghost", created_by="u")
    repo.soft_delete(agent_id)
    assert repo.get_by_slug("ghost") is None


def test_get_by_slug_include_deleted_sees_soft_deleted(repo):
    """The escape hatch the free-slug search relies on.

    ``slug`` is UNIQUE across deleted rows too, so a soft-deleted agent still
    owns its slug at the storage layer. Both backends must therefore be able to
    see it — otherwise ``_unique_slug`` hands out a taken slug and the next
    INSERT raises a constraint error."""
    agent_id = repo.create(name="Ghost", slug="ghost2", created_by="u")
    repo.soft_delete(agent_id)
    found = repo.get_by_slug("ghost2", include_deleted=True)
    assert found is not None
    assert found["id"] == agent_id


def test_list_scopes_to_created_by(repo):
    repo.create(name="Mine", slug="mine", created_by="owner")
    repo.create(name="Theirs", slug="theirs", created_by="other")
    names = {r["name"] for r in repo.list(created_by="owner")}
    assert "Mine" in names
    assert "Theirs" not in names


def test_list_excludes_soft_deleted(repo):
    live = repo.create(name="Live", slug="live", created_by="u")
    dead = repo.create(name="Dead", slug="dead", created_by="u")
    repo.soft_delete(dead)
    ids = {r["id"] for r in repo.list()}
    assert live in ids
    assert dead not in ids


def test_list_search_filters_by_name(repo):
    repo.create(name="Finance Bot", slug="finance", created_by="u")
    repo.create(name="Marketing Bot", slug="marketing", created_by="u")
    names = {r["name"] for r in repo.list(search="Finance")}
    assert "Finance Bot" in names
    assert "Marketing Bot" not in names


def test_update_patches_mutable_fields(repo):
    agent_id = repo.create(name="Before", slug="upd", created_by="u")
    assert repo.update(agent_id, name="After", tone="playful", status="active") is True
    row = repo.get(agent_id)
    assert row["name"] == "After"
    assert row["tone"] == "playful"
    assert row["status"] == "active"


def test_update_replaces_json_columns(repo):
    agent_id = repo.create(name="J", slug="j", created_by="u", knowledge=["old"])
    repo.update(agent_id, knowledge=["new_a", "new_b"], plugins=["p"])
    row = repo.get(agent_id)
    assert row["knowledge"] == ["new_a", "new_b"]
    assert row["plugins"] == ["p"]


def test_update_ignores_server_owned_fields(repo):
    """A hostile payload can't reassign ownership or the slug."""
    agent_id = repo.create(name="Owned", slug="owned", created_by="owner")
    repo.update(agent_id, created_by="attacker", slug="hijacked", id="agt_evil")
    row = repo.get(agent_id)
    assert row["created_by"] == "owner"
    assert row["slug"] == "owned"


def test_update_returns_false_when_missing(repo):
    assert repo.update("agt_nonexistent", name="X") is False


def test_update_returns_false_for_soft_deleted(repo):
    agent_id = repo.create(name="Gone", slug="gone", created_by="u")
    repo.soft_delete(agent_id)
    assert repo.update(agent_id, name="Zombie") is False


def test_soft_delete_sets_deleted_at(repo):
    agent_id = repo.create(name="ToDelete", slug="to-delete", created_by="u")
    repo.soft_delete(agent_id)
    assert repo.get(agent_id) is None
    row = repo.get(agent_id, include_deleted=True)
    assert row is not None
    assert row["deleted_at"] is not None
    assert row["updated_at"] >= row["created_at"]


def test_count_for_user_counts_live_only(repo):
    repo.create(name="A", slug="ca", created_by="owner")
    dead = repo.create(name="B", slug="cb", created_by="owner")
    repo.create(name="C", slug="cc", created_by="other")
    assert repo.count_for_user("owner") == 2
    repo.soft_delete(dead)
    assert repo.count_for_user("owner") == 1
    assert repo.count_for_user("nobody") == 0
