"""Unit tests for WelcomeTemplateRepository."""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.welcome_template import WelcomeTemplateRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    yield c
    c.close()


def test_get_returns_none_on_fresh_install(conn):
    repo = WelcomeTemplateRepository(conn)
    row = repo.get()
    assert row is not None
    assert row["content"] is None  # default sentinel


def test_set_stores_content(conn):
    repo = WelcomeTemplateRepository(conn)
    repo.set("Hello {{ instance.name }}", updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] == "Hello {{ instance.name }}"
    assert row["updated_by"] == "admin@example.com"
    assert row["updated_at"] is not None


def test_reset_clears_content(conn):
    repo = WelcomeTemplateRepository(conn)
    repo.set("custom", updated_by="admin@example.com")
    repo.reset(updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] is None
