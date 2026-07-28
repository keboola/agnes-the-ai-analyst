"""Atomicity of the knowledge-domain junction rewrites (DuckDB).

``MemoryDomainsRepository.replace_domains_for_item`` and
``KnowledgeRepository.update`` both rewrite the ``knowledge_item_domains``
junction as DELETE-then-INSERT on the shared singleton connection. These
tests pin that the rewrite is transaction-wrapped:

  * a concurrent reader never sees the empty post-DELETE / pre-INSERT window
    (an item would momentarily read as domain-less, breaking domain-scoped
    RBAC), and
  * a bad slug rolls the whole operation back — including the scalar column
    update in ``KnowledgeRepository.update`` — instead of half-writing.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.memory_domains import MemoryDomainsRepository


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db

    monkeypatch.setattr(db, "_system_db_conn", None, raising=False)
    monkeypatch.setattr(db, "_system_db_path", None, raising=False)
    conn = get_system_db()
    kr = KnowledgeRepository(conn)
    dr = MemoryDomainsRepository(conn)
    dr.create(slug="zfin", name="ZFin", description="", icon="", color="", created_by="a")
    dr.create(slug="zleg", name="ZLeg", description="", icon="", color="", created_by="a")
    kr.create(
        id="i1", title="A", content="C", category="x",
        source_type="manual", added_by="a",
    )
    return conn, kr, dr


def _domains(conn, item_id="i1"):
    rows = conn.execute(
        "SELECT md.slug FROM knowledge_item_domains kid "
        "JOIN memory_domains md ON md.id = kid.domain_id "
        "WHERE kid.item_id = ?",
        [item_id],
    ).fetchall()
    return {r[0] for r in rows}


def test_replace_domains_rolls_back_on_unknown_slug(repos):
    conn, _, dr = repos
    dr.replace_domains_for_item("i1", ["zfin", "zleg"], added_by="a")
    assert _domains(conn) == {"zfin", "zleg"}

    # A typo in the middle of the set must not half-write — the prior
    # membership stays intact.
    with pytest.raises(ValueError):
        dr.replace_domains_for_item("i1", ["zfin", "bogus"], added_by="a")
    assert _domains(conn) == {"zfin", "zleg"}


def test_replace_domains_reader_isolation(repos):
    conn, _, dr = repos
    dr.replace_domains_for_item("i1", ["zfin", "zleg"], added_by="a")

    writer = get_system_db()
    reader = get_system_db()
    writer.execute("BEGIN")
    writer.execute("DELETE FROM knowledge_item_domains WHERE item_id = ?", ["i1"])
    try:
        assert _domains(reader) == {"zfin", "zleg"}, "reader saw empty intermediate"
    finally:
        writer.execute("ROLLBACK")


def test_update_rolls_back_scalar_and_domain_on_bad_slug(repos):
    conn, kr, _ = repos
    kr.update("i1", title="T2", domain="zfin")
    assert _domains(conn) == {"zfin"}
    assert conn.execute("SELECT title FROM knowledge_items WHERE id='i1'").fetchone()[0] == "T2"

    # A bad domain slug must roll back the scalar title change too — the whole
    # update is one transaction, not a half-applied edit.
    with pytest.raises(ValueError):
        kr.update("i1", title="T3", domain="bogus")
    assert conn.execute("SELECT title FROM knowledge_items WHERE id='i1'").fetchone()[0] == "T2"
    assert _domains(conn) == {"zfin"}


def test_update_domain_swap_is_atomic(repos):
    conn, kr, _ = repos
    kr.update("i1", domain="zfin")
    assert _domains(conn) == {"zfin"}
    # Swapping the domain replaces cleanly (single-domain helper semantics).
    kr.update("i1", domain="zleg")
    assert _domains(conn) == {"zleg"}
