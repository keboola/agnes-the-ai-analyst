"""Cross-engine contract tests for the memory_domains repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Follows the pattern established in ``test_data_packages_contract.py``
(Task 1D.1). Both backends are seeded with ``knowledge_items`` rows
(``ki_1``, ``ki_2``) so the ``knowledge_item_domains`` bridge tests
have valid item_ids to point at.

Schema-drift note (Task 1B.2): the PG ``knowledge_items`` table doesn't
have an ``is_required`` column yet; the PG repo's
``list_items_of_domain`` projects ``FALSE AS is_required`` for shape
parity. DuckDB seeds default ``is_required = FALSE`` too, so both sides
naturally return False here — no special-casing needed.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _seed_knowledge_items_duckdb(conn) -> None:
    for kid, title in (("ki_1", "First fact"), ("ki_2", "Second fact")):
        conn.execute(
            "INSERT INTO knowledge_items (id, title) VALUES (?, ?)",
            [kid, title],
        )


def _seed_knowledge_items_pg(engine) -> None:
    with engine.begin() as conn:
        for kid, title in (("ki_1", "First fact"), ("ki_2", "Second fact")):
            conn.execute(
                sa.text(
                    "INSERT INTO knowledge_items (id, title) "
                    "VALUES (:id, :title)"
                ),
                {"id": kid, "title": title},
            )


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    _seed_knowledge_items_duckdb(conn)
    return MemoryDomainsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    _seed_knowledge_items_pg(pg_engine)

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.memory_domains_pg import MemoryDomainsPgRepository
    return MemoryDomainsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a memory_domains repo bound to either DuckDB or PG."""
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

def test_create_then_get_consistent(repo):
    did = repo.create(
        name="Sales", slug="sales", description="d",
        icon=None, color=None, created_by="u",
    )
    row = repo.get(did)
    assert row is not None
    assert did.startswith("md_")
    assert row["id"] == did
    assert row["slug"] == "sales"
    assert row["name"] == "Sales"
    assert row["description"] == "d"
    assert row["created_by"] == "u"


def test_get_by_slug_consistent(repo):
    did = repo.create(
        name="A", slug="a", description=None,
        icon=None, color=None, created_by="u",
    )
    found = repo.get_by_slug("a")
    assert found is not None
    assert found["id"] == did
    assert repo.get_by_slug("missing") is None


def test_exists_by_slug_consistent(repo):
    repo.create(
        name="X", slug="x", description=None,
        icon=None, color=None, created_by="u",
    )
    assert repo.exists_by_slug("x") is True
    assert repo.exists_by_slug("nope") is False


def test_ensure_seed_inserts_deterministic_id_then_noops(repo):
    """``ensure_seed`` (the lifespan canonical-domain seed) inserts under the
    caller-supplied deterministic id — unlike ``create``, which generates
    ``md_<uuid12>`` — and a second call is a no-op on both engines."""
    inserted = repo.ensure_seed(
        domain_id="md_probe", slug="probe", name="Probe", icon="🧪", color="#eeeeee"
    )
    assert inserted is True
    row = repo.get_by_slug("probe")
    assert row is not None
    assert row["id"] == "md_probe"
    assert row["name"] == "Probe"
    assert row["icon"] == "🧪"
    assert row["color"] == "#eeeeee"
    assert row["status"] == "prod"
    assert row["created_by"] == "system:seed"

    again = repo.ensure_seed(
        domain_id="md_probe", slug="probe", name="Probe", icon="🧪", color="#eeeeee"
    )
    assert again is False


def test_ensure_seed_never_modifies_existing_row(repo):
    """Admin customizations survive the boot-time re-seed."""
    repo.ensure_seed(domain_id="md_probe", slug="probe", name="Probe", icon=None, color=None)
    repo.update("md_probe", name="Renamed by admin")

    assert repo.ensure_seed(domain_id="md_probe", slug="probe", name="Probe", icon=None, color=None) is False
    assert repo.get("md_probe")["name"] == "Renamed by admin"


def test_ensure_seed_does_not_resurrect_soft_deleted(repo):
    """An admin-deleted canonical domain stays deleted across reboots — the
    soft-deleted row still holds its slug, so the seed insert no-ops."""
    repo.ensure_seed(domain_id="md_probe", slug="probe", name="Probe", icon=None, color=None)
    repo.delete("md_probe")

    assert repo.ensure_seed(domain_id="md_probe", slug="probe", name="Probe", icon=None, color=None) is False
    assert repo.get_by_slug("probe") is None
    assert repo.get("md_probe", include_deleted=True) is not None


def test_delete_round_trip(repo):
    """Soft delete hides the row; include_deleted reveals it; restore brings it back."""
    did = repo.create(
        name="X", slug="ghost", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.delete(did)
    assert repo.get(did) is None
    assert repo.get_by_slug("ghost") is None
    assert repo.get(did, include_deleted=True) is not None
    repo.restore(did)
    assert repo.get(did) is not None


def test_add_item_idempotent(repo):
    did = repo.create(
        name="Sales", slug="sales", description=None,
        icon=None, color=None, created_by="u",
    )
    assert repo.add_item(did, "ki_1", added_by="u") is True
    assert repo.add_item(did, "ki_1", added_by="u") is False


def test_list_domains_of_item_joins_correctly(repo):
    a = repo.create(
        name="A", slug="a", description=None,
        icon=None, color=None, created_by="u",
    )
    b = repo.create(
        name="B", slug="b", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.add_item(a, "ki_1", added_by="u")
    repo.add_item(b, "ki_1", added_by="u")
    domains = repo.list_domains_of_item("ki_1")
    assert sorted(d["id"] for d in domains) == sorted([a, b])


def test_resolve_ids_to_slugs_consistent(repo):
    """Empty → {}; live ids resolve; unknown + soft-deleted ids omitted."""
    # Empty input short-circuits to an empty mapping on both backends.
    assert repo.resolve_ids_to_slugs([]) == {}

    live = repo.create(
        name="Live", slug="live", description=None,
        icon=None, color=None, created_by="u",
    )
    gone = repo.create(
        name="Gone", slug="gone", description=None,
        icon=None, color=None, created_by="u",
    )
    repo.delete(gone)  # soft delete — must be omitted

    result = repo.resolve_ids_to_slugs([live, gone, "md_does_not_exist"])
    assert result == {live: "live"}
