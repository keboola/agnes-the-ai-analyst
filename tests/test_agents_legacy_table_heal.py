"""The pre-merge paper-theme ``agents`` table heals into main's canonical shape.

Before PR #1113 combined the two agent features, the paper-theme branch owned
its own ``agents`` table (``created_by`` / ``instructions`` / globally-UNIQUE
``slug``). Such a database arrives at the merge already stamped past v101, so
``_v100_to_v101``'s ``CREATE TABLE IF NOT EXISTS`` never lays down main's shape
and ``_v109_to_v110`` finds the superset columns already there — leaving the
table permanently in the old shape while ``src/repositories/agents.py`` writes
the canonical one. Every ``POST /api/agents`` then 500s with ``Binder Error:
Table "agents" does not have a column with name "owner_user_id"``, which is the
/agents builder's "Build an agent" button doing nothing.

``_heal_legacy_agents_table`` is stamp-independent for exactly that reason, so
these tests drive it through ``_ensure_schema`` on an already-current stamp.
"""

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema
from src.repositories.agents import AgentsRepository

# The paper-theme branch's own agents DDL, verbatim from src/db.py at
# 777aca43^ (`_AGENTS_CREATE_SQL`, its v103). Frozen here on purpose: this is
# the historical shape the heal has to recognize, so it must NOT be regenerated
# from anything the current tree still ships.
_LEGACY_DDL = """
CREATE TABLE agents (
    id              VARCHAR PRIMARY KEY,
    slug            VARCHAR UNIQUE NOT NULL,
    name            VARCHAR NOT NULL,
    role            VARCHAR DEFAULT '',
    instructions    TEXT DEFAULT '',
    tone            VARCHAR DEFAULT 'concise',
    greeting        TEXT DEFAULT '',
    knowledge       TEXT DEFAULT '[]',
    plugins         TEXT DEFAULT '[]',
    surfaces        TEXT DEFAULT '{}',
    status          VARCHAR NOT NULL DEFAULT 'draft',
    created_by      VARCHAR NOT NULL,
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    deleted_at      TIMESTAMP
);
"""


def _columns(conn: duckdb.DuckDBPyConnection) -> set:
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"
        ).fetchall()
    }


@pytest.fixture
def legacy_db(tmp_path):
    """A DuckDB file carrying the legacy table and a fully-current stamp."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version VALUES (?, current_timestamp)", [SCHEMA_VERSION])
    conn.execute(_LEGACY_DDL)
    conn.execute(
        """INSERT INTO agents
           (id, slug, name, role, instructions, tone, greeting,
            knowledge, plugins, surfaces, status, created_by, deleted_at)
           VALUES ('agt_1', 'revenue-analyst', 'Revenue Analyst', 'Answers revenue questions.',
                   'Cite the table you queried.', 'precise', 'Hi.',
                   '["k1"]', '["p1"]', '{"web": true}', 'ready', 'user-1', NULL),
                  ('agt_2', 'retired', 'Retired', '', '', 'concise', '',
                   '[]', '[]', '{}', 'draft', 'user-2', current_timestamp)"""
    )
    yield conn
    conn.close()


def test_legacy_shape_is_rebuilt_despite_a_current_stamp(legacy_db):
    assert "created_by" in _columns(legacy_db)

    _ensure_schema(legacy_db)

    cols = _columns(legacy_db)
    assert "owner_user_id" in cols
    assert "system_prompt" in cols
    assert "created_by" not in cols
    assert "instructions" not in cols
    # The scratch table the rebuild renames through must not survive it.
    tables = {r[0] for r in legacy_db.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert "agents_legacy_heal" not in tables


def test_rebuild_carries_every_row_and_authored_field(legacy_db):
    _ensure_schema(legacy_db)

    row = legacy_db.execute(
        """SELECT owner_user_id, name, slug, system_prompt, role, tone, greeting,
                  knowledge, plugins, surfaces, status, deleted_at
           FROM agents WHERE id = 'agt_1'"""
    ).fetchone()
    assert row == (
        "user-1",
        "Revenue Analyst",
        "revenue-analyst",
        "Cite the table you queried.",
        "Answers revenue questions.",
        "precise",
        "Hi.",
        '["k1"]',
        '["p1"]',
        '{"web": true}',
        "ready",
        None,
    )
    # Soft-deleted rows come across as tombstones, not as live agents — the
    # (owner_user_id, slug) UNIQUE spans them, so dropping them here would let a
    # later create reuse a taken slug and hit the constraint.
    assert legacy_db.execute("SELECT count(*) FROM agents").fetchone()[0] == 2
    assert legacy_db.execute("SELECT deleted_at IS NOT NULL FROM agents WHERE id = 'agt_2'").fetchone()[0]

    # Columns only main's shape has take their declared defaults.
    assert legacy_db.execute(
        "SELECT plugins_mode, memory_write_mode, is_default FROM agents WHERE id = 'agt_1'"
    ).fetchone() == (
        "all",
        "propose",
        False,
    )


def test_healed_table_accepts_the_repository_insert_that_used_to_500(legacy_db):
    """The actual regression: POST /api/agents writes through this repository."""
    _ensure_schema(legacy_db)

    AgentsRepository(legacy_db).create(
        id="agt_new",
        owner_user_id="user-1",
        name="",
        slug="agent",
        system_prompt="",
        role="",
        tone="concise",
        greeting="",
        knowledge="[]",
        plugins="[]",
        surfaces='{"web": true}',
        status="draft",
    )

    created = AgentsRepository(legacy_db).get_by_id("agt_new")
    assert created is not None
    assert created["owner_user_id"] == "user-1"
    assert created["status"] == "draft"


def test_heal_is_idempotent_and_leaves_a_canonical_table_alone(legacy_db):
    _ensure_schema(legacy_db)
    before = legacy_db.execute("SELECT id, owner_user_id FROM agents ORDER BY id").fetchall()

    _ensure_schema(legacy_db)
    _ensure_schema(legacy_db)

    assert legacy_db.execute("SELECT id, owner_user_id FROM agents ORDER BY id").fetchall() == before


def test_fresh_install_is_untouched(tmp_path):
    """No legacy table, nothing to heal — and the canonical shape is intact."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)

    cols = _columns(conn)
    assert "owner_user_id" in cols
    assert "created_by" not in cols
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 0
    conn.close()
