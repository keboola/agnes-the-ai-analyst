"""UsageAttributionRepository — replace, delete, lookup, precedence.

Also covers ``src.usage_attribution_helpers.update_flea_attribution`` and
``delete_flea_attribution`` as the public orchestration layer.
"""
import duckdb
import pytest

from src.db import _ensure_schema as init_database
from src.repositories.usage_attribution import UsageAttributionRepository
from src.usage_attribution_helpers import (
    delete_flea_attribution,
    update_flea_attribution,
)


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "attr.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def test_replace_for_curated_inserts_all_three_kinds(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "myplug", skills=["s1", "s2"], agents=["a1"], commands=["/c1"])
    assert conn.execute("SELECT count(*) FROM usage_attribution_skills").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM usage_attribution_agents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM usage_attribution_commands").fetchone()[0] == 1


def test_replace_is_idempotent_and_replaces(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "plug", skills=["a", "b"])
    repo.replace_for_curated("mp1", "plug", skills=["b", "c"])
    names = {r[0] for r in conn.execute(
        "SELECT skill_name FROM usage_attribution_skills WHERE ref_id='mp1/plug'"
    ).fetchall()}
    assert names == {"b", "c"}  # 'a' removed


def test_replace_scopes_by_ref_id(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "plug-a", skills=["foo"])
    repo.replace_for_curated("mp1", "plug-b", skills=["bar"])
    repo.replace_for_curated("mp1", "plug-a", skills=["baz"])  # only plug-a affected
    a = {r[0] for r in conn.execute(
        "SELECT skill_name FROM usage_attribution_skills WHERE ref_id='mp1/plug-a'"
    ).fetchall()}
    b = {r[0] for r in conn.execute(
        "SELECT skill_name FROM usage_attribution_skills WHERE ref_id='mp1/plug-b'"
    ).fetchall()}
    assert a == {"baz"}
    assert b == {"bar"}


def test_replace_for_flea_separates_from_curated(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "shared-name", skills=["s"])
    repo.replace_for_flea("entity-123", skills=["s"])
    rows = conn.execute(
        "SELECT source, ref_id FROM usage_attribution_skills WHERE skill_name='s'"
    ).fetchall()
    sources = {r[0] for r in rows}
    assert sources == {"curated", "flea"}


def test_delete_for_flea_removes_all_three_kinds(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_flea("e1", skills=["s"], agents=["a"], commands=["/c"])
    repo.delete_for_flea("e1")
    for tbl in ("usage_attribution_skills", "usage_attribution_agents", "usage_attribution_commands"):
        assert conn.execute(
            f"SELECT count(*) FROM {tbl} WHERE ref_id='e1'"
        ).fetchone()[0] == 0


def test_lookup_skill_curated_only(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "plug", skills=["my-skill"])
    assert repo.lookup(skill_name="my-skill") == ("curated", "mp1/plug")


def test_lookup_returns_curated_when_both_sources_match(conn):
    """Precedence: curated wins over flea."""
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp1", "plug", skills=["shared"])
    repo.replace_for_flea("e1", skills=["shared"])
    src, ref = repo.lookup(skill_name="shared")
    assert src == "curated"


def test_lookup_returns_none_for_unknown(conn):
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(skill_name="does-not-exist") is None


def test_lookup_requires_exactly_one_kwarg(conn):
    repo = UsageAttributionRepository(conn)
    with pytest.raises(ValueError):
        repo.lookup()
    with pytest.raises(ValueError):
        repo.lookup(skill_name="a", agent_name="b")


def test_replace_dedupes_input(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp", "plug", skills=["a", "a", "b", "", None])
    names = {r[0] for r in conn.execute(
        "SELECT skill_name FROM usage_attribution_skills"
    ).fetchall()}
    assert names == {"a", "b"}


def test_lookup_agent_and_command(conn):
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp", "plug", agents=["my-agent"], commands=["/my-cmd"])
    assert repo.lookup(agent_name="my-agent") == ("curated", "mp/plug")
    assert repo.lookup(command_name="/my-cmd") == ("curated", "mp/plug")


def test_replace_with_empty_inputs_clears_rows(conn):
    """replace with no items clears existing rows for that (source, ref_id)."""
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp", "plug", skills=["s1", "s2"])
    repo.replace_for_curated("mp", "plug")  # empty — should wipe
    count = conn.execute(
        "SELECT count(*) FROM usage_attribution_skills WHERE ref_id='mp/plug'"
    ).fetchone()[0]
    assert count == 0


def test_delete_for_flea_is_idempotent(conn):
    """Deleting a non-existent entity does not raise."""
    repo = UsageAttributionRepository(conn)
    repo.delete_for_flea("nonexistent-id")  # should not raise


def test_replace_does_not_cross_contaminate_other_plugins(conn):
    """Replace for plug-a does not affect plug-b's rows."""
    repo = UsageAttributionRepository(conn)
    repo.replace_for_curated("mp", "plug-a", skills=["x"])
    repo.replace_for_curated("mp", "plug-b", skills=["y"])
    # Now replace plug-a with nothing
    repo.replace_for_curated("mp", "plug-a")
    assert repo.lookup(skill_name="y") == ("curated", "mp/plug-b")
    assert repo.lookup(skill_name="x") is None


# ---------------------------------------------------------------------------
# update_flea_attribution / delete_flea_attribution helpers
# ---------------------------------------------------------------------------


def test_update_flea_attribution_skill(conn):
    """update_flea_attribution records a skill row for type='skill'."""
    update_flea_attribution(conn, "e1", "skill", "my-skill-by-alice")
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(skill_name="my-skill-by-alice") == ("flea", "e1")


def test_update_flea_attribution_agent(conn):
    """update_flea_attribution records an agent row for type='agent'."""
    update_flea_attribution(conn, "e2", "agent", "my-agent-by-bob")
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(agent_name="my-agent-by-bob") == ("flea", "e2")


def test_update_flea_attribution_rename_roundtrip(conn):
    """Rename: old name no longer resolves; new name does.

    Simulates the metadata-only rename path in update_entity:
      1. Entity created with name 'old-skill-by-alice'
      2. Entity renamed to 'new-skill-by-alice'
      3. Old lookup → None; new lookup → ('flea', entity_id)
    """
    entity_id = "e-rename-1"
    # Step 1: initial registration
    update_flea_attribution(conn, entity_id, "skill", "old-skill-by-alice")
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(skill_name="old-skill-by-alice") == ("flea", entity_id)

    # Step 2: rename — re-run helper with the new name
    update_flea_attribution(conn, entity_id, "skill", "new-skill-by-alice")

    # Step 3: old name gone, new name resolves
    assert repo.lookup(skill_name="old-skill-by-alice") is None
    assert repo.lookup(skill_name="new-skill-by-alice") == ("flea", entity_id)


def test_update_flea_attribution_unknown_type_falls_back_to_skill(conn):
    """Unknown entity type records a skill row (best-effort fallback)."""
    update_flea_attribution(conn, "e3", "unknown_type", "some-thing-by-user")
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(skill_name="some-thing-by-user") == ("flea", "e3")


def test_delete_flea_attribution_removes_rows(conn):
    """delete_flea_attribution wipes all three kinds for the entity."""
    update_flea_attribution(conn, "e4", "skill", "skill-to-delete")
    delete_flea_attribution(conn, "e4")
    repo = UsageAttributionRepository(conn)
    assert repo.lookup(skill_name="skill-to-delete") is None


def test_update_flea_attribution_is_best_effort_on_bad_conn(conn):
    """Passing a closed connection must not raise — failures are swallowed."""
    conn.close()
    # Should not raise even though the connection is closed
    update_flea_attribution(conn, "e5", "skill", "irrelevant")
    delete_flea_attribution(conn, "e5")
