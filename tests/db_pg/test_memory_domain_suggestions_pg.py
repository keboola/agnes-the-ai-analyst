"""Integration tests for MemoryDomainSuggestionsPgRepository.

PG-side smoke. Cross-engine parity covered in Task 1D.3 contract test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    """Per-test repo bound to a freshly-migrated PG schema.

    Mirrors ``test_memory_domains_pg.py``: alembic upgrade head, then
    wrap a ``MemoryDomainSuggestionsPgRepository`` around the engine.
    No seed rows needed — suggestions are independent of domains until
    the approve path stamps ``created_domain_id`` (audit-only, no FK).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.memory_domain_suggestions_pg import (
        MemoryDomainSuggestionsPgRepository,
    )
    return MemoryDomainSuggestionsPgRepository(pg_engine)


def test_create_then_get(repo):
    sid = repo.create(
        name="Finance",
        description="Finance domain proposal",
        rationale="We have lots of finance facts",
        created_by="alice@example.com",
    )
    row = repo.get(sid)
    assert row is not None
    assert sid.startswith("sug_")
    assert row["name"] == "Finance"
    assert row["status"] == "pending"
    assert row["created_by"] == "alice@example.com"
    assert row["resolved_at"] is None


def test_count_pending(repo):
    repo.create(name="A", description=None, rationale=None, created_by="u")
    repo.create(name="B", description=None, rationale=None, created_by="u")
    assert repo.count_pending() == 2


def test_resolve_to_approved(repo):
    sid = repo.create(
        name="X", description=None, rationale=None, created_by="u"
    )
    ok = repo.resolve(
        sid,
        status="approved",
        resolved_by="admin@example.com",
        created_domain_id="md_abc",
    )
    assert ok is True
    row = repo.get(sid)
    assert row["status"] == "approved"
    assert row["created_domain_id"] == "md_abc"
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "admin@example.com"


def test_list_filtered_by_status(repo):
    a = repo.create(name="A", description=None, rationale=None, created_by="u")
    b = repo.create(name="B", description=None, rationale=None, created_by="u")
    repo.resolve(
        a,
        status="approved",
        resolved_by="admin@example.com",
        created_domain_id="md_x",
    )
    pending = repo.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == b
