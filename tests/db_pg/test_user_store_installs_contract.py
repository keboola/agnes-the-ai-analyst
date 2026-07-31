"""Cross-engine contract tests for the user_store_installs repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both
backends; the same rows must come back.

The rule under test is ``list_for_user``'s visibility filter — the single
chokepoint every surface reads through (Library, /stack, marketplace browse,
and the served marketplace.zip). It is not a plain ``IN`` list: an entity kept
Private serves to its own author and to nobody else, and guardrail quarantine
writes the same ``visibility_status='hidden'`` as the Private choice, so the
branch carries a correlated ``NOT EXISTS`` over ``store_submissions``. Two
engines hand-maintaining that clause is exactly the drift this file exists to
catch.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

OWNER = "owner-1"
OTHER = "other-1"


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return (
        UserStoreInstallsRepository(conn),
        StoreEntitiesRepository(conn),
        StoreSubmissionsRepository(conn),
        conn,
    )


def _make_pg(pg_engine, monkeypatch):
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

    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository

    engine = db_pg.get_engine()
    return (
        UserStoreInstallsPgRepository(engine),
        StoreEntitiesPgRepository(engine),
        StoreSubmissionsPgRepository(engine),
        None,
    )


@pytest.fixture(params=["duckdb", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(installs, entities, submissions)`` bound to one backend."""
    if request.param == "duckdb":
        installs, entities, submissions, conn = _make_duckdb(tmp_path)
        yield installs, entities, submissions
        if conn is not None:
            conn.close()
    else:
        installs, entities, submissions, _ = _make_pg(pg_engine, monkeypatch)
        yield installs, entities, submissions


def _entity(entities, *, name: str, visibility_status: str, owner: str = OWNER) -> str:
    eid = f"ent-{name}"
    entities.create(
        id=eid,
        owner_user_id=owner,
        owner_username=owner,
        type="skill",
        name=name,
        description="A description long enough to look like a real one.",
        category=None,
        version="1.0.0",
        file_size=100,
        visibility_status=visibility_status,
    )
    return eid


def _reject(submissions, entity_id: str, *, status: str = "blocked_llm") -> None:
    submissions.create(
        submitter_id=OWNER,
        submitter_email="owner@x.com",
        type="skill",
        name=entity_id,
        version="1.0.0",
        status=status,
        entity_id=entity_id,
        file_size=100,
        bundle_sha256="0" * 64,
    )


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["approved", "archived"])
def test_approved_and_archived_serve_to_any_installer(repos, status):
    installs, entities, _subs = repos
    eid = _entity(entities, name=f"pub-{status}", visibility_status=status)
    installs.install(OTHER, eid)
    assert [r["id"] for r in installs.list_for_user(OTHER)] == [eid]


def test_own_hidden_serves_to_its_author(repos):
    """The Private tier: an entity kept private reaches the Stack of the person
    who authored it."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="mine", visibility_status="hidden")
    installs.install(OWNER, eid)
    assert [r["id"] for r in installs.list_for_user(OWNER)] == [eid]


def test_hidden_never_serves_to_a_third_party(repos):
    installs, entities, _subs = repos
    eid = _entity(entities, name="notyours", visibility_status="hidden")
    installs.install(OTHER, eid)
    assert installs.list_for_user(OTHER) == []


def test_rejected_hidden_never_serves_even_to_its_author(repos):
    """Quarantine writes the same status as Private, so the blocking verdict is
    the only separator. A bundle review rejected stays out."""
    installs, entities, subs = repos
    eid = _entity(entities, name="rejected", visibility_status="hidden")
    _reject(subs, eid)
    installs.install(OWNER, eid)
    assert installs.list_for_user(OWNER) == []


def test_review_error_is_not_a_rejection(repos):
    """No verdict is not the same as a rejection — an instance without LLM
    credentials must not silently stop serving its users' Private uploads."""
    installs, entities, subs = repos
    eid = _entity(entities, name="noverdict", visibility_status="hidden")
    _reject(subs, eid, status="review_error")
    installs.install(OWNER, eid)
    assert [r["id"] for r in installs.list_for_user(OWNER)] == [eid]


def test_pending_never_serves(repos):
    """Only 'hidden' gets the owner exemption; an entity still awaiting review
    on the share path is excluded for everyone, author included."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="inreview", visibility_status="pending")
    installs.install(OWNER, eid)
    assert installs.list_for_user(OWNER) == []


def test_list_for_user_projection_matches_across_engines(repos):
    """The joined projection is what marketplace_filter builds plugin entries
    from — a column missing on one engine is a serve-time KeyError."""
    installs, entities, _subs = repos
    eid = _entity(entities, name="shaped", visibility_status="approved")
    installs.install(OTHER, eid)
    row = installs.list_for_user(OTHER)[0]
    for key in (
        "id",
        "owner_user_id",
        "owner_username",
        "type",
        "name",
        "description",
        "version",
        "visibility_status",
        "synthetic_name",
        "installed_at",
    ):
        assert key in row, f"{key} missing from list_for_user projection"
    assert row["owner_user_id"] == OWNER
    assert row["visibility_status"] == "approved"
