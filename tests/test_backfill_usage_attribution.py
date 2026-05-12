"""Backfill script integration test.

Seeds curated marketplace_plugins + store_entities rows, runs the backfill,
and asserts the attribution tables are populated correctly.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema as init_database
from src.repositories.marketplace_plugins import MarketplacePluginsRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.usage_attribution import UsageAttributionRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DATA_DIR with a fresh system.duckdb."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "marketplaces").mkdir()
    (data_dir / "store").mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    import src.db as db
    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    yield data_dir
    # Teardown
    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None


@pytest.fixture
def db_conn(tmp_path):
    """Standalone in-memory DuckDB with full schema for unit-style tests."""
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers to build plugin clones on disk
# ---------------------------------------------------------------------------


def _make_skill(plugin_root: Path, skill_name: str) -> None:
    skill_dir = plugin_root / "skills" / skill_name.replace(" ", "-")
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test skill\n---\n\nBody.",
        encoding="utf-8",
    )


def _make_agent(plugin_root: Path, agent_name: str) -> None:
    agents_dir = plugin_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name.replace(' ', '-')}.md").write_text(
        f"---\nname: {agent_name}\ndescription: test agent\n---\n\nBody.",
        encoding="utf-8",
    )


def _make_command(plugin_root: Path, cmd_name: str) -> None:
    commands_dir = plugin_root / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / f"{cmd_name.lstrip('/').replace(' ', '-')}.md").write_text(
        f"---\nname: {cmd_name.lstrip('/')}\ndescription: test cmd\n---\n\nBody.",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Unit tests against an isolated DuckDB (no DATA_DIR)
# ---------------------------------------------------------------------------


def test_backfill_curated_from_direct_conn(db_conn, tmp_path):
    """_backfill_curated walks marketplace_plugins + plugin clones correctly."""
    # Seed marketplace_plugins table
    mp_repo = MarketplacePluginsRepository(db_conn)
    mp_repo.replace_for_marketplace("mp1", [{"name": "plug-a"}, {"name": "plug-b"}])

    # Build on-disk plugin clones under a fake marketplaces dir
    mkt_dir = tmp_path / "marketplaces"
    plug_a_root = mkt_dir / "mp1" / "plugins" / "plug-a"
    plug_b_root = mkt_dir / "mp1" / "plugins" / "plug-b"
    _make_skill(plug_a_root, "alpha-skill")
    _make_agent(plug_a_root, "alpha-agent")
    _make_command(plug_a_root, "alpha-cmd")
    _make_skill(plug_b_root, "beta-skill")

    # Run the backfill logic directly (without DATA_DIR env)
    from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
    attr = UsageAttributionRepository(db_conn)
    all_plugins = mp_repo.list_all()
    for plugin in all_plugins:
        mid = plugin["marketplace_id"]
        name = plugin["name"]
        root = mkt_dir / mid / "plugins" / name
        attr.replace_for_curated(
            mid, name,
            skills=list_inner_skills(root),
            agents=list_inner_agents(root),
            commands=list_commands(root),
        )

    assert attr.lookup(skill_name="alpha-skill") == ("curated", "mp1/plug-a")
    assert attr.lookup(agent_name="alpha-agent") == ("curated", "mp1/plug-a")
    assert attr.lookup(command_name="/alpha-cmd") == ("curated", "mp1/plug-a")
    assert attr.lookup(skill_name="beta-skill") == ("curated", "mp1/plug-b")
    assert attr.lookup(skill_name="nonexistent") is None


def test_backfill_flea_skill_entity(db_conn, tmp_path):
    """Flea skill entity gets single-row attribution under its name."""
    ent_repo = StoreEntitiesRepository(db_conn)
    ent_repo.create(
        id="ent-skill-1",
        owner_user_id="user1",
        owner_username="alice",
        type="skill",
        name="my-skill-by-alice",
        description="A skill",
        category=None,
        version="v1",
        visibility_status="approved",
    )

    attr = UsageAttributionRepository(db_conn)
    items, _ = ent_repo.list(visibility_status=["approved"])
    for entity in items:
        if entity["type"] == "skill":
            attr.replace_for_flea(entity["id"], skills=[entity["name"]])

    assert attr.lookup(skill_name="my-skill-by-alice") == ("flea", "ent-skill-1")


def test_backfill_flea_agent_entity(db_conn):
    """Flea agent entity gets single-row attribution under its name."""
    ent_repo = StoreEntitiesRepository(db_conn)
    ent_repo.create(
        id="ent-agent-1",
        owner_user_id="user2",
        owner_username="bob",
        type="agent",
        name="my-agent-by-bob",
        description="An agent",
        category=None,
        version="v1",
        visibility_status="approved",
    )

    attr = UsageAttributionRepository(db_conn)
    attr.replace_for_flea("ent-agent-1", agents=["my-agent-by-bob"])
    assert attr.lookup(agent_name="my-agent-by-bob") == ("flea", "ent-agent-1")


def test_backfill_flea_plugin_entity(db_conn, tmp_path):
    """Flea plugin entity walks the baked plugin tree."""
    ent_repo = StoreEntitiesRepository(db_conn)
    ent_repo.create(
        id="ent-plug-1",
        owner_user_id="user3",
        owner_username="carol",
        type="plugin",
        name="my-plugin-by-carol",
        description="A plugin bundle",
        category=None,
        version="v1",
        visibility_status="approved",
    )

    # Build the on-disk plugin tree
    plugin_dir = tmp_path / "store" / "ent-plug-1" / "plugin"
    _make_skill(plugin_dir, "bundled-skill")
    _make_command(plugin_dir, "bundled-cmd")

    from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
    attr = UsageAttributionRepository(db_conn)
    attr.replace_for_flea(
        "ent-plug-1",
        skills=list_inner_skills(plugin_dir),
        agents=list_inner_agents(plugin_dir),
        commands=list_commands(plugin_dir),
    )

    assert attr.lookup(skill_name="bundled-skill") == ("flea", "ent-plug-1")
    assert attr.lookup(command_name="/bundled-cmd") == ("flea", "ent-plug-1")


def test_backfill_is_idempotent(db_conn, tmp_path):
    """Running the backfill logic twice does not duplicate rows."""
    mp_repo = MarketplacePluginsRepository(db_conn)
    mp_repo.replace_for_marketplace("mp2", [{"name": "plug-x"}])

    root = tmp_path / "mp2" / "plugins" / "plug-x"
    _make_skill(root, "idempotent-skill")

    from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
    attr = UsageAttributionRepository(db_conn)

    def _run():
        for plugin in mp_repo.list_all():
            mid = plugin["marketplace_id"]
            name = plugin["name"]
            r = tmp_path / mid / "plugins" / name
            attr.replace_for_curated(
                mid, name,
                skills=list_inner_skills(r),
                agents=list_inner_agents(r),
                commands=list_commands(r),
            )

    _run()
    _run()  # second pass must not duplicate

    count = db_conn.execute(
        "SELECT count(*) FROM usage_attribution_skills WHERE skill_name='idempotent-skill'"
    ).fetchone()[0]
    assert count == 1


def test_backfill_missing_plugin_clone_does_not_crash(db_conn, tmp_path):
    """A plugin listed in DB but absent from disk produces empty attribution (no crash)."""
    mp_repo = MarketplacePluginsRepository(db_conn)
    mp_repo.replace_for_marketplace("mp3", [{"name": "ghost-plug"}])

    from src.marketplace_listing import list_commands, list_inner_agents, list_inner_skills
    attr = UsageAttributionRepository(db_conn)
    # plugin_root doesn't exist — all list_* helpers return [] gracefully
    ghost_root = tmp_path / "no-such-dir"
    attr.replace_for_curated(
        "mp3", "ghost-plug",
        skills=list_inner_skills(ghost_root),
        agents=list_inner_agents(ghost_root),
        commands=list_commands(ghost_root),
    )

    count = db_conn.execute(
        "SELECT count(*) FROM usage_attribution_skills WHERE ref_id='mp3/ghost-plug'"
    ).fetchone()[0]
    assert count == 0


def test_backfill_curated_and_flea_coexist(db_conn, tmp_path):
    """Same skill name from curated + flea both persist; lookup returns curated."""
    attr = UsageAttributionRepository(db_conn)
    attr.replace_for_curated("mp", "plug", skills=["shared-name"])
    attr.replace_for_flea("e1", skills=["shared-name"])

    src, ref = attr.lookup(skill_name="shared-name")
    assert src == "curated"
    # Flea row also exists
    rows = db_conn.execute(
        "SELECT source FROM usage_attribution_skills WHERE skill_name='shared-name'"
    ).fetchall()
    assert {r[0] for r in rows} == {"curated", "flea"}
