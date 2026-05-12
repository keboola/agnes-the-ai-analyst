"""UsageAttributionRepository — replace, delete, lookup, precedence."""
import duckdb
import pytest

from src.db import _ensure_schema as init_database
from src.repositories.usage_attribution import UsageAttributionRepository


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
