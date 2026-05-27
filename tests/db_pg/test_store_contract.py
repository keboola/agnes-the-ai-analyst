"""Cross-engine contract tests for the store/marketplace repository cluster.

Targets: marketplace_registry_repo, store_entities_repo,
         user_store_installs_repo, store_submissions_repo.
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the pattern established in test_audit_contract.py.
"""
from __future__ import annotations

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repos(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "registry": MarketplaceRegistryRepository(conn),
        "entities": StoreEntitiesRepository(conn),
        "installs": UserStoreInstallsRepository(conn),
        "submissions": StoreSubmissionsRepository(conn),
        "users": UserRepository(conn),
    }, conn


def _make_pg_repos(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.marketplace_registry_pg import MarketplaceRegistryPgRepository
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    return {
        "registry": MarketplaceRegistryPgRepository(engine),
        "entities": StoreEntitiesPgRepository(engine),
        "installs": UserStoreInstallsPgRepository(engine),
        "submissions": StoreSubmissionsPgRepository(engine),
        "users": UsersPgRepository(engine),
    }, None


@pytest.fixture(params=["duckdb", "pg"])
def store_repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repos_dict, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repos, conn = _make_duckdb_repos(tmp_path)
        yield repos, conn, backend
        if conn is not None:
            conn.close()
    else:
        repos, _ = _make_pg_repos(pg_engine, monkeypatch)
        yield repos, None, backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_entity(repo, **kwargs):
    defaults = dict(
        id="entity-1",
        owner_user_id="user-1",
        owner_username="alice",
        type="skill",
        name="my-skill",
        description="A test skill",
        category="Productivity",
        version="abc123",
        file_size=1024,
        visibility_status="pending",
    )
    defaults.update(kwargs)
    return repo.create(**defaults)


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------

def test_register_marketplace_then_list_returns_it(store_repos):
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(
        id="mp-1",
        name="Test Marketplace",
        url="https://example.com/repo.git",
        curator_name="alice",
    )
    all_regs = reg.list_all()
    ids = [r["id"] for r in all_regs]
    assert "mp-1" in ids
    fetched = reg.get("mp-1")
    assert fetched is not None
    assert fetched["name"] == "Test Marketplace"
    assert fetched["curator_name"] == "alice"


def test_register_marketplace_upsert_preserves_curator(store_repos):
    """Re-register with curator_name=None must NOT clobber existing curator."""
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(id="mp-2", name="MP v1", url="https://example.com/r.git", curator_name="bob")
    reg.register(id="mp-2", name="MP v2", url="https://example.com/r.git")
    row = reg.get("mp-2")
    assert row["name"] == "MP v2"
    assert row["curator_name"] == "bob"


def test_store_entity_create_then_get(store_repos):
    repos, _, backend = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    entity = _make_entity(repos["entities"])
    assert entity["id"] == "entity-1"
    assert entity["name"] == "my-skill"
    assert entity["visibility_status"] == "pending"
    fetched = repos["entities"].get("entity-1")
    assert fetched is not None
    assert fetched["id"] == "entity-1"


def test_store_entity_set_visibility_approved(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])
    repos["entities"].set_visibility("entity-1", "approved")
    row = repos["entities"].get("entity-1")
    assert row["visibility_status"] == "approved"


def test_install_plugin_creates_row(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    result = repos["installs"].install("user-1", "entity-1")
    assert result is True  # new row created

    # Idempotent — second call returns False
    result2 = repos["installs"].install("user-1", "entity-1")
    assert result2 is False


def test_uninstall_removes_row(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    repos["installs"].install("user-1", "entity-1")
    removed = repos["installs"].uninstall("user-1", "entity-1")
    assert removed is True

    removed2 = repos["installs"].uninstall("user-1", "entity-1")
    assert removed2 is False


def test_submission_lifecycle_pending_to_approved(store_repos):
    """create → status='pending_llm' → update_status to 'approved' → get reflects it."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-1",
    )
    assert isinstance(sub_id, str) and len(sub_id) > 0

    row = repos["submissions"].get(sub_id)
    assert row is not None
    assert row["status"] == "pending_llm"

    updated = repos["submissions"].update_status(sub_id, status="approved")
    assert updated is True

    row2 = repos["submissions"].get(sub_id)
    assert row2["status"] == "approved"


def test_submission_terminal_status_not_overwritten(store_repos):
    """update_status must not overwrite a terminal state (CAS guard)."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="approved",
        entity_id="entity-1",
    )
    # Attempt to overwrite terminal 'approved' without allow flag
    updated = repos["submissions"].update_status(sub_id, status="pending_llm")
    assert updated is False

    row = repos["submissions"].get(sub_id)
    assert row["status"] == "approved"


def test_archived_entity_visible_with_list(store_repos):
    """Archived entity survives get(); list with visibility filter controls visibility."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    # Archive it
    repos["entities"].archive("entity-1", by_user_id="user-1")
    row = repos["entities"].get("entity-1")
    assert row is not None
    assert row["visibility_status"] == "archived"

    # list() with visibility_status=["approved"] must NOT include archived
    items, total = repos["entities"].list(visibility_status=["approved"])
    ids = [i["id"] for i in items]
    assert "entity-1" not in ids

    # list() without filter (admin view) DOES include archived
    items_all, total_all = repos["entities"].list()
    ids_all = [i["id"] for i in items_all]
    assert "entity-1" in ids_all
